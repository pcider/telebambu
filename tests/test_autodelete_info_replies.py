"""Replies to informational commands in group chats are deleted after info_reply_ttl."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import settings as settings_module
from bot import messages as messages_module
from bot.handlers import BotHandlers, _is_info_command
from bot.messages import MessageService
from data import Role
from settings import Settings


def _settings(**kwargs) -> Settings:
    return Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123', **kwargs)


def _update(chat_type='supergroup', chat_id=-100123, message_id=5):
    sent = iter(range(100, 200))

    async def reply_text(*args, **kwargs):
        return SimpleNamespace(chat_id=chat_id, message_id=next(sent))

    message = SimpleNamespace(chat=SimpleNamespace(type=chat_type), chat_id=chat_id, message_id=message_id,
                              reply_to_message=None, reply_text=AsyncMock(side_effect=reply_text),
                              reply_photo=AsyncMock(side_effect=reply_text))
    return SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=message)


class InfoCommandMatching(unittest.TestCase):
    def test_read_only_uses_match(self):
        msg = SimpleNamespace(reply_to_message=None)
        for name, args in [('help', []), ('info', []), ('info', ['3']), ('camera', ['3']), ('staff', []),
                           ('staff', ['LIST', '@x']), ('config', []), ('printer', ['list']),
                           ('notify_user', ['3']), ('skip', []), ('skip', ['2']), ('speed', []),
                           ('queue', [])]:
            self.assertTrue(_is_info_command(name, args, msg), (name, args))

    def test_state_changes_dont_match(self):
        msg = SimpleNamespace(reply_to_message=None)
        for name, args in [('staff', ['edit', 'mod', '@x']), ('config', ['set', 'chat_id', '1']),
                           ('printer', ['del', '3']), ('notify_user', ['3', '@x']), ('skip', ['2', '1']),
                           ('speed', ['sport']), ('claim', ['3']), ('queue', ['3'])]:
            self.assertFalse(_is_info_command(name, args, msg), (name, args))

    def test_notify_user_replying_to_someone_adds_them(self):
        reply_to = SimpleNamespace(forum_topic_created=None)
        msg = SimpleNamespace(reply_to_message=reply_to, is_topic_message=False)
        self.assertFalse(_is_info_command('notify_user', ['3'], msg))


class AutodeleteWrapper(unittest.IsolatedAsyncioTestCase):
    def _handlers(self, **kwargs):
        ms = Mock(delete_later=Mock())
        handlers = BotHandlers(Mock(), ms, Mock(), _settings(**kwargs))
        return handlers, ms

    async def test_group_reply_and_command_are_deleted_after_ttl(self):
        handlers, ms = self._handlers()

        async def callback(update, context):
            await update.effective_message.reply_text('hello')
            await update.effective_message.reply_photo(photo=b'x')

        update = _update()
        await handlers._autodelete_info('help', callback)(update, SimpleNamespace(args=[]))
        ms.delete_later.assert_called_once_with([(-100123, 5), (-100123, 100), (-100123, 101)], 60)
        update.effective_message.reply_text.assert_awaited_once_with('hello')

    async def test_dm_replies_are_kept(self):
        handlers, ms = self._handlers()
        callback = AsyncMock()
        update = _update(chat_type='private')
        await handlers._autodelete_info('help', callback)(update, SimpleNamespace(args=[]))
        callback.assert_awaited_once_with(update, unittest.mock.ANY)
        ms.delete_later.assert_not_called()

    async def test_state_changing_use_is_kept(self):
        handlers, ms = self._handlers()
        callback = AsyncMock()
        update = _update()
        await handlers._autodelete_info('staff', callback)(update, SimpleNamespace(args=['edit', 'mod', '@x']))
        callback.assert_awaited_once()
        self.assertIs(callback.call_args.args[0], update)
        ms.delete_later.assert_not_called()

    async def test_off_keeps_replies(self):
        for ttl in (None, False, 0, -5, float('inf'), float('nan')):
            handlers, ms = self._handlers(info_reply_ttl=ttl)
            await handlers._autodelete_info('help', AsyncMock())(_update(), SimpleNamespace(args=[]))
            ms.delete_later.assert_not_called()

    async def test_deletes_scheduled_even_if_the_handler_fails(self):
        handlers, ms = self._handlers(info_reply_ttl=30)

        async def callback(update, context):
            await update.effective_message.reply_text('partial')
            raise RuntimeError('boom')

        with self.assertRaises(RuntimeError):
            await handlers._autodelete_info('info', callback)(_update(), SimpleNamespace(args=['3']))
        ms.delete_later.assert_called_once_with([(-100123, 5), (-100123, 100)], 30)


class DeleteLater(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_after_delay_and_ignores_failures(self):
        bot = Mock(delete_message=AsyncMock(side_effect=[Exception('no rights'), None]))
        ms = MessageService(bot, Mock(), Mock())
        sleep = AsyncMock()
        with patch.object(messages_module.asyncio, 'sleep', sleep):
            ms.delete_later([(1, 2), (1, 3)], 60)
            await asyncio.gather(*ms._background_tasks)
        sleep.assert_awaited_once_with(60)
        self.assertEqual(bot.delete_message.await_count, 2)
        bot.delete_message.assert_awaited_with(chat_id=1, message_id=3)


class ConfigSet(unittest.IsolatedAsyncioTestCase):
    async def _set(self, raw):
        storage = Mock(get_role=Mock(return_value=Role.OWNER), display_name=Mock(return_value='@owner'))
        settings = _settings()
        settings.save = Mock()
        handlers = BotHandlers(storage, Mock(log_message=AsyncMock()), Mock(), settings)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 effective_message=SimpleNamespace(reply_text=AsyncMock()))
        with patch('bot.handlers.get_bot_context'):
            await handlers.cmd_config(update, SimpleNamespace(args=['set', 'info_reply_ttl', raw]))
        return settings, update.effective_message.reply_text.call_args.args[0]

    async def test_accepts_positive_numbers_and_off(self):
        settings, text = await self._set('90')
        self.assertEqual(settings.info_reply_ttl, 90)
        self.assertEqual(text, 'info_reply_ttl set to 90.')
        settings, _ = await self._set('off')
        self.assertIsNone(settings.info_reply_ttl)

    async def test_rejects_other_values(self):
        for raw in ('0', '-1', 'inf', 'nan', 'soon'):
            settings, text = await self._set(raw)
            self.assertEqual(settings.info_reply_ttl, 60, raw)
            self.assertIn('positive number', text)

    def test_default_and_loading(self):
        base = {'telegram_bot_token': 'x', 'chat_id': 1, 'status_chat_id': 1}
        self.assertEqual(settings_module._from_dict(base, 'config.yml').info_reply_ttl, 60)
        self.assertIsNone(settings_module._from_dict({**base, 'info_reply_ttl': None}, 'config.yml').info_reply_ttl)


if __name__ == '__main__':
    unittest.main()
