from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from bambulabs_api import GcodeState

from .manager import PrinterManager, PrinterEvent, EventType, format_duration, to_int

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

    async def run(self):
        while True:
            await asyncio.sleep(self.settings.update_interval)
            self._record_tick()
            try:
                await self.log_stats_if_due()
                await self.tick()
            except Exception as e:
                print(f'Monitor tick failed: {e}')

    def _record_tick(self):
        now = time.monotonic()
        if self._last_tick_at is not None:
            self._tick_gaps.append(now - self._last_tick_at)
        self._last_tick_at = now

    async def log_stats_if_due(self):
        """Log min/avg/max time between updates and every printer's status, then start a new window."""
        stats_interval = self.settings.stats_log_interval
        if not stats_interval or time.monotonic() - self._stats_since < stats_interval:
            return

        gaps = self._tick_gaps
        if gaps:
            timing = (f'{len(gaps)} updates, avg {sum(gaps) / len(gaps):.2f}s, '
                      f'min {min(gaps):.2f}s, max {max(gaps):.2f}s')
        else:
            timing = 'no updates'
        window = format_duration(round((time.monotonic() - self._stats_since) / 60))
        status = '\n'.join(self.pm.get_status_lines())
        await self.ms.log_message(f'Update stats (last {window}): {timing}\n{status}')

        self._tick_gaps = []
        self._stats_since = time.monotonic()

    async def tick(self):
        await self.pm.reconnect_if_needed(self.ms.log_message)

        try:
            await self.ms.update_status_message(self.pm.get_status_text())
        except Exception as e:
            print(f'Failed to update status message: {e}')

        # Printers are independent, so a slow camera capture on one doesn't hold up the rest
        await asyncio.gather(*(self._process_printer(num) for num in self.pm.numbers()))

        await self.ms.flush_logs()

    async def _process_printer(self, num: int):
        # Handle events first, so checks below see sessions already caught up after a restart
        for event in self.pm.poll(num):
            try:
                await self._handle_event(event)
            except Exception as e:
                print(f'Error handling event {event.type} for printer {num}: {e}')

        try:
            await self._check_stale_camera(num)
            await self._check_periodic_camera(num)
            await self._check_low_filament(num)
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
            printer.stop_print()
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
