import json
import os
import time
from dataclasses import dataclass, asdict, field, fields
from typing import Optional

from .roles import Role

DATA_FILE = os.path.join(os.path.dirname(__file__), '..', 'data.json')
# Bumped when data.json's layout changes; older files are migrated on load
DATA_VERSION = 2


@dataclass
class PrintSession:
    message_id: int
    chat_id: str
    printer_num: int
    claimed_by: Optional[int] = None
    claimed_username: Optional[str] = None
    dm_preference: str = "chat"  # "chat" or "dm"
    layer2_notify: bool = True
    layer2_notified: bool = False
    print_time: Optional[str] = None
    started_at: Optional[float] = None  # when the bot saw the print start, for its duration in the log chat
    file_name: Optional[str] = None
    # Both names the printer reports, so a restart can tell whether it is still the same print
    subtask_name: Optional[str] = None
    gcode_file: Optional[str] = None
    total_layers: int = 0
    notify_layer: Optional[int] = None
    notify_layer_notified: bool = False
    notify_type: Optional[str] = None  # "layer" or "percent"
    notify_original_value: Optional[int] = None  # original value for display
    notify_every_type: Optional[str] = None  # "layers", "percent", or "time"
    notify_every_value: Optional[int] = None
    notify_every_last_value: Optional[int] = None
    notify_every_last_sent_at: Optional[float] = None
    watchers: list[int] = field(default_factory=list)  # extra users notified alongside the claimer
    stop_at_layer: Optional[int] = None  # stop the print once it reaches this layer
    stopped_at_layer: Optional[int] = None  # set when the bot stopped the print on request
    low_filament_alerted: list[int] = field(default_factory=list)  # tray ids already reported as low
    catchup: bool = False  # announced at startup rather than when it started


@dataclass
class KnownUser:
    """A Telegram user the bot has seen, so commands can refer to them by @username."""
    username: Optional[str] = None
    name: Optional[str] = None


@dataclass
class UserPreferences:
    default_dm_preference: str = "chat"
    layer2_notify: bool = True


def _from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring keys it doesn't know about."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _migrate_to_1_based(data: dict):
    """Version 1 keyed prints by 0-based printer index; renumber them to 1-based printer numbers."""
    prints = {}
    for idx, session in data.get('active_prints', {}).items():
        num = int(idx) + 1
        session.pop('printer_index', None)
        session['printer_num'] = num
        prints[str(num)] = session
    data['active_prints'] = prints


class Storage:
    def __init__(self):
        self.active_prints: dict[int, PrintSession] = {}  # printer number (1-based) -> PrintSession
        self.user_preferences: dict[int, UserPreferences] = {}  # user_id -> UserPreferences
        self.status_message_id: Optional[int] = None
        self.roles: dict[int, Role] = {}  # user_id -> Role, only staff are stored
        self.users: dict[int, KnownUser] = {}  # user_id -> last seen username/name
        # False while /staff is disabled: only the owner keeps a role. Stored roles are kept,
        # so admins and mods get theirs back when it is enabled again.
        self.roles_enabled = True
        self._load()

    def _load(self):
        if not os.path.exists(DATA_FILE):
            return
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)

            if data.get('version', 1) < 2:
                _migrate_to_1_based(data)

            for num, session_data in data.get('active_prints', {}).items():
                self.active_prints[int(num)] = _from_dict(PrintSession, session_data)

            for user_id, prefs_data in data.get('user_preferences', {}).items():
                self.user_preferences[int(user_id)] = _from_dict(UserPreferences, prefs_data)

            self.status_message_id = data.get('status_message_id')

            for user_id, role in data.get('roles', {}).items():
                self.roles[int(user_id)] = Role[role]

            for user_id, user_data in data.get('users', {}).items():
                self.users[int(user_id)] = _from_dict(KnownUser, user_data)
        except Exception as e:
            print(f'Failed to load data: {e}')

    def _save(self):
        data = {
            'version': DATA_VERSION,
            'active_prints': {str(num): asdict(s) for num, s in self.active_prints.items()},
            'user_preferences': {str(uid): asdict(p) for uid, p in self.user_preferences.items()},
            'status_message_id': self.status_message_id,
            'roles': {str(uid): role.name for uid, role in self.roles.items()},
            'users': {str(uid): asdict(u) for uid, u in self.users.items()},
        }
        # Write to a temp file then rename, so a crash mid-write can't corrupt data.json
        tmp_file = DATA_FILE + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_file, DATA_FILE)

    def _update(self, printer_num: int, **changes) -> Optional[PrintSession]:
        """Apply field changes to an active session and persist. No-op if there is no session."""
        session = self.active_prints.get(printer_num)
        if session:
            for key, value in changes.items():
                setattr(session, key, value)
            self._save()
        return session

    def _user_prefs(self, user_id: int) -> UserPreferences:
        return self.user_preferences.setdefault(user_id, UserPreferences())

    # --- Session lifecycle ---

    def start_print(self, printer_num: int, message_id: int, chat_id: str,
                    print_time: str = None, total_layers: int = 0, file_name: str = None,
                    catchup: bool = False, subtask_name: str = None, gcode_file: str = None) -> PrintSession:
        session = PrintSession(
            message_id=message_id,
            chat_id=chat_id,
            printer_num=printer_num,
            print_time=print_time,
            # Unknown for prints found already running at startup
            started_at=None if catchup else time.time(),
            total_layers=total_layers,
            file_name=file_name,
            subtask_name=subtask_name,
            gcode_file=gcode_file,
            catchup=catchup,
        )
        self.active_prints[printer_num] = session
        self._save()
        return session

    def end_print(self, printer_num: int) -> Optional[PrintSession]:
        session = self.active_prints.pop(printer_num, None)
        self._save()
        return session

    def get_print(self, printer_num: int) -> Optional[PrintSession]:
        return self.active_prints.get(printer_num)

    def claimed_printers(self, user_id: int) -> list[int]:
        """Printer numbers currently claimed by a user, in ascending order."""
        return sorted(num for num, s in self.active_prints.items() if s.claimed_by == user_id)

    def watched_printers(self, user_id: int) -> list[int]:
        """Printer numbers whose notifications a user was added to with /notify_user."""
        return sorted(num for num, s in self.active_prints.items() if user_id in s.watchers)

    def claim_print(self, printer_num: int, user_id: int, username: str) -> Optional[PrintSession]:
        changes = {'claimed_by': user_id, 'claimed_username': username}
        # Apply user's default preferences if they exist
        prefs = self.user_preferences.get(user_id)
        if prefs:
            changes.update(dm_preference=prefs.default_dm_preference, layer2_notify=prefs.layer2_notify)
        return self._update(printer_num, **changes)

    def unclaim_print(self, printer_num: int) -> Optional[PrintSession]:
        """Reset a session to its unclaimed state, keeping only the print's own details."""
        session = self.active_prints.get(printer_num)
        if not session:
            return None
        self.active_prints[printer_num] = PrintSession(
            message_id=session.message_id,
            chat_id=session.chat_id,
            printer_num=printer_num,
            print_time=session.print_time,
            started_at=session.started_at,
            total_layers=session.total_layers,
            file_name=session.file_name,
            subtask_name=session.subtask_name,
            gcode_file=session.gcode_file,
            catchup=session.catchup,
        )
        self._save()
        return self.active_prints[printer_num]

    def add_watchers(self, printer_num: int, user_ids: list[int]) -> list[int]:
        """Add users to a print's notifications, returning the ones that were newly added."""
        session = self.active_prints.get(printer_num)
        if not session:
            return []
        added = [uid for uid in dict.fromkeys(user_ids) if uid != session.claimed_by and uid not in session.watchers]
        self._update(printer_num, watchers=session.watchers + added)
        return added

    def remove_watchers(self, printer_num: int, user_ids: list[int]) -> list[int]:
        session = self.active_prints.get(printer_num)
        if not session:
            return []
        removed = [uid for uid in session.watchers if uid in user_ids]
        self._update(printer_num, watchers=[uid for uid in session.watchers if uid not in removed])
        return removed

    # --- Claimer preferences (also saved as the user's defaults) ---

    def set_dm_preference(self, printer_num: int, preference: str):
        session = self.active_prints.get(printer_num)
        if session and session.claimed_by:
            self._user_prefs(session.claimed_by).default_dm_preference = preference
        self._update(printer_num, dm_preference=preference)

    def set_layer2_notify(self, printer_num: int, enabled: bool):
        session = self.active_prints.get(printer_num)
        if session and session.claimed_by:
            self._user_prefs(session.claimed_by).layer2_notify = enabled
        self._update(printer_num, layer2_notify=enabled)

    # --- Notifications ---

    def mark_layer2_notified(self, printer_num: int):
        self._update(printer_num, layer2_notified=True)

    def set_notify_layer(self, printer_num: int, layer: int, notify_type: str = "layer", original_value: int = None):
        self._update(
            printer_num,
            notify_layer=layer,
            notify_layer_notified=False,
            notify_type=notify_type,
            notify_original_value=original_value if original_value is not None else layer,
        )

    def set_stop_at(self, printer_num: int, layer: Optional[int]):
        self._update(printer_num, stop_at_layer=layer)

    def mark_low_filament_alerted(self, printer_num: int, tray_id: int):
        session = self.active_prints.get(printer_num)
        if session:
            self._update(printer_num, low_filament_alerted=session.low_filament_alerted + [tray_id])

    def mark_stopped(self, printer_num: int, layer: int):
        self._update(printer_num, stop_at_layer=None, stopped_at_layer=layer)

    def mark_notify_layer_notified(self, printer_num: int):
        self._update(printer_num, notify_layer_notified=True)

    def set_notify_every(self, printer_num: int, notify_type: str, value: int, initial_value: int = 0):
        self._update(
            printer_num,
            notify_every_type=notify_type,
            notify_every_value=value,
            notify_every_last_value=initial_value,
            notify_every_last_sent_at=time.time() if notify_type == "time" else None,
        )

    def clear_notify_every(self, printer_num: int):
        self._update(
            printer_num,
            notify_every_type=None,
            notify_every_value=None,
            notify_every_last_value=None,
            notify_every_last_sent_at=None,
        )

    def mark_notify_every_sent(self, printer_num: int, value: int = None):
        changes = {'notify_every_last_sent_at': time.time()}
        if value is not None:
            changes['notify_every_last_value'] = value
        self._update(printer_num, **changes)

    # --- Users & roles ---

    def remember_user(self, user_id: int, username: Optional[str], name: Optional[str]):
        """Record a user's current username and name, saving only when they changed."""
        known = KnownUser(username=username, name=name)
        if self.users.get(user_id) != known:
            self.users[user_id] = known
            self._save()

    def find_user(self, username: str) -> Optional[int]:
        """Look up a user id by @username (case-insensitive), if the bot has seen them."""
        username = username.lstrip('@').lower()
        for user_id, known in self.users.items():
            if known.username and known.username.lower() == username:
                return user_id
        return None

    def display_name(self, user_id: int) -> str:
        known = self.users.get(user_id)
        if known and known.username:
            return f'@{known.username}'
        if known and known.name:
            return known.name
        return str(user_id)

    def get_role(self, user_id: int) -> Role:
        role = self.roles.get(user_id, Role.USER)
        if not self.roles_enabled and role != Role.OWNER:
            return Role.USER
        return role

    def set_role(self, user_id: int, role: Role):
        if role == Role.USER:
            self.roles.pop(user_id, None)
        else:
            self.roles[user_id] = role
        self._save()

    @property
    def owner_id(self) -> Optional[int]:
        return next((uid for uid, role in self.roles.items() if role == Role.OWNER), None)

    def ensure_owner(self, user_id: Optional[int]):
        """Seed the owner role from config on first run; later transfers take precedence."""
        if user_id and self.owner_id is None:
            self.set_role(int(user_id), Role.OWNER)

    def transfer_ownership(self, new_owner_id: int):
        """Make someone else the owner; the previous owner becomes an admin."""
        old_owner = self.owner_id
        if old_owner is not None:
            self.roles[old_owner] = Role.ADMIN
        self.roles[new_owner_id] = Role.OWNER
        self._save()

    def staff(self) -> dict[Role, list[int]]:
        grouped = {role: [] for role in (Role.OWNER, Role.ADMIN, Role.MOD)}
        for user_id in self.roles:
            role = self.get_role(user_id)
            if role != Role.USER:
                grouped[role].append(user_id)
        return grouped

    # --- Status message ---

    def set_status_message_id(self, message_id: int):
        self.status_message_id = message_id
        self._save()
