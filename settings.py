"""Bot configuration, stored in config.yml.

The bot rewrites config.yml when the owner changes a setting with /config (and
when admins manage printers), so comments added by hand are not kept.
"""
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

import yaml

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yml')
LEGACY_CONFIG_MODULE = 'config'

HEADER = """\
# Telebambu configuration. The bot rewrites this file when settings change
# through /config or /printer, so comments added here are not kept.
#
# Chat targets are 'chat_id' or 'chat_id/thread_id' for forum topics.
# log_chat_id: null disables the log chat. stats_log_interval: null disables
# the periodic update timing report. info_reply_ttl: seconds before replies to
# informational commands in groups are deleted, or null to keep them. owner_id seeds the owner role on first
# run; after that roles live in data.json and change through /staff.
"""


# Commands that config.yml can turn off one by one. A disabled /staff also leaves the owner
# as the only role: admins and mods act as users until it is turned back on.
DISABLEABLE_COMMANDS = ('print', 'speed', 'skip', 'staff', 'forcereclaim', 'restart', 'restartall', 'home')


@dataclass
class PrinterConfig:
    ip: str
    access_code: str
    serial: str
    mac: str = ''


@dataclass
class Settings:
    telegram_bot_token: str
    chat_id: str
    status_chat_id: str
    owner_id: Optional[int] = None
    log_chat_id: Optional[str] = None
    update_interval: float = 5
    stats_log_interval: Optional[float] = 60 * 60
    auto_restart_printers: bool = True
    # Seconds before group replies to informational commands (and the commands) are deleted; None keeps them
    info_reply_ttl: Optional[float] = 60
    # One flag per command in DISABLEABLE_COMMANDS; true turns that command off
    disabled_commands: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(DISABLEABLE_COMMANDS, False))
    printers: dict[int, PrinterConfig] = field(default_factory=dict)
    path: str = field(default=CONFIG_FILE, repr=False, compare=False)

    def is_disabled(self, command: str) -> bool:
        return bool(self.disabled_commands.get(command))

    def disabled(self) -> frozenset[str]:
        return frozenset(command for command in DISABLEABLE_COMMANDS if self.is_disabled(command))

    def save(self):
        data = asdict(self)
        del data['path']
        data['printers'] = {num: asdict(p) for num, p in sorted(self.printers.items())}
        tmp_file = self.path + '.tmp'
        with open(tmp_file, 'w') as f:
            f.write(HEADER + '\n')
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp_file, self.path)


# Settings that may be left out of config.yml (or config.py, in upper case) to use the default
OPTIONAL_KEYS = ('update_interval', 'stats_log_interval', 'auto_restart_printers', 'info_reply_ttl')


def _chat(value) -> Optional[str]:
    return None if value is None else str(value)


def _disabled_commands(data: dict) -> dict[str, bool]:
    """Every disableable command's flag, defaulting to enabled when config.yml leaves it out."""
    given = data.get('disabled_commands') or {}
    unknown = sorted(set(given) - set(DISABLEABLE_COMMANDS))
    if unknown:
        print(f"Ignoring unknown disabled_commands in config: {', '.join(map(str, unknown))}")
    return {command: bool(given.get(command, False)) for command in DISABLEABLE_COMMANDS}


def _from_dict(data: dict, path: str) -> Settings:
    printers = {
        int(num): PrinterConfig(ip=str(p['ip']), access_code=str(p['access_code']),
                                serial=str(p['serial']), mac=str(p.get('mac') or ''))
        for num, p in (data.get('printers') or {}).items()
    }
    owner_id = data.get('owner_id')
    return Settings(
        telegram_bot_token=str(data['telegram_bot_token']),
        chat_id=_chat(data['chat_id']),
        status_chat_id=_chat(data['status_chat_id']),
        owner_id=int(owner_id) if owner_id else None,
        log_chat_id=_chat(data.get('log_chat_id')),
        disabled_commands=_disabled_commands(data),
        printers=printers,
        path=path,
        # Only keys that are set, so missing ones fall back to the Settings defaults
        **{key: data[key] for key in OPTIONAL_KEYS if key in data},
    )


def _from_legacy_module(module, path: str) -> Settings:
    """Build settings from the old config.py, where the Nth PRINTERS entry is Printer N."""
    printers = {
        num: PrinterConfig(ip=ip, access_code=str(code), serial=serial, mac=mac)
        for num, (_name, mac, ip, code, serial) in enumerate(module.PRINTERS, start=1)
    }
    return Settings(
        telegram_bot_token=module.TELEGRAM_BOT_TOKEN,
        chat_id=_chat(module.CHAT_ID),
        status_chat_id=_chat(module.STATUS_CHAT_ID),
        owner_id=getattr(module, 'OWNER_ID', None),
        log_chat_id=_chat(getattr(module, 'LOG_CHAT_ID', None)),
        printers=printers,
        path=path,
        **{key: getattr(module, key.upper()) for key in OPTIONAL_KEYS if hasattr(module, key.upper())},
    )


def load(path: str = CONFIG_FILE) -> Settings:
    """Load config.yml, creating it from a legacy config.py on first run after upgrading."""
    if os.path.exists(path):
        with open(path) as f:
            return _from_dict(yaml.safe_load(f) or {}, path)

    try:
        import importlib
        legacy = importlib.import_module(LEGACY_CONFIG_MODULE)
    except ImportError:
        raise SystemExit(f'No config found. Copy config_example.yml to {os.path.basename(path)} and edit it.')

    settings = _from_legacy_module(legacy, path)
    settings.save()
    print(f'Migrated config.py to {path}. config.py is no longer used and can be deleted.')
    return settings
