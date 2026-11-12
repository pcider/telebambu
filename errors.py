"""Bambu printer error codes (from error_codes.json) and helpers to interpret them."""
import json
import os

_ERROR_CODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'error_codes.json')
with open(_ERROR_CODES_FILE, 'r') as _f:
    ERROR_CODES: dict[str, str] = json.load(_f)


def lookup_error(code) -> str:
    """Look up an error code and return a human-readable description."""
    if code is None:
        return "Unknown error"
    hex_code = f"{code:08X}" if isinstance(code, int) else str(code).replace("-", "").replace(" ", "")
    return ERROR_CODES.get(hex_code, f"Unknown error (code: {hex_code})")


def is_filament_runout(description: str) -> bool:
    """Whether an error description is one of the "filament ran out" errors."""
    text = description.lower()
    return 'ran out' in text or 'run out' in text
