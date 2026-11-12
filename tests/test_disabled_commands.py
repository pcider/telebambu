"""The per-command disabled_commands flags in config.yml."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import settings as settings_module
from bot import ui
from bot.commands import get_commands
from bot.handlers import BotHandlers
from data import Role, storage as storage_module
from data.storage import PrintSession, Storage
from settings import Settings


def _settings(**disabled) -> Settings:
    settings = Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123')
    settings.disabled_commands.update(disabled)
    return settings


class DisabledCommands(unittest.IsolatedAsyncioTestCase):
    def test_missing_flags_default_to_enabled_and_unknown_ones_are_ignored(self):
        data = {'telegram_bot_token': 'x', 'chat_id': 1, 'status_chat_id': 1,
                'disabled_commands': {'print': True, 'bogus': True}}
        loaded = settings_module._from_dict(data, 'config.yml')
        self.assertEqual(loaded.disabled(), frozenset({'print'}))
        self.assertNotIn('bogus', loaded.disabled_commands)
        self.assertFalse(loaded.is_disabled('home'))

    def test_menu_hides_disabled_commands(self):
        names = {c.name for c in get_commands(Role.OWNER, frozenset({'print', 'forcereclaim'}))}
        self.assertNotIn('print', names)
        self.assertNotIn('forcereclaim', names)
        self.assertIn('forceunclaim', names)
        self.assertIn('home', names)

    def test_help_leaves_out_disabled_commands(self):
        text = ui.help_text(Role.OWNER, frozenset({'speed', 'skip', 'staff', 'print', 'forcereclaim'}))
        for gone in ('/speed', '/skip', '/staff', '/print ', '/forcereclaim'):
            self.assertNotIn(gone, text)
        self.assertIn('/camera, /light, /stop_at, /pause, /resume <printer>', text)
        self.assertIn("/forceunclaim <printer> - Unclaim someone else's print", text)
        self.assertIn('/home', text)

    async def test_disabled_command_replies_instead_of_running(self):
        handlers = BotHandlers(Mock(), Mock(), Mock(), _settings(home=True))
        callback = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
        await handlers._unless_disabled('home', callback)(update, None)
        callback.assert_not_awaited()
        update.effective_message.reply_text.assert_awaited_once_with('/home is disabled on this bot.')

        handlers.settings.disabled_commands['home'] = False
        await handlers._unless_disabled('home', callback)(update, None)
        callback.assert_awaited_once()

    async def test_forceunclaim_unclaims_and_tells_the_claimer(self):
        session = PrintSession(10, '-100123', 3, claimed_by=42, claimed_username='@claimer')
        storage = Mock(get_print=Mock(return_value=session), display_name=Mock(return_value='@admin'))
        storage.get_role.return_value = Role.ADMIN
        storage.unclaim_print.return_value = session
        handlers = BotHandlers(storage, Mock(log_message=AsyncMock()), Mock(), _settings())
        handlers._claim = AsyncMock()
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(args=['3'],
                                  bot=SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock()))
        await handlers.cmd_forceunclaim(update, context)
        storage.unclaim_print.assert_called_once_with(3)
        handlers._claim.assert_not_awaited()
        self.assertEqual(reply.call_args.args[0], 'Unclaimed Printer 3 from @claimer.')
        context.bot.send_message.assert_awaited_once_with(chat_id=42, text='@admin unclaimed Printer 3.')


class OwnerOnlyRoles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(storage_module, 'DATA_FILE', os.path.join(tmp.name, 'data.json'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.storage = Storage()
        self.storage.set_role(1, Role.OWNER)
        self.storage.set_role(2, Role.ADMIN)
        self.storage.set_role(3, Role.MOD)

    def test_disabled_staff_leaves_only_the_owner_and_keeps_stored_roles(self):
        self.storage.roles_enabled = False
        self.assertEqual(self.storage.get_role(1), Role.OWNER)
        self.assertEqual(self.storage.get_role(2), Role.USER)
        self.assertEqual(self.storage.get_role(3), Role.USER)
        self.assertEqual(self.storage.staff(), {Role.OWNER: [1], Role.ADMIN: [], Role.MOD: []})

        self.storage.roles_enabled = True
        self.assertEqual(self.storage.get_role(2), Role.ADMIN)


if __name__ == '__main__':
    unittest.main()
