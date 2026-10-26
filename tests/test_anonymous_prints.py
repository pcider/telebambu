"""Opt-in anonymous prints: an anonymous claimer's name only reaches the log chat."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import Forbidden

from bot import ui
from bot.handlers import BotHandlers
from bot.messages import MessageService
from bot.telegram_bot import BotContext
from data import storage as storage_module
from data.storage import Storage
from settings import Settings

MAIN, LOG = '-100123', '-100999'
CLAIMER, OTHER = 42, 7
SECRET = 'secret_claimer'


class FakeBot:
    """Records every text the bot sends, edits or shows, with the chat it went to."""
    username = 'telebambu_bot'

    def __init__(self):
        self.sent: list[tuple[object, str]] = []
        self.refuse_dm = False
        self._next_id = 100

    def _record(self, chat_id, text):
        self.sent.append((chat_id, text or ''))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id, chat_id=chat_id)

    async def send_message(self, chat_id, text, message_thread_id=None, reply_markup=None, **_):
        if self.refuse_dm and chat_id == CLAIMER:
            raise Forbidden('bot was blocked by the user')
        return self._record(chat_id, text + _buttons(reply_markup))

    async def send_photo(self, chat_id, photo, caption=None, message_thread_id=None, reply_markup=None, **_):
        return await self.send_message(chat_id, caption or '', reply_markup=reply_markup)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **_):
        return self._record(chat_id, text + _buttons(reply_markup))

    async def delete_message(self, chat_id, message_id):
        pass


def _buttons(markup) -> str:
    if not markup:
        return ''
    return ' ' + ' '.join(button.text for row in markup.inline_keyboard for button in row)


class AnonymousPrints(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(storage_module, 'DATA_FILE', os.path.join(tmp.name, 'data.json'))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.storage = Storage()
        self.bot = FakeBot()
        self.ms = MessageService(self.bot, BotContext(MAIN, None, MAIN, None, LOG, None), self.storage)
        self.pm = MagicMock()
        self.pm.__contains__.return_value = True
        self.pm.snapshot.return_value = None
        self.pm.status_line.return_value = '3: RUNNING'
        self.settings = Settings(telegram_bot_token='x', chat_id=MAIN, status_chat_id=MAIN, log_chat_id=LOG)
        self.handlers = BotHandlers(self.storage, self.ms, self.pm, self.settings)
        self.context = SimpleNamespace(bot=self.bot, args=[])
        self.storage.remember_user(CLAIMER, SECRET, 'Secret Person')
        self.storage.remember_user(OTHER, 'other', 'Other')

    def _user(self, user_id):
        known = self.storage.users[user_id]
        return SimpleNamespace(id=user_id, username=known.username, full_name=known.name, is_bot=False)

    def _update(self, user_id, chat_type='supergroup'):
        """A command update whose replies are recorded as sent to the main chat (or the DM)."""
        chat_id = user_id if chat_type == 'private' else MAIN

        async def reply_text(text, **kwargs):
            return self.bot._record(chat_id, text + _buttons(kwargs.get('reply_markup')))
        message = SimpleNamespace(reply_text=reply_text, chat=SimpleNamespace(type=chat_type),
                                  reply_to_message=None, entities=None)
        return SimpleNamespace(effective_user=self._user(user_id), effective_message=message)

    async def _press(self, user_id, data):
        """Press an inline button; the callback alert counts as shown in the main chat."""
        async def answer(text=None, show_alert=False, **_):
            if text:
                self.bot._record(MAIN, text)

        async def edit_message_text(text, reply_markup=None):
            self.bot._record(user_id, text + _buttons(reply_markup))
        query = SimpleNamespace(data=data, from_user=self._user(user_id), answer=answer,
                                edit_message_text=edit_message_text, message=None)
        callback = self.handlers.cb_claim if data.startswith('claim') else self.handlers.cb_anonymous_toggle
        await callback(SimpleNamespace(callback_query=query), self.context)

    async def _claim(self, user_id):
        """/claim 3 in the user's DM, where the Claim in DM button sends them."""
        self.context.args = ['3']
        await self.handlers.cmd_claim(self._update(user_id, 'private'), self.context)
        self.context.args = []

    def _public_texts(self):
        """Everything other users could see: all chats except the log chat and the claimer's DM."""
        return [text for chat_id, text in self.bot.sent if chat_id not in (LOG, CLAIMER)]

    def assertNameHidden(self):
        for text in self._public_texts():
            self.assertNotIn(SECRET, text)
            self.assertNotIn('Secret Person', text)

    async def _anonymous_claim(self):
        self.storage.set_anonymous(CLAIMER, True)
        await self.ms.send_print_started(3, '1h', 100)
        await self._claim(CLAIMER)

    async def test_anonymous_claimer_name_never_reaches_the_main_chat(self):
        await self._anonymous_claim()
        self.assertTrue(self.storage.get_print(3).anonymous)
        main_edit = [text for chat_id, text in self.bot.sent if chat_id == MAIN][-1]
        self.assertIn(f'Printer 3 started by {ui.ANONYMOUS_NAME}', main_edit)

        # Others trying to claim (the legacy button, /claim in the DM and in the group), /info as a
        # watcher and the watcher list
        await self._press(OTHER, 'claim:3')
        await self._claim(OTHER)
        self.context.args = ['3']
        await self.handlers.cmd_claim(self._update(OTHER), self.context)
        self.storage.add_watchers(3, [OTHER])
        self.context.args = []
        await self.handlers.cmd_info(self._update(OTHER), self.context)
        self.context.args = ['3']
        await self.handlers.cmd_notify_user(self._update(OTHER), self.context)
        self.context.args = ['3']
        await self.handlers.cmd_info(self._update(CLAIMER), self.context)

        await self.ms.send_print_finished(3, b'jpeg')
        self.assertIn(f'finished printing. ({ui.ANONYMOUS_NAME})', '\n'.join(self._public_texts()))

        # A later print stays anonymous and fails in the main chat
        await self.ms.send_print_started(3, '1h', 100)
        await self._claim(CLAIMER)
        await self.ms.send_print_failed(3, 0)
        self.ms._background_tasks.pop().cancel()

        self.assertNameHidden()
        # The log chat still says who claimed
        log = '\n'.join([text for chat_id, text in self.bot.sent if chat_id == LOG] + self.ms._log_buffer)
        self.assertIn(f'@{SECRET} claimed Printer 3 anonymously', log)

    async def test_dm_unavailable_note_and_start_prompt_hide_the_name(self):
        self.bot.refuse_dm = True
        await self._anonymous_claim()
        self.storage.set_dm_preference(3, 'dm')
        await self.ms.send_print_finished(3, None)
        texts = '\n'.join(self._public_texts())
        self.assertIn(f'For {ui.ANONYMOUS_NAME}: DM unavailable', texts)
        self.assertIn('An anonymous user, please start a conversation', texts)
        self.assertNameHidden()

    async def test_names_are_shown_by_default(self):
        await self.ms.send_print_started(3, '1h', 100)
        await self._claim(CLAIMER)
        await self._claim(OTHER)
        texts = '\n'.join(self._public_texts())
        self.assertIn(f'Printer 3 started by @{SECRET}', texts)
        self.assertIn(f'already claimed by @{SECRET}', texts)

    async def test_command_applies_to_current_claims_and_refuses_groups(self):
        await self.ms.send_print_started(3, '1h', 100)
        await self._claim(CLAIMER)

        self.context.args = ['on']
        await self.handlers.cmd_anonymous(self._update(CLAIMER), self.context)
        self.assertIn('private chat', self.bot.sent[-1][1])
        self.assertFalse(self.storage.is_anonymous(CLAIMER))

        await self.handlers.cmd_anonymous(self._update(CLAIMER, 'private'), self.context)
        self.assertTrue(self.storage.is_anonymous(CLAIMER))
        self.assertTrue(self.storage.get_print(3).anonymous)
        main_edit = [text for chat_id, text in self.bot.sent if chat_id == MAIN][-1]
        self.assertEqual(main_edit, f'Printer 3 started by {ui.ANONYMOUS_NAME} (time: 1h, layers: 100)')
        self.assertIn('Anonymous is ON', self.bot.sent[-1][1])

        self.context.args = []
        await self.handlers.cmd_anonymous(self._update(CLAIMER, 'private'), self.context)
        self.assertIn('Anonymous is ON', self.bot.sent[-1][1])

        # The settings button turns it back off
        await self._press(CLAIMER, 'anon:3')
        self.assertFalse(self.storage.get_print(3).anonymous)
        self.assertIn('Anonymous: OFF', self.bot.sent[-1][1])
        self.assertIn(f'started by @{SECRET}', [text for chat_id, text in self.bot.sent if chat_id == MAIN][-1])

    async def test_disabled_command_hides_the_button_but_keeps_names_hidden(self):
        await self._anonymous_claim()
        self.settings.disabled_commands['anonymous'] = True
        text, keyboard = self.handlers._settings_message(3)
        self.assertNotIn('Anonymous:', _buttons(keyboard))
        await self._press(CLAIMER, 'anon:3')
        self.assertTrue(self.storage.get_print(3).anonymous)
        self.assertIn('/anonymous is disabled', self.bot.sent[-1][1])
        self.assertNotIn('/anonymous', ui.help_text(ui.Role.USER, self.settings.disabled()))

    def test_preference_survives_a_restart(self):
        self.storage.set_anonymous(CLAIMER, True)
        self.assertTrue(Storage().is_anonymous(CLAIMER))


if __name__ == '__main__':
    unittest.main()
