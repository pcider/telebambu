import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto

import bambulabs_api as bl
from bambulabs_api import GcodeState, PrintStatus, Printer

# How long to wait for a fresh camera frame after turning the light on
CAMERA_POLL_INTERVAL = 2  # seconds
CAMERA_MAX_POLLS = 10
# Minimum gap between two pause notifications for the same printer
PAUSE_NOTIFY_COOLDOWN = 60  # seconds
# States a printer can be in right before a new print starts running
PRE_PRINT_STATES = (GcodeState.FINISH, GcodeState.IDLE, GcodeState.PREPARE)
# States where the printer is not in use
IDLE_STATES = (GcodeState.IDLE, GcodeState.FINISH, GcodeState.UNKNOWN)


class _CameraRetryNoiseFilter(logging.Filter):
    """Drop the camera thread's network errors, which bambulabs_api logs on every
    5-second retry while a printer is offline. PrinterManager logs outages itself,
    once per outage; other library errors (e.g. a wrong access code) still get through."""
    NOISE_PREFIXES = ('Error occurred: [Errno', 'Error in socket:')

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(self.NOISE_PREFIXES)


logging.getLogger('bambulabs_api').addFilter(_CameraRetryNoiseFilter())


def to_int(value, default: int = 0) -> int:
    """Coerce a printer value (which may be None or 'Unknown') to an int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_duration(total_mins) -> str:
    total_mins = to_int(total_mins)
    hrs, mins = divmod(total_mins, 60)
    return f'{hrs}h{mins}m' if hrs > 0 else f'{mins}m'


class EventType(Enum):
    PRINT_STARTED = auto()
    PRINT_FINISHED = auto()
    PRINT_FAILED = auto()
    PRINT_PAUSED = auto()
    LAYER_CHANGED = auto()
    STATE_CHANGED = auto()


@dataclass
class PrinterEvent:
    type: EventType
    printer_index: int
    printer: Printer
    data: dict = field(default_factory=dict)


@dataclass
class PrinterSnapshot:
    """Point-in-time view of a printer's progress."""
    state: GcodeState
    print_state: PrintStatus
    progress: int
    time_left: str
    layer: int
    total_layers: int


class PrinterManager:
    def __init__(self, printer_configs: list):
        count = len(printer_configs)
        self.printer_configs = printer_configs
        self.printers: list[Printer | None] = [None] * count
        self.prev_states: list[tuple[GcodeState, PrintStatus]] = [(GcodeState.UNKNOWN, PrintStatus.UNKNOWN)] * count
        self.prev_layers: list[int] = [0] * count
        self.last_paused_time: list[float] = [0.0] * count
        self._camera_locks = [asyncio.Lock() for _ in range(count)]
        self._logged_disconnected: set[int] = set()

    def __len__(self) -> int:
        return len(self.printer_configs)

    # --- Connection ---

    async def connect_all(self, log_fn):
        for i, (name, mac, ip, access_code, serial) in enumerate(self.printer_configs):
            await log_fn(f'Connecting to printer {i + 1} at IP {ip}')
            try:
                # Keep the Printer object even if the first connect fails, so
                # reconnect_if_needed() can keep retrying it.
                self.printers[i] = bl.Printer(ip, access_code, serial)
                self.printers[i].connect()
            except Exception as e:
                await log_fn(f'Failed to connect to printer {i + 1}: {e}')

    async def reconnect_if_needed(self, log_fn):
        for i, printer in enumerate(self.printers):
            if not printer:
                continue
            if printer.mqtt_client_connected():
                if i in self._logged_disconnected:
                    self._logged_disconnected.discard(i)
                    await log_fn(f'Printer {i + 1} is back online')
                continue

            # Only log once per outage; retries continue silently until it's back
            first_failure = i not in self._logged_disconnected
            if first_failure:
                self._logged_disconnected.add(i)
                ip = self.printer_configs[i][2]
                await log_fn(f'Printer {i + 1} ({ip}) is unreachable, retrying quietly until it reconnects')
            try:
                printer.connect()
            except Exception as e:
                if first_failure:
                    print(f'Failed to reconnect printer {i + 1}: {e}')

    def disconnect_all(self):
        for i, printer in enumerate(self.printers):
            if printer:
                try:
                    printer.disconnect()
                except Exception as e:
                    print(f'Failed to disconnect printer {i + 1}: {e}')

    def restart(self, index: int):
        """Reboot a printer and re-establish the connection. Raises on failure."""
        printer = self.get_printer(index)
        if not printer:
            raise LookupError(f'Printer {index + 1} not found.')
        printer.reboot()
        printer.disconnect()
        printer.connect()

    # --- Accessors ---

    def get_printer(self, index: int) -> Printer | None:
        if 0 <= index < len(self.printers):
            return self.printers[index]
        return None

    def get_online_printer(self, index: int) -> Printer | None:
        """The printer if it's connected and has reported state, else None."""
        printer = self.get_printer(index)
        if printer and printer.mqtt_client_connected() and printer.mqtt_client_ready():
            return printer
        return None

    def snapshot(self, index: int) -> PrinterSnapshot | None:
        printer = self.get_online_printer(index)
        if not printer:
            return None
        return PrinterSnapshot(
            state=printer.get_state(),
            print_state=printer.get_current_state(),
            progress=to_int(printer.get_percentage()),
            time_left=format_duration(printer.get_time()),
            layer=to_int(printer.current_layer_num()),
            total_layers=to_int(printer.total_layer_num()),
        )

    # --- State polling ---

    def poll(self, index: int) -> list[PrinterEvent]:
        """Compare a printer's state with the previous poll and return the resulting events."""
        printer = self.get_online_printer(index)
        if not printer:
            return []
        try:
            return list(self._diff_state(index, printer))
        except Exception as e:
            print(f'Error checking printer {index + 1} state: {e}')
            return []

    def _diff_state(self, i: int, printer: Printer):
        prev_gcode_state, prev_print_state = self.prev_states[i]
        gcode_state = printer.get_state()
        print_state = printer.get_current_state()
        self.prev_states[i] = (gcode_state, print_state)

        def event(event_type: EventType, **data) -> PrinterEvent:
            return PrinterEvent(type=event_type, printer_index=i, printer=printer, data=data)

        # Check for gcode state changes
        if prev_gcode_state != GcodeState.UNKNOWN and prev_gcode_state != gcode_state:
            yield event(EventType.STATE_CHANGED, prev=prev_gcode_state, new=gcode_state)

            if gcode_state == GcodeState.FINISH:
                yield event(EventType.PRINT_FINISHED)

            elif gcode_state == GcodeState.FAILED:
                yield event(EventType.PRINT_FAILED, error_code=printer.print_error_code())

            elif prev_gcode_state == GcodeState.RUNNING and gcode_state == GcodeState.PAUSE:
                now = time.time()
                if now - self.last_paused_time[i] > PAUSE_NOTIFY_COOLDOWN:
                    self.last_paused_time[i] = now
                    yield event(EventType.PRINT_PAUSED, error_code=printer.print_error_code())

            elif prev_gcode_state in PRE_PRINT_STATES and gcode_state == GcodeState.RUNNING:
                self.prev_layers[i] = 0  # Reset layer tracking for new print
                yield event(EventType.PRINT_STARTED)

        # Check for layer changes
        if gcode_state == GcodeState.RUNNING:
            current_layer = to_int(printer.current_layer_num())
            prev_layer = self.prev_layers[i]
            if current_layer != prev_layer:
                self.prev_layers[i] = current_layer
                yield event(EventType.LAYER_CHANGED, prev_layer=prev_layer, layer=current_layer)

        # Check for print state changes
        if prev_print_state != PrintStatus.UNKNOWN and prev_print_state != print_state:
            yield event(EventType.STATE_CHANGED, prev_print=prev_print_state, new_print=print_state)

    def get_status_text(self) -> str:
        lines = []
        for i in range(len(self)):
            snap = self.snapshot(i)
            if not snap:
                lines.append(f'{i + 1}: OFFLINE')
                continue

            line = f'{i + 1}: {snap.state} ({snap.print_state}'
            if snap.state not in IDLE_STATES:
                line += f', {snap.progress}% done, {snap.time_left} left, L:{snap.layer}/{snap.total_layers}'
            line += ')'
            if not self.has_camera_frame(i):
                line += ' [NO CAM]'
            lines.append(line)

        body = '\n'.join(lines)
        return (
            'Printer Statuses:```c\n'
            f'{body}\n'
            'Note: "FINISH/IDLE" means not in use\n'
            f'Updated on: {time.strftime("%Y-%m-%d %H:%M")}\n'
            '```\n'
        )

    # --- Light & camera ---

    def is_light_on(self, index: int) -> bool:
        printer = self.get_online_printer(index)
        return bool(printer) and printer.get_light_state() == 'on'

    def set_light(self, index: int, on: bool):
        printer = self.get_online_printer(index)
        if printer:
            printer.turn_light_on() if on else printer.turn_light_off()

    def has_camera_frame(self, index: int) -> bool:
        printer = self.get_online_printer(index)
        if not printer:
            return False
        # A capture in progress clears the frame on purpose; don't report that as stale.
        return self._camera_locks[index].locked() or printer.camera_client.last_frame is not None

    async def get_camera_frame(self, index: int) -> bytes | None:
        """Turn on the light, capture a fresh frame, then restore the light."""
        printer = self.get_online_printer(index)
        if not printer:
            return None

        # Serialise captures per printer so concurrent requests don't clear each other's frame
        async with self._camera_locks[index]:
            light_was_on = self.is_light_on(index)
            if not light_was_on:
                printer.turn_light_on()
            printer.camera_client.last_frame = None
            try:
                for _ in range(CAMERA_MAX_POLLS):
                    await asyncio.sleep(CAMERA_POLL_INTERVAL)
                    if printer.camera_client.last_frame:
                        break
                frame = printer.camera_client.last_frame
            finally:
                if not light_was_on:
                    try:
                        printer.turn_light_off()
                    except Exception as e:
                        print(f'Failed to turn off printer {index + 1} light: {e}')

        return bytes(frame) if frame else None
