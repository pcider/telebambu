import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Generator

import bambulabs_api as bl
from bambulabs_api import GcodeState, PrintStatus, Printer


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
    data: dict = None


class PrinterManager:
    def __init__(self, printer_configs: list):
        self.printer_configs = printer_configs
        self.printers: list[Printer | None] = [None] * len(printer_configs)
        self.prev_states: list[tuple[GcodeState, PrintStatus]] = [
            (GcodeState.UNKNOWN, PrintStatus.UNKNOWN)
        ] * len(printer_configs)
        self.prev_layers: list[int] = [0] * len(printer_configs)
        self.last_paused_time: list[float] = [0.0] * len(printer_configs)

    async def connect_all(self, log_fn=None):
        for i, config in enumerate(self.printer_configs):
            name, mac, ip, access_code, serial = config
            msg = f'Connecting to printer {i + 1} at IP {ip}'
            print(msg)
            if log_fn:
                await log_fn(msg)

            try:
                p = bl.Printer(ip, access_code, serial)
                p.connect()
                self.printers[i] = p
            except Exception as e:
                err_msg = f'Failed to connect to printer {i + 1}: {e}'
                print(err_msg)
                if log_fn:
                    await log_fn(err_msg)

    async def reconnect_if_needed(self, log_fn=None):
        for i, printer in enumerate(self.printers):
            if printer and not printer.mqtt_client_connected():
                msg = f'Printer {i + 1} not connected, reconnecting'
                print(msg)
                if log_fn:
                    await log_fn(msg)
                try:
                    printer.connect()
                except Exception as e:
                    print(f'Failed to reconnect printer {i + 1}: {e}')

    def check_states(self) -> Generator[PrinterEvent, None, None]:
        for i, printer in enumerate(self.printers):
            if not printer or not printer.mqtt_client_ready():
                continue

            try:
                yield from self._check_printer_state(i, printer)
            except Exception as e:
                print(f'Error checking printer {i + 1} state: {e}')

    def _check_printer_state(self, i: int, printer: Printer) -> Generator[PrinterEvent, None, None]:
        prev_gcode_state, prev_print_state = self.prev_states[i]
        gcode_state = printer.get_state()
        print_state = printer.get_current_state()
        self.prev_states[i] = (gcode_state, print_state)

        # Check for gcode state changes
        if prev_gcode_state != GcodeState.UNKNOWN and prev_gcode_state != gcode_state:
            yield PrinterEvent(
                type=EventType.STATE_CHANGED,
                printer_index=i,
                printer=printer,
                data={'prev': prev_gcode_state, 'new': gcode_state}
            )

            if gcode_state == GcodeState.FINISH:
                yield PrinterEvent(
                    type=EventType.PRINT_FINISHED,
                    printer_index=i,
                    printer=printer
                )

            elif gcode_state == GcodeState.FAILED:
                yield PrinterEvent(
                    type=EventType.PRINT_FAILED,
                    printer_index=i,
                    printer=printer,
                    data={'error_code': printer.print_error_code()}
                )

            elif prev_gcode_state == GcodeState.RUNNING and gcode_state == GcodeState.PAUSE:
                now = time.time()
                if now - self.last_paused_time[i] > 60:
                    yield PrinterEvent(
                        type=EventType.PRINT_PAUSED,
                        printer_index=i,
                        printer=printer,
                        data={'error_code': printer.print_error_code()}
                    )
                    self.last_paused_time[i] = now

            elif prev_gcode_state in (GcodeState.FINISH, GcodeState.IDLE, GcodeState.PREPARE) and gcode_state == GcodeState.RUNNING:
                self.prev_layers[i] = 0  # Reset layer tracking for new print
                yield PrinterEvent(
                    type=EventType.PRINT_STARTED,
                    printer_index=i,
                    printer=printer,
                    data={'print_time': printer.get_time()}
                )

        # Check for layer changes
        if gcode_state == GcodeState.RUNNING:
            current_layer = printer.current_layer_num()
            if current_layer != self.prev_layers[i]:
                prev_layer = self.prev_layers[i]
                self.prev_layers[i] = current_layer
                yield PrinterEvent(
                    type=EventType.LAYER_CHANGED,
                    printer_index=i,
                    printer=printer,
                    data={'prev_layer': prev_layer, 'layer': current_layer}
                )

        # Check for print state changes
        if prev_print_state != PrintStatus.UNKNOWN and prev_print_state != print_state:
            yield PrinterEvent(
                type=EventType.STATE_CHANGED,
                printer_index=i,
                printer=printer,
                data={'prev_print': prev_print_state, 'new_print': print_state}
            )

    def get_status_text(self) -> str:
        status_message = 'Printer Statuses:```c\n'

        for i, printer in enumerate(self.printers):
            if not printer or not printer.mqtt_client_ready():
                continue

            gcode_state = printer.get_state()
            print_state = printer.get_current_state()

            status_message += f'{i + 1}: {gcode_state} ({print_state}'

            if gcode_state not in (GcodeState.IDLE, GcodeState.FINISH, GcodeState.UNKNOWN):
                progress = printer.get_percentage()
                time_left = self._format_print_time(printer.get_time())
                layer = printer.current_layer_num()
                total_layers = printer.total_layer_num()
                status_message += f', {progress}% done, {time_left} left, L:{layer}/{total_layers}'

            status_message += ')\n'

        status_message += 'Note: "FINISH/IDLE" means not in use\n'
        status_message += f'Updated on: {time.strftime("%Y-%m-%d %H:%M")}\n'
        status_message += '```\n'

        return status_message

    def _format_print_time(self, total_mins: int) -> str:
        hrs = total_mins // 60
        mins = total_mins % 60
        return f'{hrs}h{mins}m' if hrs > 0 else f'{mins}m'

    def get_printer(self, index: int) -> Printer | None:
        if 0 <= index < len(self.printers):
            return self.printers[index]
        return None

    def get_camera_frame(self, index: int) -> bytes | None:
        printer = self.get_printer(index)
        if not printer or not printer.mqtt_client_ready():
            return None
        frame = printer.camera_client.last_frame
        if isinstance(frame, bytearray):
            return bytes(frame)
        return frame

    def disconnect_all(self):
        for i, printer in enumerate(self.printers):
            if printer:
                try:
                    printer.disconnect()
                except Exception as e:
                    print(f'Failed to disconnect printer {i + 1}: {e}')
