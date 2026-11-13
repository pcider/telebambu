from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

from bambulabs_api import GcodeState

from .manager import (PrinterManager, PrinterEvent, EventType, format_duration, to_int, run_blocking,
                      BLOCKING_CALL_TIMEOUT)
from .timing import LoopLagProbe, StepTimer, span, use_timer

from errors import is_filament_runout, lookup_error

if TYPE_CHECKING:
    from bot.messages import MessageService
    from data.storage import PrintSession
    from settings import Settings

PRINT_STARTED_DELAY = 2  # seconds to let the printer update its time estimate
# Don't flag a missing camera frame until this long after a printer connects/restarts,
# since the camera stream can take a while to establish (TLS handshake, auth, retries)
CAMERA_STARTUP_GRACE = 30  # seconds
# Warn the claimer once per tray when the active spool drops to this many percent
LOW_FILAMENT_PERCENT = 10
# Slow updates are logged one by one to stdout, and to the log chat at most this often, grouped
SLOW_UPDATE_REPORT_INTERVAL = 60  # seconds
# Written with the time at the start of every update, so the router's watchdog
# (deploy/openwrt/telebambu-watchdog) can tell a frozen bot from a running one. tmpfs, so no flash wear.
HEARTBEAT_FILE = os.environ.get('TELEBAMBU_HEARTBEAT', '/tmp/telebambu.heartbeat')
# Only auto-restart printers in these states, so a running print is never interrupted
NOT_PRINTING_STATES = (GcodeState.IDLE, GcodeState.FINISH)
# States where a print is underway
ACTIVE_PRINT_STATES = (GcodeState.RUNNING, GcodeState.PAUSE)


def _print_name(name: str) -> str:
    """A print name without its file extension, so 'X' and 'X.gcode.3mf' compare equal."""
    for suffix in ('.gcode.3mf', '.3mf', '.gcode'):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def _is_replaced(session: PrintSession | None, subtask: str, gcode_file: str) -> bool:
    """Whether the printer's current print is provably a different one from the stored session.
    Names are only compared like for like, since either can be missing (no subtask before MQTT
    sends one) and gcode names like 'plate_1.gcode' repeat across projects."""
    if not session:
        return False
    if session.subtask_name and subtask:
        return _print_name(session.subtask_name) != _print_name(subtask)
    if session.gcode_file and gcode_file:
        return _print_name(session.gcode_file) != _print_name(gcode_file)
    if session.subtask_name or session.gcode_file or not session.file_name:
        return False  # Nothing comparable, assume the same print
    # Older sessions stored one name, which could have been either
    current = {_print_name(n) for n in (subtask, gcode_file) if n}
    return bool(current) and _print_name(session.file_name) not in current


class PrinterMonitor:
    def __init__(self, printer_manager: PrinterManager, message_service: MessageService, settings: Settings):
        self.pm = printer_manager
        self.ms = message_service
        self.storage = message_service.storage
        # Settings are read on every tick, so /config changes apply without a restart
        self.settings = settings
        # Printers already reported as having a stale camera, to avoid spam
        self._stale_camera_reported: set[int] = set()
        # Time between tick starts, so event loop stalls show up on top of the sleep interval
        self._stats_since = time.monotonic()
        self._last_tick_at: float | None = None
        self._tick_gaps: list[float] = []
        # Step timings of the previous update, the event loop's lag, and the slowest update so far
        self._last_timer: StepTimer | None = None
        self._lag_probe = LoopLagProbe()
        self.heartbeat_file: str | None = HEARTBEAT_FILE  # None turns the heartbeat off
        self._heartbeat_failed = False
        self._slowest: tuple[float, str] | None = None  # (gap, breakdown) in the stats window
        self._slow_count = 0  # slow updates not yet reported to the log chat
        self._slow_worst: tuple[float, str] | None = None
        self._slow_reported_at = time.monotonic()
        # Per printer: a worker handling its events and checks off the update, so a camera capture
        # or a Telegram send can't make the update late
        self._workers: dict[int, _PrinterWorker] = {}
        # Background status message edit and log flush (task, start time), at most one of each at a time
        self._background: dict[str, tuple[asyncio.Task, float]] = {}

    async def run(self):
        """Start an update every update_interval on a fixed clock. Each update only polls the
        printers and hands work to background tasks, and gives up after update_time_budget."""
        self._lag_probe.start()
        next_at = time.monotonic()
        try:
            while True:
                interval = self.settings.update_interval
                next_at += interval
                now = time.monotonic()
                if next_at < now:
                    next_at = now  # fell behind: start now rather than bursting to catch up
                await asyncio.sleep(next_at - now)
                woke_late = time.monotonic() - next_at
                self._write_heartbeat()
                gap = self._record_tick()
                try:
                    if gap is not None:
                        await self._check_slow_update(gap, interval, woke_late)
                    await self._report_slow_updates_if_due()
                except Exception as e:
                    print(f'Failed to check update timing: {e}')

                timer = StepTimer()
                with use_timer(timer):
                    await self._run_update(timer)
                timer.finish()
                self._last_timer = timer
        finally:
            self._lag_probe.stop()
            self._stop_background()

    def _write_heartbeat(self):
        """Record that the monitor loop is alive (a frozen event loop or a dead loop stops this)."""
        if not self.heartbeat_file:
            return
        try:
            tmp_file = self.heartbeat_file + '.tmp'
            with open(tmp_file, 'w') as f:
                f.write(f'{time.time():.0f}\n')
            os.replace(tmp_file, self.heartbeat_file)
            self._heartbeat_failed = False
        except OSError as e:
            if not self._heartbeat_failed:  # once per outage
                print(f'Failed to write heartbeat {self.heartbeat_file}: {e}')
                self._heartbeat_failed = True

    async def _run_update(self, timer: StepTimer):
        budget = self.settings.update_time_budget
        deadline = asyncio.timeout(budget)
        try:
            async with deadline:
                with span('stats'):
                    await self.log_stats_if_due()
                await self.tick()
        except TimeoutError:
            if not deadline.expired():
                raise
            steps = ' < '.join(timer.interrupted) or 'unknown'
            print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Update stopped at its {budget}s budget '
                  f'during: {steps} [{timer.breakdown()}]')
        except Exception as e:
            print(f'Monitor tick failed: {e}')

    def _record_tick(self) -> float | None:
        """Note the start of an update; returns the time since the previous one started."""
        now = time.monotonic()
        gap = None
        if self._last_tick_at is not None:
            gap = now - self._last_tick_at
            self._tick_gaps.append(gap)
        self._last_tick_at = now
        return gap

    def _describe_gap(self, gap: float, expected: float, woke_late: float, loop_lag: float) -> str:
        """Where the time between two update starts went, e.g. for the 39.28s max in the stats."""
        prev = self._last_timer
        took = f'{prev.total:.2f}s [{prev.breakdown()}]' if prev else 'unknown'
        return (f'{gap:.2f}s (+{gap - expected:.2f}s): previous update took {took}; '
                f'sleep woke {woke_late:.2f}s late; event loop blocked up to {loop_lag:.2f}s')

    async def _check_slow_update(self, gap: float, expected: float, woke_late: float):
        """Log the step breakdown of an update that started update_deviation_warn late or more."""
        loop_lag = self._lag_probe.take_max()
        deviation = self.settings.update_deviation_warn
        is_slow = deviation is not None and gap - expected > deviation
        is_slowest = self._slowest is None or gap > self._slowest[0]
        if not (is_slow or is_slowest):
            return
        detail = self._describe_gap(gap, expected, woke_late, loop_lag)
        if is_slowest:
            self._slowest = (gap, detail)
        if not is_slow:
            return
        await self.ms.log_message(f'Slow update {detail}', stdout_only=True)
        self._slow_count += 1
        if self._slow_worst is None or gap > self._slow_worst[0]:
            self._slow_worst = (gap, detail)

    async def _report_slow_updates_if_due(self):
        """Send the slow updates since the last report to the log chat, grouped, so they can't flood it."""
        if time.monotonic() - self._slow_reported_at < SLOW_UPDATE_REPORT_INTERVAL:
            return
        if self._slow_count:
            count = self._slow_count
            plural = 's' if count != 1 else ''
            await self.ms.log_message(f'{count} slow update{plural} in the last '
                                      f'{format_duration(round((time.monotonic() - self._slow_reported_at) / 60))}, '
                                      f'worst {self._slow_worst[1]}')
        self._slow_count = 0
        self._slow_worst = None
        self._slow_reported_at = time.monotonic()

    async def log_stats_if_due(self):
        """Log min/avg/max time between updates and every printer's status, then start a new window."""
        stats_interval = self.settings.stats_log_interval
        if not stats_interval or time.monotonic() - self._stats_since < stats_interval:
            return

        gaps = self._tick_gaps
        if gaps:
            ordered = sorted(gaps)
            p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
            timing = (f'{len(gaps)} updates, avg {sum(gaps) / len(gaps):.2f}s, '
                      f'min {min(gaps):.2f}s, p95 {p95:.2f}s, max {max(gaps):.2f}s')
            deviation = self.settings.update_deviation_warn
            if deviation is not None:
                limit = self.settings.update_interval + deviation
                timing += f', {sum(g > limit for g in gaps)} over {limit:.1f}s'
            if self._slowest:
                timing += f'\nSlowest: {self._slowest[1]}'
        else:
            timing = 'no updates'
        window = format_duration(round((time.monotonic() - self._stats_since) / 60))
        status = '\n'.join(self.pm.get_status_lines())
        await self.ms.log_message(f'Update stats (last {window}): {timing}\n{status}')

        self._tick_gaps = []
        self._slowest = None
        self._stats_since = time.monotonic()

    async def tick(self):
        """One update: poll every printer and queue the work. Nothing here waits on Telegram, a
        camera or a printer command; those run in the background."""
        with span('reconnect'):
            await self.pm.reconnect_if_needed(self.ms.log_message)
        self.pm.request_pushalls()

        with span('status text'):
            status = self.pm.get_status_text()
        self._in_background('status msg', lambda: self.ms.update_status_message(status))

        numbers = self.pm.numbers()
        for num in numbers:
            # Polling compares cached MQTT state, so it's quick; the events go to the printer's worker
            events = self.pm.poll(num)
            self._worker(num).submit(events)
        for num in set(self._workers) - set(numbers):
            self._workers.pop(num).stop()  # printer removed with /printer del

        self._in_background('flush logs', self.ms.flush_logs)

    def _worker(self, num: int) -> _PrinterWorker:
        worker = self._workers.get(num)
        if worker is None:
            worker = self._workers[num] = _PrinterWorker(num, self._process_printer)
        return worker

    def _in_background(self, name: str, make_coro):
        """Run a step as a background task, skipping it while the previous one is still running."""
        running = self._background.get(name)
        if running and not running[0].done():
            print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Skipped {name}: the previous one is still running '
                  f'({time.monotonic() - running[1]:.1f}s)')
            return
        self._background[name] = (asyncio.create_task(self._timed_job(name, make_coro)), time.monotonic())

    async def _timed_job(self, name: str, make_coro):
        """Run background work with its own step timer, logging it if it took longer than the budget."""
        timer = StepTimer()
        with use_timer(timer):
            try:
                with span(name):
                    await make_coro()
            except Exception as e:
                print(f'Failed: {name}: {e}')
        timer.finish()
        if timer.total > self.settings.update_time_budget:
            print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Background {name} took {timer.total:.2f}s '
                  f'[{timer.breakdown()}]')

    def _stop_background(self):
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()
        for task, _started in self._background.values():
            task.cancel()
        self._background.clear()

    async def _process_printer(self, num: int, events: list[PrinterEvent]):
        """Handle a printer's events, then run its checks. Runs in the printer's worker."""
        await self._timed_job(f'P{num} work', lambda: self._handle_printer(num, events))

    async def _handle_printer(self, num: int, events: list[PrinterEvent]):
        # Handle events first, so checks below see sessions already caught up after a restart
        for event in events:
            try:
                with span(f'P{num} {event.type.name}'):
                    await self._handle_event(event)
            except Exception as e:
                print(f'Error handling event {event.type} for printer {num}: {e}')

        try:
            for check in (self._check_stale_camera, self._check_periodic_camera, self._check_low_filament):
                with span(f'P{num} {check.__name__.removeprefix("_check_")}'):
                    await check(num)
        except Exception as e:
            print(f'Error running checks for printer {num}: {e}')

    async def _check_stale_camera(self, num: int):
        """Auto-restart an idle/finished printer whose camera has stopped sending frames (once per outage)."""
        printer = self.pm.get_online_printer(num)
        if not printer:
            return
        if self.pm.seconds_since_connect(num) < CAMERA_STARTUP_GRACE:
            return

        has_frame = self.pm.has_camera_frame(num)
        state = printer.get_state()
        if not has_frame and state in NOT_PRINTING_STATES:
            if num not in self._stale_camera_reported:
                self._stale_camera_reported.add(num)
                if not self.settings.auto_restart_printers:
                    await self.ms.log_message(f"Printer {num} has no camera ({state}). Auto-restart disabled, skipping.")
                    return
                await self.ms.log_message(f"Printer {num} has no camera ({state}). Auto-restarting...")
                try:
                    await self.pm.restart(num)
                except Exception as e:
                    await self.ms.log_message(f"Failed to auto-restart Printer {num}: {e}")
        elif has_frame:
            # Camera recovered, clear the flag
            self._stale_camera_reported.discard(num)

    async def _check_low_filament(self, num: int):
        """Warn a claimed print's users once when the spool in use is nearly empty."""
        session = self.storage.get_print(num)
        if not session or not session.claimed_by:
            return
        snap = self.pm.snapshot(num)
        if not snap or snap.state != GcodeState.RUNNING:
            return
        tray = next((t for t in self.pm.filament(num) if t.active), None)
        if not tray or not 0 <= tray.remain <= LOW_FILAMENT_PERCENT or tray.tray_id in session.low_filament_alerted:
            return
        self.storage.mark_low_filament_alerted(num, tray.tray_id)
        await self.ms.send_filament_notification(num, f"Filament is running low ({tray.describe()}).")

    async def _check_periodic_camera(self, num: int):
        """Send a recurring camera snapshot when the claimer's configured trigger is due."""
        session = self.storage.get_print(num)
        if not session or not session.claimed_by or not session.notify_every_type:
            return
        snap = self.pm.snapshot(num)
        if not snap or snap.state != GcodeState.RUNNING:
            return

        interval = session.notify_every_value or 0
        trigger_value = None
        if session.notify_every_type in ("layers", "percent"):
            current = snap.layer if session.notify_every_type == "layers" else snap.progress
            trigger_value = current // interval if interval else 0
            due = trigger_value > (session.notify_every_last_value or 0)
        else:  # "time"
            due = time.time() - (session.notify_every_last_sent_at or 0) >= interval * 60

        if due:
            frame = await self.pm.get_camera_frame(num)
            await self.ms.send_periodic_camera_notification(num, snap, trigger_value, frame)

    async def _handle_event(self, event: PrinterEvent):
        printer = event.printer
        num = event.printer_num

        if event.type == EventType.STATE_CHANGED:
            # Log state changes to stdout only (not to Telegram)
            if 'new' in event.data:
                await self.ms.log_message(
                    f'Printer {num} GCODE state: {event.data["prev"]} -> {event.data["new"]}', stdout_only=True)
            else:
                await self.ms.log_message(
                    f'Printer {num} PRINT state: {event.data["prev_print"]} -> {event.data["new_print"]}', stdout_only=True)

        elif event.type == EventType.PRINT_STARTED:
            await asyncio.sleep(PRINT_STARTED_DELAY)
            await self.ms.send_print_started(num, format_duration(printer.get_time()), to_int(printer.total_layer_num()),
                                             self.pm.file_name(num) or None, names=self.pm.print_names(num))

        elif event.type == EventType.FIRST_SEEN:
            await self._reconcile_on_startup(num, event.data['state'])

        elif event.type == EventType.PRINT_FINISHED:
            await self.ms.send_print_finished(num, await self.pm.get_camera_frame(num))

        elif event.type == EventType.PRINT_FAILED:
            frame = await self.pm.get_camera_frame(num)
            await self.ms.send_print_failed(num, event.data.get('error_code'), frame)

        elif event.type == EventType.PRINT_PAUSED:
            frame = await self.pm.get_camera_frame(num)
            await self.ms.log_message(f'Printer {num} has paused printing. (code: {event.data.get("error_code")})', frame)
            reason = lookup_error(event.data.get('error_code'))
            if is_filament_runout(reason):
                await self.ms.send_filament_notification(num, f"Paused because the filament ran out. {reason}", frame)

        elif event.type == EventType.LAYER_CHANGED:
            await self._handle_layer_change(num, event.data['prev_layer'], event.data['layer'])

    async def _reconcile_on_startup(self, num: int, state: GcodeState):
        """Compare a printer's live state with the print stored before the restart, and
        catch up on whatever changed while the bot was down."""
        printer = self.pm.get_printer(num)
        session = self.storage.get_print(num)
        current = self.pm.file_name(num) or None
        names = self.pm.print_names(num)
        replaced = _is_replaced(session, *names)
        prefix = f'Startup: Printer {num}'

        async def announce_current():
            await self.ms.send_print_started(num, format_duration(printer.get_time()), to_int(printer.total_layer_num()),
                                             file_name=current, catchup=True, names=names)

        if state in ACTIVE_PRINT_STATES:
            if not session:
                await announce_current()
                await self.ms.log_message(f"{prefix}: untracked print '{current}' found running, announced [catchup]")
            elif replaced:
                # The camera now shows the new print, so the old one gets no image
                await self.ms.send_print_finished(num, None, catchup=True)
                await announce_current()
                await self.ms.log_message(f"{prefix}: stored print '{session.file_name}' replaced by '{current}' while offline")
            else:
                note = '' if session.file_name else ' (no stored file name, assuming same print)'
                await self.ms.log_message(f"{prefix}: resumed tracked print '{current}'{note}")

        elif state in (GcodeState.FINISH, GcodeState.IDLE) and session:
            await self.ms.send_print_finished(num, await self.pm.get_camera_frame(num), catchup=True)
            await self.ms.log_message(f'{prefix}: tracked print ended while offline, sent [catchup] finish')

        elif state == GcodeState.FAILED and session:
            frame = await self.pm.get_camera_frame(num)
            await self.ms.send_print_failed(num, printer.print_error_code(), frame, catchup=True)
            await self.ms.log_message(f'{prefix}: tracked print failed while offline, sent [catchup] failure')

        elif state == GcodeState.PREPARE and replaced:
            # The PREPARE -> RUNNING change announces the new print as usual
            await self.ms.send_print_finished(num, None, catchup=True)
            await self.ms.log_message(f'{prefix}: stored print replaced, new print preparing')

        else:
            await self.ms.log_message(f'{prefix}: {state}, nothing to catch up')

    async def _stop_print(self, num: int, layer: int):
        """Stop a print that reached its /stop_at layer."""
        printer = self.pm.get_online_printer(num)
        if not printer:
            return
        try:
            await run_blocking(printer.stop_print, BLOCKING_CALL_TIMEOUT)
        except Exception as e:
            # stop_at_layer is kept, so the next layer change tries again
            await self.ms.log_message(f'Failed to send stop to Printer {num} at layer {layer}: {e}')
            return
        # The print's end is reported by the state change it causes (FAILED), not here
        self.storage.mark_stopped(num, layer)
        await self.ms.log_message(f'Printer {num}: sent stop at layer {layer} (/stop_at).')

    async def _handle_layer_change(self, num: int, prev_layer: int, layer: int):
        session = self.storage.get_print(num)
        if session and session.stop_at_layer and layer >= session.stop_at_layer:
            await self._stop_print(num, layer)
            return
        if not session or not session.claimed_by:
            return

        # Use crossings rather than exact matches so a missed poll doesn't skip a notification
        layer2_due = session.layer2_notify and not session.layer2_notified and prev_layer < 2 <= layer
        custom_due = bool(session.notify_layer) and not session.notify_layer_notified and layer >= session.notify_layer
        if not (layer2_due or custom_due):
            return

        # One capture serves both notifications
        frame = await self.pm.get_camera_frame(num)
        if layer2_due:
            await self.ms.send_layer2_notification(num, frame)
        if custom_due:
            await self.ms.send_custom_layer_notification(num, frame)


class _PrinterWorker:
    """Handles one printer's events and checks in order, in the background. Events that arrive
    while it's busy are queued and handled together on its next round, so none are lost and a
    slow round (e.g. a 20s camera capture) never delays the monitor's updates."""

    def __init__(self, num: int, process):
        self.num = num
        self._process = process
        self._events: list[PrinterEvent] = []
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    def submit(self, events: list[PrinterEvent]):
        self._events.extend(events)
        self._wake.set()

    def stop(self):
        self._task.cancel()

    async def _run(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            events, self._events = self._events, []
            try:
                await self._process(self.num, events)
            except Exception as e:
                print(f'Printer {self.num} worker failed: {e}')
