import json
import os
from dataclasses import dataclass, asdict
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


@dataclass
class UserPreferences:
    default_dm_preference: str = "chat"
    layer2_notify: bool = True


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
                self.active_prints[int(idx)] = PrintSession(**session_data)

            for user_id, prefs_data in data.get('user_preferences', {}).items():
                self.user_preferences[int(user_id)] = UserPreferences(**prefs_data)

            self.status_message_id = data.get('status_message_id')
        except Exception as e:
            print(f'Failed to load data: {e}')

    def _save(self):
        data = {
            'active_prints': {
                str(idx): asdict(session)
                for idx, session in self.active_prints.items()
            },
            'user_preferences': {
                str(uid): asdict(prefs)
                for uid, prefs in self.user_preferences.items()
            },
            'status_message_id': self.status_message_id
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def start_print(self, printer_index: int, message_id: int, chat_id: str) -> PrintSession:
        session = PrintSession(
            message_id=message_id,
            chat_id=chat_id,
            printer_index=printer_index
        )
        self.active_prints[printer_index] = session
        self._save()
        return session

    def claim_print(self, printer_index: int, user_id: int, username: str) -> Optional[PrintSession]:
        session = self.active_prints.get(printer_index)
        if not session:
            return None

        session.claimed_by = user_id
        session.claimed_username = username

        # Apply user's default preferences if they exist
        if user_id in self.user_preferences:
            prefs = self.user_preferences[user_id]
            session.dm_preference = prefs.default_dm_preference
            session.layer2_notify = prefs.layer2_notify

        self._save()
        return session

    def set_dm_preference(self, printer_index: int, preference: str):
        session = self.active_prints.get(printer_index)
        if session:
            session.dm_preference = preference
            # Also save as user's default
            if session.claimed_by:
                if session.claimed_by not in self.user_preferences:
                    self.user_preferences[session.claimed_by] = UserPreferences()
                self.user_preferences[session.claimed_by].default_dm_preference = preference
            self._save()

    def set_layer2_notify(self, printer_index: int, enabled: bool):
        session = self.active_prints.get(printer_index)
        if session:
            session.layer2_notify = enabled
            # Also save as user's default
            if session.claimed_by:
                if session.claimed_by not in self.user_preferences:
                    self.user_preferences[session.claimed_by] = UserPreferences()
                self.user_preferences[session.claimed_by].layer2_notify = enabled
            self._save()

    def mark_layer2_notified(self, printer_index: int):
        session = self.active_prints.get(printer_index)
        if session:
            session.layer2_notified = True
            self._save()

    def end_print(self, printer_index: int) -> Optional[PrintSession]:
        session = self.active_prints.pop(printer_index, None)
        self._save()
        return session

    def get_print(self, printer_index: int) -> Optional[PrintSession]:
        return self.active_prints.get(printer_index)

    def set_status_message_id(self, message_id: int):
        self.status_message_id = message_id
        self._save()
