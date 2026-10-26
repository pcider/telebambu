"""/config set, here, disable and enable."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import settings as settings_module
from bot import handlers as handlers_module
from bot.handlers import CONFIG_KEYS, BotHandlers
from data import Role
from settings import DISABLEABLE_COMMANDS, Settings


class ConfigCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'config.yml')
        self.settings = Settings(telegram_bot_token='secret-token', chat_id='-100123/5',
                                 status_chat_id='-100123', log_chat_id='-100999', path=self.path)
        self.storage = Mock(roles_enabled=True, display_name=Mock(return_value='@owner'))
        self.storage.get_role.return_value = Role.OWNER
        self.ms = Mock(log_message=AsyncMock())
        self.handlers = BotHandlers(self.storage, self.ms, Mock(), self.settings)
        menu = patch.object(handlers_module, 'set_command_menu', AsyncMock())
        self.set_command_menu = menu.start()
        self.addCleanup(menu.stop)

    async def config(self, *args, chat_id=-100777, thread=None):
        reply = AsyncMock()
        message = SimpleNamespace(reply_text=reply, chat_id=chat_id, message_thread_id=thread,
                                  is_topic_message=thread is not None)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=message)
        await self.handlers.cmd_config(update, SimpleNamespace(args=list(args), bot=Mock()))
        return reply.call_args.args[0]

    def saved(self) -> Settings:
        return settings_module.load(self.path)

    async def test_show_lists_every_key_and_disabled_commands_but_not_the_token(self):
        self.settings.disabled_commands['home'] = True
        text = await self.config()
        for key in CONFIG_KEYS:
            self.assertIn(f'- {key}:', text)
        self.assertIn('- thread_id: 5', text)
        self.assertIn('- status_thread_id: None', text)
        self.assertIn('Disabled commands: /home', text)
        self.assertNotIn('secret-token', text)

    async def test_set_values_are_parsed_saved_and_logged(self):
        cases = [('stats_log_interval', 'off', None), ('stats_log_interval', '120', 120),
                 ('auto_restart_printers', 'false', False), ('update_interval', '2.5', 2.5),
                 ('update_interval', 'inf', float('inf')), ('log_chat_id', 'none', None)]
        for key, raw, expected in cases:
            self.assertEqual(await self.config('set', key, raw), f'{key} set to {expected}.')
            self.assertEqual(getattr(self.saved(), key), expected)
        self.assertEqual(self.ms.log_message.await_count, len(cases))
        self.ms.log_message.assert_awaited_with('Config change: @owner set log_chat_id from -100999 to None.')

    async def test_invalid_values_are_rejected_without_saving(self):
        for key, raw in [('update_interval', 'nan'), ('update_interval', '0.5'), ('stats_log_interval', 'nan'),
                         ('stats_log_interval', '0'), ('auto_restart_printers', 'maybe'), ('chat_id', 'a/b'),
                         ('thread_id', 'abc')]:
            await self.config('set', key, raw)
        self.assertFalse(os.path.exists(self.path))
        self.ms.log_message.assert_not_awaited()
        self.assertEqual(self.settings.update_interval, 5)

    async def test_thread_keys_change_only_the_topic(self):
        await self.config('set', 'status_thread_id', '42')
        self.assertEqual(self.settings.status_chat_id, '-100123/42')
        self.ms.reset_status_message.assert_called_once()
        await self.config('set', 'thread_id', 'none')
        self.assertEqual(self.saved().chat_id, '-100123')
        self.assertEqual(self.ms.ctx.status_thread_id, 42)

        await self.config('set', 'log_chat_id', 'off')
        self.assertEqual(await self.config('set', 'log_thread_id', '3'), 'Set log_chat_id before log_thread_id.')

    async def test_here_uses_the_current_chat_and_topic(self):
        self.assertEqual(await self.config('here', 'log_chat_id', chat_id=-100555, thread=8),
                         'log_chat_id set to -100555/8.')
        self.assertEqual(self.saved().log_chat_id, '-100555/8')
        await self.config('here', 'chat_id', chat_id=-100555)
        self.assertEqual(self.settings.chat_id, '-100555')
        self.set_command_menu.assert_awaited_once()
        # Only chat target keys
        self.assertIn('Usage', await self.config('here', 'update_interval'))

    async def test_disable_and_enable_commands(self):
        self.assertEqual(await self.config('disable', '/home'), '/home disabled.')
        self.assertTrue(self.saved().is_disabled('home'))
        self.assertEqual(await self.config('disable', 'home'), '/home is already disabled.')
        self.assertEqual(await self.config('enable', 'home'), '/home enabled.')
        self.assertFalse(self.saved().is_disabled('home'))
        self.assertEqual(self.set_command_menu.await_count, 2)
        self.ms.log_message.assert_awaited_with('Config change: @owner enabled /home.')

        text = await self.config('disable', 'forceunclaim')
        self.assertIn("can't be turned off", text)
        self.assertNotIn('forceunclaim', self.settings.disabled_commands)
        self.assertEqual(set(self.settings.disabled_commands), set(DISABLEABLE_COMMANDS))

    async def test_disabling_staff_leaves_only_the_owner_at_once(self):
        await self.config('disable', 'staff')
        self.assertFalse(self.storage.roles_enabled)
        await self.config('enable', 'staff')
        self.assertTrue(self.storage.roles_enabled)

    async def test_only_the_owner_can_use_it(self):
        self.storage.get_role.return_value = Role.ADMIN
        self.assertEqual(await self.config('set', 'update_interval', '10'), 'Only the owner can use this command.')
        self.assertEqual(self.settings.update_interval, 5)


if __name__ == '__main__':
    unittest.main()
