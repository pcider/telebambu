"""Reading print files from a printer's SD card over FTPS and parsing Bambu 3MF metadata."""
import io
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from xml.etree import ElementTree

from bambulabs_api.ftp_client import ImplicitFTP_TLS

FTP_PORT = 990
FTP_USER = 'bblp'
FTP_TIMEOUT = 30  # seconds


@dataclass
class PrintObject:
    identify_id: int
    name: str


@contextmanager
def ftp_session(ip: str, access_code: str):
    """A logged-in FTPS connection to a printer.

    bambulabs_api's own FTP helpers share one connection per printer and swallow
    errors, so each operation here opens its own connection and lets errors raise.
    """
    ftp = ImplicitFTP_TLS(timeout=FTP_TIMEOUT)
    ftp.connect(host=ip, port=FTP_PORT)
    try:
        ftp.login(FTP_USER, access_code)
        ftp.prot_p()
        yield ftp
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def print_file_candidates(gcode_file: str, subtask_name: str) -> list[str]:
    """Where the current print's 3MF may be stored, most likely first."""
    candidates = []
    if gcode_file.lower().endswith('.3mf'):
        candidates.append(gcode_file if gcode_file.startswith('/') else f'/{gcode_file}')
    if subtask_name:
        for folder in ('/cache/', '/'):
            for ext in ('.gcode.3mf', '.3mf'):
                candidates.append(f'{folder}{subtask_name}{ext}')
    return list(dict.fromkeys(candidates))


def download_first(ip: str, access_code: str, paths: list[str]) -> tuple[str, bytes]:
    """Download the first of ``paths`` that exists. Raises FileNotFoundError if none do."""
    with ftp_session(ip, access_code) as ftp:
        for path in paths:
            buffer = io.BytesIO()
            try:
                ftp.retrbinary(f'RETR {path}', buffer.write)
            except Exception:
                continue
            return path, buffer.getvalue()
    raise FileNotFoundError(f"couldn't find the print file on the printer (tried {', '.join(paths) or 'nothing'})")


def plate_from_gcode_file(gcode_file: str) -> int:
    """Plate being printed, from a gcode path like ``/data/Metadata/plate_2.gcode``."""
    match = re.search(r'plate_(\d+)\.gcode', gcode_file or '')
    return int(match[1]) if match else 1


def _plate_elements(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        try:
            config = archive.read('Metadata/slice_info.config')
        except KeyError:
            raise ValueError('this 3MF has not been sliced (no Metadata/slice_info.config)') from None
    for plate in ElementTree.fromstring(config).iter('plate'):
        index = next((m.get('value') for m in plate.iter('metadata') if m.get('key') == 'index'), '1')
        yield int(index), plate


def plate_objects(data: bytes, plate_number: int) -> list[PrintObject]:
    """Objects on a sliced plate, with the ids the printer uses to skip them."""
    for index, plate in _plate_elements(data):
        if index == plate_number:
            return [PrintObject(int(obj.get('identify_id')), obj.get('name') or f"Object {obj.get('identify_id')}")
                    for obj in plate.iter('object') if obj.get('identify_id')]
    raise ValueError(f'plate {plate_number} is not in this file')


def upload(ip: str, access_code: str, path: str, data: bytes):
    """Upload a file to the printer's SD card. Raises on failure."""
    with ftp_session(ip, access_code) as ftp:
        ftp.storbinary(f'STOR {path}', io.BytesIO(data), blocksize=32768)


def sliced_plates(data: bytes) -> list[int]:
    """Plate numbers that have sliced gcode in a 3MF. Raises ValueError if it isn't a 3MF."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        raise ValueError('this is not a valid 3MF file') from None
    return sorted(int(m[1]) for name in names if (m := re.fullmatch(r'Metadata/plate_(\d+)\.gcode', name)))


def ams_mapping(data: bytes, plate_number: int) -> list[int]:
    """AMS tray for each of the project's filaments, assuming filament N is loaded in tray N.

    The printer expects one entry per project filament; filaments the plate
    doesn't use are -1.
    """
    for index, plate in _plate_elements(data):
        if index == plate_number:
            used = {int(f.get('id')) for f in plate.iter('filament') if f.get('id')}
            return [n - 1 if n in used else -1 for n in range(1, max(used, default=1) + 1)]
    raise ValueError(f'plate {plate_number} is not in this file')
