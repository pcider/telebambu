import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto

import bambulabs_api as bl
from bambulabs_api import GcodeState, PrintStatus, Printer

from settings import PrinterConfig
from . import files

# How long to wait for a fresh camera frame after turning the light on
CAMERA_POLL_INTERVAL = 2  # seconds
CAMERA_MAX_POLLS = 10
# Minimum gap between two pause notifications for the same printer
PAUSE_NOTIFY_COOLDOWN = 60  # seconds
# Upper bound on waiting for a printer restart, so a hang in bambulabs_api can't freeze the bot
RESTART_TIMEOUT = 60  # seconds
# States a printer can be in right before a new print starts running
PRE_PRINT_STATES = (GcodeState.FINISH, GcodeState.IDLE, GcodeState.PREPARE)
# How long to wait for a removed printer to disconnect before giving up on it
DISCONNECT_TIMEOUT = 10  # seconds
# How long uploading a print file over FTP may take
UPLOAD_TIMEOUT = 5 * 60  # seconds
# Bambu print speed profiles, as reported in spd_lvl and set with the print_speed command
SPEED_LEVELS = {1: 'silent', 2: 'standard', 3: 'sport', 4: 'ludicrous'}
# tray_now values that aren't an AMS slot
EXTERNAL_SPOOL_TRAY = 254
NO_TRAY = 255
# States where the printer is not in use
IDLE_STATES = (GcodeState.IDLE, GcodeState.FINISH, GcodeState.UNKNOWN)
# A short rising three-note chirp for /beep, in the M1006 buzzer format Bambu's start G-code uses
# (S1 starts the buzzer, each A..N line plays a note, W ends it). Not yet checked on a real
# printer; if it stays silent, some firmware accepts `M300 S<freq> P<ms>` instead.
BEEP_GCODE = [
    'M1006 S1',
    'M1006 A0 B10 L100 C37 D10 M60 E37 F10 N60',
    'M1006 A0 B10 L100 C41 D10 M60 E41 F10 N60',
    'M1006 A0 B10 L100 C44 D10 M60 E44 F10 N60',
    'M1006 W',
]


class _CameraRetryNoiseFilter(logging.Filter):
    """Drop the camera thread's network errors, which bambulabs_api logs on every
    5-second retry while a printer is offline. PrinterManager logs outages itself,
    once per outage; other library errors (e.g. a wrong access code) still get through."""
    NOISE_PREFIXES = ('Error occurred: [Errno', 'Error in socket:')

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return message != 'Error occurred: timed out' and not message.startswith(self.NOISE_PREFIXES)


logging.getLogger('bambulabs_api').addFilter(_CameraRetryNoiseFilter())


def to_int(value, default: int = 0) -> int:
    """Coerce a printer value (which may be None or 'Unknown') to an int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def run_blocking(fn, timeout: float):
    """Run a blocking call in a daemon thread and wait at most `timeout` seconds.

    Unlike asyncio.to_thread, a call that never returns doesn't tie up the default
    executor, which asyncio.run() waits on at exit, so shutdown can't hang on it.
    Raises TimeoutError if the call is still running when the timeout expires.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(result=None, exc=None):
        if future.done():  # we already gave up waiting
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def worker():
        try:
            result = fn()
        except BaseException as e:
            loop.call_soon_threadsafe(settle, None, e)
        else:
            loop.call_soon_threadsafe(settle, result)

    threading.Thread(target=worker, name=f'blocking-{fn.__name__}', daemon=True).start()
    return await asyncio.wait_for(future, timeout)


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
    FIRST_SEEN = auto()  # first state reported after the bot starts


@dataclass
class PrinterEvent:
    type: EventType
    printer_num: int
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


@dataclass
class PrinterDetails:
    """Extra information shown by /info for a single printer."""
    file_name: str
    nozzle_temp: float | None
    bed_temp: float | None
    chamber_temp: float | None
    speed_level: str
    speed_percent: int


@dataclass
class FilamentTray:
    """One filament source: an AMS slot or the external spool."""
    tray_id: int  # global id as used by tray_now: ams * 4 + slot, or 254 for the external spool
    label: str
    material: str
    color: str
    remain: int  # percent left, or -1 when the printer can't tell (e.g. non-Bambu spools)
    active: bool = False

    def describe(self) -> str:
        text = f"{self.label}: {self.material or 'empty'}"
        if self.color:
            text += f" {self.color}"
        if self.remain >= 0:
            text += f" ({self.remain}% left)"
        return text


def _tray(tray_id: int, label: str, raw: dict, active_id: int) -> FilamentTray:
    color = str(raw.get('tray_color') or '')
    return FilamentTray(
        tray_id=tray_id,
        label=label,
        material=str(raw.get('tray_type') or ''),
        color=f'#{color[:6]}' if len(color) >= 6 else '',
        remain=to_int(raw.get('remain'), -1),
        active=tray_id == active_id,
    )


def parse_filament(print_data: dict) -> list[FilamentTray]:
    """Filament trays from the raw MQTT ``print`` report (bambulabs_api drops the remaining %)."""
    ams = print_data.get('ams') or {}
    active_id = to_int(ams.get('tray_now'), NO_TRAY)
    trays = []
    for unit in ams.get('ams') or []:
        ams_id = to_int(unit.get('id'))
        for raw in unit.get('tray') or []:
            slot = to_int(raw.get('id'))
            if raw.get('tray_type'):
                trays.append(_tray(ams_id * 4 + slot, f'AMS{ams_id + 1} slot {slot + 1}', raw, active_id))
    external = print_data.get('vt_tray') or {}
    if external.get('tray_type'):
        trays.append(_tray(EXTERNAL_SPOOL_TRAY, 'External spool', external, active_id))
    return trays


@dataclass
class PrinterSlot:
    """A configured printer and the state tracked for it between polls."""
    ip: str
    access_code: str
    serial: str
    mac: str = ''
    printer: Printer | None = None
    prev_state: tuple[GcodeState, PrintStatus] = (GcodeState.UNKNOWN, PrintStatus.UNKNOWN)
    prev_layer: int = 0
    last_paused_time: float = 0.0
    connected_at: float = 0.0
    camera_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Objects of the current print, keyed by its file so a new print re-reads them
    objects_cache: tuple[str, list[files.PrintObject]] | None = None


class PrinterManager:
    """Printers are identified by their 1-based printer number everywhere."""

    def __init__(self, printer_configs: dict[int, PrinterConfig]):
        self.slots: dict[int, PrinterSlot] = {
            num: PrinterSlot(ip=c.ip, access_code=c.access_code, serial=c.serial, mac=c.mac)
            for num, c in printer_configs.items()
        }
        self._logged_disconnected: set[int] = set()

    def __len__(self) -> int:
        return len(self.slots)

    def __contains__(self, num: int) -> bool:
        return num in self.slots

    def numbers(self) -> list[int]:
        return sorted(self.slots)

    def numbers_text(self) -> str:
        """Human readable list of valid printer numbers, e.g. ``1-24``."""
        nums = self.numbers()
        if nums and nums == list(range(nums[0], nums[-1] + 1)):
            return f'{nums[0]}-{nums[-1]}' if len(nums) > 1 else str(nums[0])
        return ', '.join(map(str, nums))

    # --- Connection ---

    async def connect_all(self, log_fn):
        for num in self.numbers():
            await self._connect(num, log_fn)

    async def _connect(self, num: int, log_fn):
        slot = self.slots[num]
        await log_fn(f'Connecting to printer {num} at IP {slot.ip}')
        try:
            # Keep the Printer object even if the first connect fails, so
            # reconnect_if_needed() can keep retrying it.
            slot.printer = bl.Printer(slot.ip, slot.access_code, slot.serial)
            slot.printer.connect()
            slot.connected_at = time.time()
        except Exception as e:
            await log_fn(f'Failed to connect to printer {num}: {e}')

    async def reconnect_if_needed(self, log_fn):
        for num in self.numbers():
            slot = self.slots.get(num)
            printer = slot.printer if slot else None
            if not printer:
                continue
            if printer.mqtt_client_connected():
                if num in self._logged_disconnected:
                    self._logged_disconnected.discard(num)
                    await log_fn(f'Printer {num} is back online')
                continue

            # Only log once per outage; retries continue silently until it's back
            first_failure = num not in self._logged_disconnected
            if first_failure:
                self._logged_disconnected.add(num)
                await log_fn(f'Printer {num} ({slot.ip}) is unreachable, retrying quietly until it reconnects')
            try:
                printer.connect()
                slot.connected_at = time.time()
            except Exception as e:
                if first_failure:
                    print(f'Failed to reconnect printer {num}: {e}')

    async def add_printer(self, num: int, config: PrinterConfig, log_fn):
        self.slots[num] = PrinterSlot(ip=config.ip, access_code=config.access_code, serial=config.serial, mac=config.mac)
        await self._connect(num, log_fn)

    async def remove_printer(self, num: int):
        """Forget a printer and disconnect it in the background."""
        slot = self.slots.pop(num, None)
        self._logged_disconnected.discard(num)
        if slot and slot.printer:
            # disconnect() joins the camera thread, which can hang forever on an unreachable
            # printer; run_blocking abandons it after a while without holding up shutdown
            try:
                await run_blocking(slot.printer.disconnect, DISCONNECT_TIMEOUT)
            except TimeoutError:
                print(f'Printer {num} did not disconnect within {DISCONNECT_TIMEOUT}s, abandoning it')
            except Exception as e:
                print(f'Printer {num} did not disconnect cleanly: {e}')

    async def replace_printer(self, num: int, config: PrinterConfig, log_fn):
        """Reconnect a printer with new connection details."""
        await self.remove_printer(num)
        await self.add_printer(num, config, log_fn)

    def disconnect_all(self):
        for num, slot in self.slots.items():
            if slot.printer:
                try:
                    slot.printer.disconnect()
                except Exception as e:
                    print(f'Failed to disconnect printer {num}: {e}')

    async def restart(self, num: int):
        """Reboot a printer and re-establish the connection. Raises on failure.

        The library calls are blocking (stopping the camera joins its thread, which can
        hang), so they run off the event loop with a timeout and never freeze the bot.
        """
        printer = self.get_printer(num)
        if not printer:
            raise LookupError(f'Printer {num} not found.')

        def restart_printer():
            printer.reboot()
            # Bring MQTT back first, so status updates resume even if stopping the
            # camera (the step that can hang) never returns.
            printer.mqtt_stop()
            printer.mqtt_start()
            printer.camera_stop()
            printer.camera_start()

        try:
            await run_blocking(restart_printer, RESTART_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(f'still not done after {RESTART_TIMEOUT}s, '
                               'the camera thread is probably stuck') from None
        self.slots[num].connected_at = time.time()

    # --- Accessors ---

    def get_printer(self, num: int) -> Printer | None:
        slot = self.slots.get(num)
        return slot.printer if slot else None

    def seconds_since_connect(self, num: int) -> float:
        return time.time() - self.slots[num].connected_at

    def get_online_printer(self, num: int) -> Printer | None:
        """The printer if it's connected and has reported state, else None."""
        printer = self.get_printer(num)
        if printer and printer.mqtt_client_connected() and printer.mqtt_client_ready():
            return printer
        return None

    def snapshot(self, num: int) -> PrinterSnapshot | None:
        printer = self.get_online_printer(num)
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

    def file_name(self, num: int) -> str:
        """Name of the current (or last) print: the project name, falling back to the gcode file."""
        printer = self.get_online_printer(num)
        if not printer:
            return ''
        subtask, gcode_file = self.print_names(num)
        return subtask or gcode_file

    def print_names(self, num: int) -> tuple[str, str]:
        """The current print's project (subtask) name and gcode file name, either of which may be ''."""
        printer = self.get_online_printer(num)
        if not printer:
            return '', ''
        return (printer.subtask_name() or '').rsplit('/', 1)[-1], (printer.get_file_name() or '').rsplit('/', 1)[-1]

    def details(self, num: int) -> PrinterDetails | None:
        printer = self.get_online_printer(num)
        if not printer:
            return None
        level = to_int(printer.mqtt_dump().get('print', {}).get('spd_lvl'))
        return PrinterDetails(
            file_name=self.file_name(num),
            nozzle_temp=printer.get_nozzle_temperature(),
            bed_temp=printer.get_bed_temperature(),
            chamber_temp=printer.get_chamber_temperature(),
            speed_level=SPEED_LEVELS.get(level, 'unknown'),
            speed_percent=to_int(printer.get_print_speed()),
        )

    def set_speed(self, num: int, level: int) -> bool:
        """Switch the print speed profile (1 silent .. 4 ludicrous)."""
        printer = self.get_online_printer(num)
        if not printer:
            return False
        # Printer.set_print_speed() only accepts 0-3, but the printer's profiles are 1-4
        return bool(printer.mqtt_client.set_print_speed_lvl(level))

    async def print_objects(self, num: int) -> list[files.PrintObject]:
        """Objects on the plate being printed, read from the print's 3MF over FTP. Raises on failure."""
        printer = self.get_online_printer(num)
        if not printer:
            raise ConnectionError(f'Printer {num} is not connected.')
        slot = self.slots[num]
        gcode_file, subtask = printer.get_file_name() or '', printer.subtask_name() or ''
        key = f'{subtask}|{gcode_file}'
        if slot.objects_cache and slot.objects_cache[0] == key:
            return slot.objects_cache[1]

        candidates = files.print_file_candidates(gcode_file, subtask)
        _path, data = await asyncio.wait_for(
            asyncio.to_thread(files.download_first, slot.ip, slot.access_code, candidates), files.FTP_TIMEOUT * 2)
        objects = files.plate_objects(data, files.plate_from_gcode_file(gcode_file))
        slot.objects_cache = (key, objects)
        return objects

    async def upload_and_print(self, num: int, filename: str, data: bytes, plate: int,
                               use_ams: bool, mapping: list[int]):
        """Upload a sliced 3MF to the printer's SD card and start printing a plate. Raises on failure."""
        printer = self.get_online_printer(num)
        if not printer:
            raise ConnectionError(f'Printer {num} is not connected.')
        slot = self.slots[num]
        await asyncio.wait_for(asyncio.to_thread(files.upload, slot.ip, slot.access_code, f'/{filename}', data),
                               UPLOAD_TIMEOUT)
        if not printer.start_print(filename, plate, use_ams=use_ams, ams_mapping=mapping):
            raise RuntimeError('the printer did not accept the print command')

    def skipped_objects(self, num: int) -> list[int]:
        printer = self.get_online_printer(num)
        return list(printer.get_skipped_objects() or []) if printer else []

    def skip_objects(self, num: int, identify_ids: list[int]) -> bool:
        printer = self.get_online_printer(num)
        return bool(printer and printer.skip_objects(identify_ids))

    def filament(self, num: int) -> list[FilamentTray]:
        printer = self.get_online_printer(num)
        if not printer:
            return []
        return parse_filament(printer.mqtt_dump().get('print', {}))

    # --- State polling ---

    def poll(self, num: int) -> list[PrinterEvent]:
        """Compare a printer's state with the previous poll and return the resulting events."""
        printer = self.get_online_printer(num)
        if not printer:
            return []
        try:
            return list(self._diff_state(num, printer))
        except Exception as e:
            print(f'Error checking printer {num} state: {e}')
            return []

    def _diff_state(self, num: int, printer: Printer):
        slot = self.slots[num]
        prev_gcode_state, prev_print_state = slot.prev_state
        gcode_state = printer.get_state()
        print_state = printer.get_current_state()
        slot.prev_state = (gcode_state, print_state)

        def event(event_type: EventType, **data) -> PrinterEvent:
            return PrinterEvent(type=event_type, printer_num=num, printer=printer, data=data)

        # prev_state isn't reset on reconnect, so this fires once per slot: at startup, or when /printer adds or edits it
        if prev_gcode_state == GcodeState.UNKNOWN and gcode_state != GcodeState.UNKNOWN:
            yield event(EventType.FIRST_SEEN, state=gcode_state)

        # Check for gcode state changes
        if prev_gcode_state != GcodeState.UNKNOWN and prev_gcode_state != gcode_state:
            yield event(EventType.STATE_CHANGED, prev=prev_gcode_state, new=gcode_state)

            if gcode_state == GcodeState.FINISH:
                yield event(EventType.PRINT_FINISHED)

            elif gcode_state == GcodeState.FAILED:
                yield event(EventType.PRINT_FAILED, error_code=printer.print_error_code())

            elif prev_gcode_state == GcodeState.RUNNING and gcode_state == GcodeState.PAUSE:
                now = time.time()
                if now - slot.last_paused_time > PAUSE_NOTIFY_COOLDOWN:
                    slot.last_paused_time = now
                    yield event(EventType.PRINT_PAUSED, error_code=printer.print_error_code())

            elif prev_gcode_state in PRE_PRINT_STATES and gcode_state == GcodeState.RUNNING:
                slot.prev_layer = 0  # Reset layer tracking for new print
                yield event(EventType.PRINT_STARTED)

        # Check for layer changes
        if gcode_state == GcodeState.RUNNING:
            current_layer = to_int(printer.current_layer_num())
            prev_layer = slot.prev_layer
            if current_layer != prev_layer:
                slot.prev_layer = current_layer
                yield event(EventType.LAYER_CHANGED, prev_layer=prev_layer, layer=current_layer)

        # Check for print state changes
        if prev_print_state != PrintStatus.UNKNOWN and prev_print_state != print_state:
            yield event(EventType.STATE_CHANGED, prev_print=prev_print_state, new_print=print_state)

    def status_line(self, num: int) -> str:
        snap = self.snapshot(num)
        if not snap:
            return f'{num}: OFFLINE'

        line = f'{num}: {snap.state} ({snap.print_state}'
        if snap.state not in IDLE_STATES:
            line += f', {snap.progress}% done, {snap.time_left} left, L:{snap.layer}/{snap.total_layers}'
        line += ')'
        if not self.has_camera_frame(num):
            line += ' [NO CAM]'
        return line

    def get_status_lines(self) -> list[str]:
        return [self.status_line(num) for num in self.numbers()]

    def get_status_text(self) -> str:
        body = '\n'.join(self.get_status_lines())
        return (
            'Printer Statuses:```c\n'
            f'{body}\n'
            'Note: "FINISH/IDLE" means not in use\n'
            f'Updated on: {time.strftime("%Y-%m-%d %H:%M")}\n'
            '```\n'
        )

    def beep(self, num: int) -> bool:
        """Play a short tune on the printer's buzzer."""
        printer = self.get_online_printer(num)
        if not printer:
            return False
        # bambulabs_api's G-code check doesn't know M1006
        return bool(printer.gcode(BEEP_GCODE, gcode_check=False))

    # --- Light & camera ---

    def is_light_on(self, num: int) -> bool:
        printer = self.get_online_printer(num)
        return bool(printer) and printer.get_light_state() == 'on'

    def set_light(self, num: int, on: bool):
        printer = self.get_online_printer(num)
        if printer:
            printer.turn_light_on() if on else printer.turn_light_off()

    def has_camera_frame(self, num: int) -> bool:
        printer = self.get_online_printer(num)
        if not printer:
            return False
        # A capture in progress clears the frame on purpose; don't report that as stale.
        return self.slots[num].camera_lock.locked() or printer.camera_client.last_frame is not None

    async def get_camera_frame(self, num: int) -> bytes | None:
        """Turn on the light, capture a fresh frame, then restore the light."""
        printer = self.get_online_printer(num)
        if not printer:
            return None

        # Serialise captures per printer so concurrent requests don't clear each other's frame
        async with self.slots[num].camera_lock:
            light_was_on = self.is_light_on(num)
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
                        print(f'Failed to turn off printer {num} light: {e}')

        return bytes(frame) if frame else None
