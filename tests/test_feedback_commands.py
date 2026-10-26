"""/reportbug and /suggestfeature, and the feedback_chat_id / feedback_thread_id settings."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import settings as settings_module
from bot import handlers as handlers_module, ui
from bot.handlers import BotHandlers, MAX_FEEDBACK_LENGTH
from bot.telegram_bot import BotContext
from data import Role
from settings import Settings


def _handlers(log_chat_id=None, log_thread_id=None, **settings) -> BotHandlers:
    config = Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123', **settings)
    config.save = Mock()
    ms = Mock(log_message=AsyncMock())
    ms.ctx = BotContext('-100123', None, '-100123', None, log_chat_id, log_thread_id)
    return BotHandlers(Mock(), ms, Mock(), config)


def _update(text, reply_to=None, user_id=42, username='alice', chat_type='supergroup'):
    message = SimpleNamespace(
        text=text, reply_text=AsyncMock(), reply_to_message=reply_to, is_topic_message=False,
        message_thread_id=None, chat=SimpleNamespace(id=-100123, type=chat_type, title='IMBS'))
    user = SimpleNamespace(id=user_id, username=username, full_name='Alice')
    return SimpleNamespace(effective_message=message, effective_user=user)


def _context():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), args=[])


class FeedbackCommands(unittest.IsolatedAsyncioTestCase):
    async def test_bug_report_goes_to_feedback_chat_and_topic(self):
        handlers = _handlers(log_chat_id='-100999', feedback_chat_id='-100555/7', feedback_thread_id=9)
        update, context = _update('/reportbug camera is broken\nsecond line'), _context()
        await handlers.cmd_reportbug(update, context)
        kwargs = context.bot.send_message.call_args.kwargs
        self.assertEqual((kwargs['chat_id'], kwargs['message_thread_id']), ('-100555', 9))
        self.assertEqual(kwargs['text'], '🐞 Bug report\nFrom: @alice (42)\nChat: IMBS (-100123)\n\n'
                                         'camera is broken\nsecond line')
        update.effective_message.reply_text.assert_awaited_once_with('Thanks, sent to the maintainers.')

    async def test_falls_back_to_log_chat_then_reports_not_set_up(self):
        handlers = _handlers(log_chat_id='-100999', log_thread_id=3)
        context = _context()
        await handlers.cmd_suggestfeature(_update('/suggestfeature dark mode', chat_type='private'), context)
        kwargs = context.bot.send_message.call_args.kwargs
        self.assertEqual((kwargs['chat_id'], kwargs['message_thread_id']), ('-100999', 3))
        self.assertTrue(kwargs['text'].startswith('💡 Feature suggestion\n'))
        self.assertIn('Chat: private chat', kwargs['text'])

        handlers = _handlers()
        update, context = _update('/reportbug x'), _context()
        await handlers.cmd_reportbug(update, context)
        context.bot.send_message.assert_not_awaited()
        update.effective_message.reply_text.assert_awaited_once_with("Feedback isn't set up on this bot.")

    async def test_uses_replied_to_text_and_checks_empty_and_long(self):
        handlers = _handlers(feedback_chat_id='-100555')
        reply_to = SimpleNamespace(text='the bot crashed', caption=None, forum_topic_created=None, message_id=5)
        context = _context()
        await handlers.cmd_reportbug(_update('/reportbug', reply_to=reply_to), context)
        self.assertTrue(context.bot.send_message.call_args.kwargs['text'].endswith('\n\nthe bot crashed'))

        update, context = _update('/reportbug@telebambu_bot   '), _context()
        await handlers.cmd_reportbug(update, context)
        context.bot.send_message.assert_not_awaited()
        self.assertIn('Usage: /reportbug', update.effective_message.reply_text.call_args.args[0])

        update = _update('/reportbug ' + 'x' * (MAX_FEEDBACK_LENGTH + 1), user_id=7)
        await handlers.cmd_reportbug(update, context)
        context.bot.send_message.assert_not_awaited()
        self.assertIn('too long', update.effective_message.reply_text.call_args.args[0])

    async def test_rate_limit_per_user(self):
        handlers = _handlers(feedback_chat_id='-100555')
        context = _context()
        with patch.object(handlers_module.time, 'monotonic', return_value=1000):
            await handlers.cmd_reportbug(_update('/reportbug one'), context)
            update = _update('/suggestfeature two')
            await handlers.cmd_suggestfeature(update, context)
            self.assertIn('Please wait 60 s', update.effective_message.reply_text.call_args.args[0])
            await handlers.cmd_reportbug(_update('/reportbug other user', user_id=43), context)
        with patch.object(handlers_module.time, 'monotonic', return_value=1061):
            await handlers.cmd_suggestfeature(_update('/suggestfeature two'), context)
        self.assertEqual(context.bot.send_message.await_count, 3)

    async def test_failed_send_does_not_count_towards_rate_limit(self):
        handlers = _handlers(feedback_chat_id='-100555')
        context = _context()
        context.bot.send_message.side_effect = [RuntimeError('chat not found'), None]
        update = _update('/reportbug one')
        await handlers.cmd_reportbug(update, context)
        self.assertIn("couldn't be sent", update.effective_message.reply_text.call_args.args[0])
        update = _update('/reportbug one')
        await handlers.cmd_reportbug(update, context)
        update.effective_message.reply_text.assert_awaited_once_with('Thanks, sent to the maintainers.')

    async def test_owner_sets_feedback_keys_with_config(self):
        handlers = _handlers()
        handlers.storage.get_role.return_value = Role.OWNER
        handlers.storage.display_name.return_value = '@owner'
        with patch.object(handlers_module, 'get_bot_context'):
            for args in (['set', 'feedback_chat_id', '-100555/4'], ['set', 'feedback_thread_id', '12']):
                update, context = _update('/config'), _context()
                context.args = args
                await handlers.cmd_config(update, context)
            self.assertEqual((handlers.settings.feedback_chat_id, handlers.settings.feedback_thread_id),
                             ('-100555/4', 12))

            update, context = _update('/config'), _context()
            context.args = ['set', 'feedback_thread_id', 'abc']
            await handlers.cmd_config(update, context)
            self.assertIn('must be a forum topic id', update.effective_message.reply_text.call_args.args[0])

            for key in ('feedback_chat_id', 'feedback_thread_id'):
                update, context = _update('/config'), _context()
                context.args = ['set', key, 'none']
                await handlers.cmd_config(update, context)
        self.assertIsNone(handlers.settings.feedback_chat_id)
        self.assertIsNone(handlers.settings.feedback_thread_id)

    def test_config_loads_feedback_keys_and_help_lists_commands(self):
        data = {'telegram_bot_token': 'x', 'chat_id': 1, 'status_chat_id': 1,
                'feedback_chat_id': -100555, 'feedback_thread_id': 8}
        loaded = settings_module._from_dict(data, 'config.yml')
        self.assertEqual((loaded.feedback_chat_id, loaded.feedback_thread_id), ('-100555', 8))
        self.assertFalse(loaded.is_disabled('reportbug'))

        text = ui.help_text(Role.USER)
        self.assertIn('/reportbug <text>', text)
        self.assertIn('/suggestfeature <text>', text)
        text = ui.help_text(Role.USER, frozenset({'reportbug'}))
        self.assertNotIn('/reportbug', text)
        self.assertIn('/suggestfeature', text)


if __name__ == '__main__':
    unittest.main()
