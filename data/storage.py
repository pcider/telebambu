import json
import os
import time
from dataclasses import dataclass, asdict, fields
from typing import Optional

DATA_FILE = os.path.join(os.path.dirname(__file__), '..', 'data.json')


@dataclass
class PrintSession:
    message_id: int
    chat_id: str
    printer_index: int
    claimed_by: Optional[int] = None
    claimed_username: Optional[str] = None
    dm_preference: str = "chat"  # "chat" or "dm"
    layer2_notify: bool = True
    layer2_notified: bool = False
    print_time: Optional[str] = None
    total_layers: int = 0
    notify_layer: Optional[int] = None
    notify_layer_notified: bool = False
    notify_type: Optional[str] = None  # "layer" or "percent"
    notify_original_value: Optional[int] = None  # original value for display
    notify_every_type: Optional[str] = None  # "layers", "percent", or "time"
    notify_every_value: Optional[int] = None
    notify_every_last_value: Optional[int] = None
    notify_every_last_sent_at: Optional[float] = None


@dataclass
class UserPreferences:
    default_dm_preference: str = "chat"
    layer2_notify: bool = True


def _from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring keys it doesn't know about."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


class Storage:
    def __init__(self):
        self.active_prints: dict[int, PrintSession] = {}  # printer_index -> PrintSession
        self.user_preferences: dict[int, UserPreferences] = {}  # user_id -> UserPreferences
        self.status_message_id: Optional[int] = None
        self._load()

    def _load(self):
        if not os.path.exists(DATA_FILE):
            return
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)

            for idx, session_data in data.get('active_prints', {}).items():
                self.active_prints[int(idx)] = _from_dict(PrintSession, session_data)

            for user_id, prefs_data in data.get('user_preferences', {}).items():
                self.user_preferences[int(user_id)] = _from_dict(UserPreferences, prefs_data)

            self.status_message_id = data.get('status_message_id')
        except Exception as e:
            print(f'Failed to load data: {e}')

    def _save(self):
        data = {
            'active_prints': {str(idx): asdict(s) for idx, s in self.active_prints.items()},
            'user_preferences': {str(uid): asdict(p) for uid, p in self.user_preferences.items()},
            'status_message_id': self.status_message_id,
        }
        # Write to a temp file then rename, so a crash mid-write can't corrupt data.json
        tmp_file = DATA_FILE + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_file, DATA_FILE)

    def _update(self, printer_index: int, **changes) -> Optional[PrintSession]:
        """Apply field changes to an active session and persist. No-op if there is no session."""
        session = self.active_prints.get(printer_index)
        if session:
            for key, value in changes.items():
                setattr(session, key, value)
            self._save()
        return session

    def _user_prefs(self, user_id: int) -> UserPreferences:
        return self.user_preferences.setdefault(user_id, UserPreferences())

    # --- Session lifecycle ---

    def start_print(self, printer_index: int, message_id: int, chat_id: str,
                    print_time: str = None, total_layers: int = 0) -> PrintSession:
        session = PrintSession(
            message_id=message_id,
            chat_id=chat_id,
            printer_index=printer_index,
            print_time=print_time,
            total_layers=total_layers,
        )
        self.active_prints[printer_index] = session
        self._save()
        return session

    def end_print(self, printer_index: int) -> Optional[PrintSession]:
        session = self.active_prints.pop(printer_index, None)
        self._save()
        return session

    def get_print(self, printer_index: int) -> Optional[PrintSession]:
        return self.active_prints.get(printer_index)

    def claimed_printers(self, user_id: int) -> list[int]:
        """Printer indices currently claimed by a user, in ascending order."""
        return sorted(idx for idx, s in self.active_prints.items() if s.claimed_by == user_id)

    def claim_print(self, printer_index: int, user_id: int, username: str) -> Optional[PrintSession]:
        changes = {'claimed_by': user_id, 'claimed_username': username}
        # Apply user's default preferences if they exist
        prefs = self.user_preferences.get(user_id)
        if prefs:
            changes.update(dm_preference=prefs.default_dm_preference, layer2_notify=prefs.layer2_notify)
        return self._update(printer_index, **changes)

    def unclaim_print(self, printer_index: int) -> Optional[PrintSession]:
        """Reset a session to its unclaimed state, keeping only the print's own details."""
        session = self.active_prints.get(printer_index)
        if not session:
            return None
        self.active_prints[printer_index] = PrintSession(
            message_id=session.message_id,
            chat_id=session.chat_id,
            printer_index=printer_index,
            print_time=session.print_time,
            total_layers=session.total_layers,
        )
        self._save()
        return self.active_prints[printer_index]

    # --- Claimer preferences (also saved as the user's defaults) ---

    def set_dm_preference(self, printer_index: int, preference: str):
        session = self.active_prints.get(printer_index)
        if session and session.claimed_by:
            self._user_prefs(session.claimed_by).default_dm_preference = preference
        self._update(printer_index, dm_preference=preference)

    def set_layer2_notify(self, printer_index: int, enabled: bool):
        session = self.active_prints.get(printer_index)
        if session and session.claimed_by:
            self._user_prefs(session.claimed_by).layer2_notify = enabled
        self._update(printer_index, layer2_notify=enabled)

    # --- Notifications ---

    def mark_layer2_notified(self, printer_index: int):
        self._update(printer_index, layer2_notified=True)

    def set_notify_layer(self, printer_index: int, layer: int, notify_type: str = "layer", original_value: int = None):
        self._update(
            printer_index,
            notify_layer=layer,
            notify_layer_notified=False,
            notify_type=notify_type,
            notify_original_value=original_value if original_value is not None else layer,
        )

    def mark_notify_layer_notified(self, printer_index: int):
        self._update(printer_index, notify_layer_notified=True)

    def set_notify_every(self, printer_index: int, notify_type: str, value: int, initial_value: int = 0):
        self._update(
            printer_index,
            notify_every_type=notify_type,
            notify_every_value=value,
            notify_every_last_value=initial_value,
            notify_every_last_sent_at=time.time() if notify_type == "time" else None,
        )

    def clear_notify_every(self, printer_index: int):
        self._update(
            printer_index,
            notify_every_type=None,
            notify_every_value=None,
            notify_every_last_value=None,
            notify_every_last_sent_at=None,
        )

    def mark_notify_every_sent(self, printer_index: int, value: int = None):
        changes = {'notify_every_last_sent_at': time.time()}
        if value is not None:
            changes['notify_every_last_value'] = value
        self._update(printer_index, **changes)

    # --- Status message ---

    def set_status_message_id(self, message_id: int):
        self.status_message_id = message_id
        self._save()
