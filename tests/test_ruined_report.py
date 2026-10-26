"""The "Report ruined print" button: ask the presser for the cause, then tell the claimer."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram import ForceReply

import settings as settings_module
from bot import ui
from bot.handlers import BotHandlers
from bot.messages import MessageService
from bot.telegram_bot import BotContext
from data import Role
from data.storage import PrintSession
from settings import Settings


def _settings(**disabled) -> Settings:
    settings = Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123')
    settings.disabled_commands.update(disabled)
    return settings


def _query(data='ruined:3:42', user_id=7, username='presser'):
    question = SimpleNamespace(chat_id=-100123, message_id=500)
    message = SimpleNamespace(reply_text=AsyncMock(return_value=question))
    return SimpleNamespace(data=data, answer=AsyncMock(), message=message,
                           from_user=SimpleNamespace(id=user_id, username=username, full_name='Pat Presser'))


def _reply(user_id=7, to=500, text='spaghetti everywhere'):
    confirmation = SimpleNamespace(chat_id=-100123, message_id=502)
    message = SimpleNamespace(chat_id=-100123, message_id=501, text=text, caption=None,
                              reply_to_message=SimpleNamespace(message_id=to),
                              reply_text=AsyncMock(return_value=confirmation))
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user_id))


class RuinedReport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = PrintSession(10, '-100123', 3, claimed_by=42, claimed_username='@claimer')
        self.storage = Mock(get_print=Mock(return_value=self.session),
                            display_name=Mock(side_effect=lambda uid: {1: '@owner', 7: '@presser', 42: '@claimer'}[uid]))
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), delete_message=AsyncMock())
        self.ms = MessageService(self.bot, BotContext('-100123', 5, '-100123', 6, None, None), self.storage)
        self.ms.log_message = AsyncMock()
        self.ms.delete_later = Mock()
        self.handlers = BotHandlers(self.storage, self.ms, Mock(), _settings())

    async def asyncTearDown(self):
        for pending in self.handlers._ruined_questions.values():
            pending.timeout.cancel()

    async def _press(self, query=None):
        query = query or _query()
        await self.handlers.cb_report_ruined(SimpleNamespace(callback_query=query), None)
        return query

    def test_claimed_keyboard_keeps_existing_buttons(self):
        keyboard = self.handlers._claimed_keyboard(3, 42, ui.start_dm_keyboard('bot', 3))
        rows = keyboard.inline_keyboard
        self.assertEqual(rows[0][0].text, 'Start DM with bot')
        self.assertEqual(rows[1][0].callback_data, 'ruined:3:42')
        disabled = BotHandlers(self.storage, self.ms, Mock(), _settings(ruined_report=True))
        self.assertIsNone(disabled._claimed_keyboard(3, 42))

    async def test_press_asks_the_presser_with_a_selective_force_reply(self):
        query = await self._press()
        text = query.message.reply_text.call_args.args[0]
        self.assertEqual(text, "@presser, what's wrong with Printer 3's print? Reply to this message with the cause.")
        markup = query.message.reply_text.call_args.kwargs['reply_markup']
        self.assertIsInstance(markup, ForceReply)
        self.assertTrue(markup.selective)

    async def test_users_without_username_get_a_text_mention(self):
        query = await self._press(_query(username=None))
        self.assertIn('<a href="tg://user?id=7">Pat Presser</a>', query.message.reply_text.call_args.args[0])

    async def test_second_press_while_pending_is_refused(self):
        await self._press()
        second = await self._press(_query(user_id=8))
        second.message.reply_text.assert_not_awaited()
        self.assertTrue(second.answer.call_args.kwargs['show_alert'])

    async def test_disabled_button_refuses(self):
        self.handlers.settings.disabled_commands['ruined_report'] = True
        query = await self._press()
        query.message.reply_text.assert_not_awaited()
        self.assertIn('disabled', query.answer.call_args.args[0])

    async def test_reply_dms_claimer_logs_and_cleans_up(self):
        await self._press()
        update = _reply()
        await self.handlers.on_ruined_reply(update, None)
        self.bot.send_message.assert_awaited_once()
        sent = self.bot.send_message.call_args.kwargs
        self.assertEqual(sent['chat_id'], 42)
        self.assertEqual(sent['text'], '⚠️ @presser reports your print on Printer 3 is ruined: spaghetti everywhere')
        self.assertIn('spaghetti everywhere', self.ms.log_message.call_args.args[0])
        deleted = {c.kwargs['message_id'] for c in self.bot.delete_message.call_args_list}
        self.assertEqual(deleted, {500, 501})
        self.ms.delete_later.assert_called_once_with(-100123, 502, 5)
        self.assertEqual(self.handlers._ruined_questions, {})

    async def test_refused_dm_falls_back_to_the_main_chat(self):
        self.ms._dm_unreachable.add(42)
        await self._press()
        await self.handlers.on_ruined_reply(_reply(), None)
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], '-100123')

    async def test_replies_from_others_are_ignored(self):
        await self._press()
        await self.handlers.on_ruined_reply(_reply(user_id=8), None)
        await self.handlers.on_ruined_reply(_reply(to=999), None)
        self.bot.send_message.assert_not_awaited()
        self.assertIn(3, self.handlers._ruined_questions)

    async def test_ended_session_still_reaches_the_claimer_from_the_button(self):
        self.storage.get_print.return_value = None
        await self._press()
        await self.handlers.on_ruined_reply(_reply(), None)
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 42)

    async def test_unanswered_question_is_deleted_after_the_timeout(self):
        self.handlers.settings.ruined_report_timeout = 0.01
        await self._press()
        await asyncio.sleep(0.05)
        self.bot.delete_message.assert_awaited_once_with(chat_id=-100123, message_id=500)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.handlers._ruined_questions, {})

    async def test_failure_message_in_main_chat_has_the_button(self):
        self.ms._spawn = Mock(side_effect=lambda coro: coro.close())
        await self.ms.send_print_failed(3, 'some_error')
        keyboards = {c.kwargs['chat_id']: c.kwargs['reply_markup'] for c in self.bot.send_message.call_args_list}
        self.assertEqual(keyboards['-100123'].inline_keyboard[0][0].callback_data, 'ruined:3:42')

    async def test_config_set_timeout(self):
        self.storage.get_role.return_value = Role.OWNER
        self.handlers.settings.save = Mock()
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=SimpleNamespace(reply_text=reply))
        await self.handlers.cmd_config(update, SimpleNamespace(args=['set', 'ruined_report_timeout', '0']))
        self.assertIn('positive', reply.call_args.args[0])
        await self.handlers.cmd_config(update, SimpleNamespace(args=['set', 'ruined_report_timeout', '30']))
        self.assertEqual(self.handlers.settings.ruined_report_timeout, 30)
        self.handlers.settings.save.assert_called_once()

    def test_timeout_loads_from_config(self):
        data = {'telegram_bot_token': 'x', 'chat_id': 1, 'status_chat_id': 1, 'ruined_report_timeout': 20}
        self.assertEqual(settings_module._from_dict(data, 'config.yml').ruined_report_timeout, 20)
        del data['ruined_report_timeout']
        self.assertEqual(settings_module._from_dict(data, 'config.yml').ruined_report_timeout, 10)


if __name__ == '__main__':
    unittest.main()
