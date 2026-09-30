from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from bambulabs_api import GcodeState

from .manager import PrinterManager, PrinterEvent, EventType, format_duration, to_int

if TYPE_CHECKING:
    from bot.messages import MessageService

DEFAULT_UPDATE_INTERVAL = 5  # seconds
PRINT_STARTED_DELAY = 2  # seconds to let the printer update its time estimate
# Don't flag a missing camera frame until this long after a printer connects/restarts,
# since the camera stream can take a while to establish (TLS handshake, auth, retries)
CAMERA_STARTUP_GRACE = 30  # seconds
# Only auto-restart printers in these states, so a running print is never interrupted
NOT_PRINTING_STATES = (GcodeState.IDLE, GcodeState.FINISH)


class PrinterMonitor:
    def __init__(self, printer_manager: PrinterManager, message_service: MessageService, interval: float = DEFAULT_UPDATE_INTERVAL,
                 auto_restart: bool = True):
        self.pm = printer_manager
        self.ms = message_service
        self.storage = message_service.storage
        self.interval = interval
        self.auto_restart = auto_restart
        # Printers already reported as having a stale camera, to avoid spam
        self._stale_camera_reported: set[int] = set()

    async def run(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.tick()
            except Exception as e:
                print(f'Monitor tick failed: {e}')

    async def tick(self):
        await self.pm.reconnect_if_needed(self.ms.log_message)

        try:
            await self.ms.update_status_message(self.pm.get_status_text())
        except Exception as e:
            print(f'Failed to update status message: {e}')

        # Printers are independent, so a slow camera capture on one doesn't hold up the rest
        await asyncio.gather(*(self._process_printer(i) for i in range(len(self.pm))))

        await self.ms.flush_logs()

    async def _process_printer(self, i: int):
        try:
            await self._check_stale_camera(i)
            await self._check_periodic_camera(i)
        except Exception as e:
            print(f'Error running checks for printer {i + 1}: {e}')

        for event in self.pm.poll(i):
            try:
                await self._handle_event(event)
            except Exception as e:
                print(f'Error handling event {event.type} for printer {i + 1}: {e}')

    async def _check_stale_camera(self, i: int):
        """Auto-restart an idle/finished printer whose camera has stopped sending frames (once per outage)."""
        printer = self.pm.get_online_printer(i)
        if not printer:
            return
        if self.pm.seconds_since_connect(i) < CAMERA_STARTUP_GRACE:
            return

        has_frame = self.pm.has_camera_frame(i)
        state = printer.get_state()
        if not has_frame and state in NOT_PRINTING_STATES:
            if i not in self._stale_camera_reported:
                self._stale_camera_reported.add(i)
                if not self.auto_restart:
                    await self.ms.log_message(f"Printer {i + 1} has no camera ({state}). Auto-restart disabled, skipping.")
                    return
                await self.ms.log_message(f"Printer {i + 1} has no camera ({state}). Auto-restarting...")
                try:
                    self.pm.restart(i)
                except Exception as e:
                    await self.ms.log_message(f"Failed to auto-restart Printer {i + 1}: {e}")
        elif has_frame:
            # Camera recovered, clear the flag
            self._stale_camera_reported.discard(i)

    async def _check_periodic_camera(self, i: int):
        """Send a recurring camera snapshot when the claimer's configured trigger is due."""
        session = self.storage.get_print(i)
        if not session or not session.claimed_by or not session.notify_every_type:
            return
        snap = self.pm.snapshot(i)
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
            frame = await self.pm.get_camera_frame(i)
            await self.ms.send_periodic_camera_notification(i, snap, trigger_value, frame)

    async def _handle_event(self, event: PrinterEvent):
        printer = event.printer
        i = event.printer_index

        if event.type == EventType.STATE_CHANGED:
            # Log state changes to stdout only (not to Telegram)
            if 'new' in event.data:
                await self.ms.log_message(
                    f'Printer {i + 1} GCODE state: {event.data["prev"]} -> {event.data["new"]}', stdout_only=True)
            else:
                await self.ms.log_message(
                    f'Printer {i + 1} PRINT state: {event.data["prev_print"]} -> {event.data["new_print"]}', stdout_only=True)

        elif event.type == EventType.PRINT_STARTED:
            await asyncio.sleep(PRINT_STARTED_DELAY)
            await self.ms.send_print_started(i, format_duration(printer.get_time()), to_int(printer.total_layer_num()))

        elif event.type == EventType.PRINT_FINISHED:
            await self.ms.send_print_finished(i, await self.pm.get_camera_frame(i))

        elif event.type == EventType.PRINT_FAILED:
            frame = await self.pm.get_camera_frame(i)
            await self.ms.send_print_failed(i, event.data.get('error_code'), frame)

        elif event.type == EventType.PRINT_PAUSED:
            frame = await self.pm.get_camera_frame(i)
            await self.ms.log_message(f'Printer {i + 1} has paused printing. (code: {event.data.get("error_code")})', frame)

        elif event.type == EventType.LAYER_CHANGED:
            await self._handle_layer_change(i, event.data['prev_layer'], event.data['layer'])

    async def _handle_layer_change(self, i: int, prev_layer: int, layer: int):
        session = self.storage.get_print(i)
        if not session or not session.claimed_by:
            return

        # Use crossings rather than exact matches so a missed poll doesn't skip a notification
        layer2_due = session.layer2_notify and not session.layer2_notified and prev_layer < 2 <= layer
        custom_due = bool(session.notify_layer) and not session.notify_layer_notified and layer >= session.notify_layer
        if not (layer2_due or custom_due):
            return

        # One capture serves both notifications
        frame = await self.pm.get_camera_frame(i)
        if layer2_due:
            await self.ms.send_layer2_notification(i, frame)
        if custom_due:
            await self.ms.send_custom_layer_notification(i, frame)
