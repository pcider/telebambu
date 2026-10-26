"""The log_commands flag: commands and button presses posted to the log chat."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import settings as settings_module
from bot.handlers import BotHandlers, _logged_command
from data import Role
from settings import Settings


def _settings(log_commands=True) -> Settings:
    return Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123', log_commands=log_commands)


def _user(**kwargs):
    return SimpleNamespace(id=42, username='alice', full_name='Alice A', is_bot=False, **kwargs)


def _message_update(text=None, caption=None, chat_type='supergroup', topic=False):
    message = SimpleNamespace(text=text, caption=caption, is_topic_message=topic, message_thread_id=7 if topic else None)
    chat = SimpleNamespace(type=chat_type, title='IMBS', id=-100123)
    return SimpleNamespace(callback_query=None, message=message, edited_message=None,
                           effective_user=_user(), effective_chat=chat)


def _context():
    return SimpleNamespace(bot=SimpleNamespace(username='TelebambuBot'))


class CommandLog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ms = Mock(log_message=AsyncMock())
        self.handlers = BotHandlers(Mock(), self.ms, Mock(), _settings())

    async def _logged(self, update):
        await self.handlers.log_command(update, _context())
        return self.ms.log_message.await_args.args[0] if self.ms.log_message.await_args else None

    async def test_command_in_group_topic(self):
        text = await self._logged(_message_update('/claim 3', topic=True))
        self.assertEqual(text, 'Command: @alice (42) in IMBS (-100123, thread 7): /claim 3')

    async def test_command_in_dm(self):
        self.assertEqual(await self._logged(_message_update('/info', chat_type='private')),
                         'Command: @alice (42) in DM: /info')

    async def test_print_caption_is_logged(self):
        self.assertEqual(await self._logged(_message_update(caption='/print 3 1 ams')),
                         'Command: @alice (42) in IMBS (-100123): /print 3 1 ams')

    async def test_button_press(self):
        update = SimpleNamespace(callback_query=SimpleNamespace(data='claim:3', message=None),
                                 effective_user=_user(), effective_chat=SimpleNamespace(type='private'))
        self.assertEqual(await self._logged(update), 'Button: @alice (42) in DM: claim:3')

    async def test_plain_text_and_other_bots_commands_are_ignored(self):
        self.assertIsNone(await self._logged(_message_update('hello')))
        self.assertIsNone(await self._logged(_message_update('/start@OtherBot')))
        self.assertIsNotNone(await self._logged(_message_update('/start@telebambubot')))

    async def test_off_by_default(self):
        self.handlers.settings = Settings(telegram_bot_token='x', chat_id='1', status_chat_id='1')
        self.assertIsNone(await self._logged(_message_update('/claim 3')))

    async def test_logging_failure_is_swallowed(self):
        self.ms.log_message.side_effect = RuntimeError('boom')
        with patch('builtins.print'):
            await self.handlers.log_command(_message_update('/claim 3'), _context())

    def test_printer_secrets_are_redacted(self):
        self.assertEqual(_logged_command('/printer add 3 AA:BB:CC:DD:EE:FF 10.0.0.1 12345678 SERIAL'),
                         '/printer add [redacted]')
        self.assertEqual(_logged_command('/printer@TelebambuBot edit 3 code 87654321'),
                         '/printer@TelebambuBot edit [redacted]')
        self.assertEqual(_logged_command('/printer add,3,mac,ip,12345678,SN'), '/printer add [redacted]')
        self.assertEqual(_logged_command('/printer list'), '/printer list')
        self.assertEqual(_logged_command('/printer'), '/printer')
        self.assertEqual(_logged_command('/printers foo'), '/printers foo')

    async def test_redacted_in_log_message(self):
        text = await self._logged(_message_update('/printer edit 3 code 87654321'))
        self.assertNotIn('87654321', text)
        self.assertTrue(text.endswith('/printer edit [redacted]'))


class ConfigSetLogCommands(unittest.IsolatedAsyncioTestCase):
    def test_loaded_from_config(self):
        data = {'telegram_bot_token': 'x', 'chat_id': 1, 'status_chat_id': 1}
        self.assertFalse(settings_module._from_dict(data, 'config.yml').log_commands)
        self.assertTrue(settings_module._from_dict({**data, 'log_commands': True}, 'config.yml').log_commands)

    async def _set(self, value):
        settings = _settings(log_commands=False)
        settings.save = Mock()
        storage = Mock(display_name=Mock(return_value='@owner'))
        storage.get_role.return_value = Role.OWNER
        handlers = BotHandlers(storage, Mock(log_message=AsyncMock()), Mock(), settings)
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=SimpleNamespace(reply_text=reply))
        with patch('bot.handlers.get_bot_context'):
            await handlers.cmd_config(update, SimpleNamespace(args=['set', 'log_commands', value]))
        return settings, reply.await_args.args[0]

    async def test_owner_can_toggle(self):
        settings, text = await self._set('true')
        self.assertIs(settings.log_commands, True)
        self.assertEqual(text, 'log_commands set to True.')
        settings.save.assert_called_once()

    async def test_rejects_other_values(self):
        settings, text = await self._set('maybe')
        self.assertIs(settings.log_commands, False)
        self.assertEqual(text, 'log_commands must be true or false.')
        settings.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
