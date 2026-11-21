"""Timing for the monitor's updates, so a slow update can be traced to the step that caused it."""
from __future__ import annotations

import asyncio
import contextvars
import time
from contextlib import contextmanager

# Spans shorter than this are left out of breakdowns
MIN_REPORTED_SPAN = 0.05  # seconds
# How often the loop lag probe wakes up
LAG_PROBE_INTERVAL = 0.25  # seconds


class StepTimer:
    """Wall-clock time spent in named steps. Steps for different printers run concurrently,
    so their times overlap and don't add up to the total."""

    def __init__(self):
        self.started = time.monotonic()
        self.total: float | None = None  # set by finish()
        self.spans: dict[str, float] = {}
        self._open: dict[str, int] = {}
        self.interrupted: list[str] = []

    @contextmanager
    def span(self, name: str):
        """Add the time spent inside the block to `name` (works across awaits)."""
        start = time.monotonic()
        self._open[name] = self._open.get(name, 0) + 1
        try:
            yield
        except asyncio.CancelledError:
            # Cut off (e.g. by the update's time budget): remember where, innermost step first
            self.interrupted.append(name)
            raise
        finally:
            self.spans[name] = self.spans.get(name, 0.0) + time.monotonic() - start
            self._open[name] -= 1
            if not self._open[name]:
                del self._open[name]

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def finish(self):
        self.total = self.elapsed()

    def open_steps(self) -> list[str]:
        """Steps that haven't finished yet."""
        return list(self._open)

    def breakdown(self, limit: int = 8) -> str:
        """The slowest steps, longest first, e.g. 'P4 PRINT_FINISHED 31.4s, status msg 5.2s'."""
        slow = sorted(((t, n) for n, t in self.spans.items() if t >= MIN_REPORTED_SPAN), reverse=True)
        if not slow:
            return 'no step over 0.05s'
        text = ', '.join(f'{name} {t:.2f}s' for t, name in slow[:limit])
        if len(slow) > limit:
            text += f', +{len(slow) - limit} more'
        return text


class _NoTimer(StepTimer):
    """Used outside a timed update, so span() can be called anywhere."""

    @contextmanager
    def span(self, name: str):
        yield


_current: contextvars.ContextVar[StepTimer] = contextvars.ContextVar('step_timer', default=_NoTimer())


def current_timer() -> StepTimer:
    """The timer of the update (or background job) this code is running for."""
    return _current.get()


def span(name: str):
    """Time a step against the current update's timer."""
    return _current.get().span(name)


@contextmanager
def use_timer(timer: StepTimer):
    """Make `timer` the current one; tasks created inside inherit it."""
    token = _current.set(timer)
    try:
        yield timer
    finally:
        _current.reset(token)


class LoopLagProbe:
    """Measures how late the event loop wakes a short sleep. Anything that blocks the loop
    (a synchronous network call, a slow library call) shows up here, wherever it ran."""

    def __init__(self, interval: float = LAG_PROBE_INTERVAL):
        self.interval = interval
        self._max_lag = 0.0
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        while True:
            start = time.monotonic()
            await asyncio.sleep(self.interval)
            self._max_lag = max(self._max_lag, time.monotonic() - start - self.interval)

    def take_max(self) -> float:
        """Longest lag since the last call."""
        lag, self._max_lag = self._max_lag, 0.0
        return lag
