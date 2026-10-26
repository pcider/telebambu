"""Commands only work in DMs and command chats (dm_only_commands / command_chats)."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.ext import ApplicationHandlerStop, MessageHandler

import settings as settings_module
from bot import ui
from bot.handlers import BotHandlers
from data import Role
from settings import Settings

MAIN = '-100111'
STATUS = '-100222'
LOG = '-100333'
OWNER = 1
USER = 2


def _settings(**kwargs) -> Settings:
    values = dict(telegram_bot_token='x', chat_id=f'{MAIN}/5', status_chat_id=f'{STATUS}/6', log_chat_id=LOG)
    values.update(kwargs)
    return Settings(**values)


def _handlers(settings=None) -> BotHandlers:
    storage = Mock(display_name=Mock(return_value='@owner'))
    storage.get_role.side_effect = lambda uid: Role.OWNER if uid == OWNER else Role.USER
    settings = settings or _settings()
    settings.save = Mock()
    handlers = BotHandlers(storage, Mock(log_message=AsyncMock()), Mock(), settings)
    handlers.register(Mock())
    return handlers


def _update(text, chat_id=MAIN, chat_type='supergroup', user_id=USER, caption=False):
    message = SimpleNamespace(
        text=None if caption else text, caption=text if caption else None,
        chat=SimpleNamespace(id=int(chat_id), type=chat_type, title='IMBS'),
        message_thread_id=5, is_topic_message=True,
        delete=AsyncMock(), reply_text=AsyncMock())
    user = SimpleNamespace(id=user_id, username='someone', full_name='Some One', is_bot=False)
    return SimpleNamespace(effective_message=message, effective_user=user, effective_chat=message.chat)


def _context(dm_fails=False):
    hint = SimpleNamespace(delete=AsyncMock())
    send = AsyncMock(side_effect=[Exception('Forbidden'), hint] if dm_fails else None)
    return SimpleNamespace(bot=SimpleNamespace(username='TeleBambuBot', send_message=send),
                           application=SimpleNamespace(create_task=Mock(side_effect=lambda coro: coro.close())),
                           args=[])


class Gate(unittest.IsolatedAsyncioTestCase):
    async def _blocked(self, handlers, update, context=None) -> bool:
        try:
            await handlers.gate_commands(update, context or _context())
        except ApplicationHandlerStop:
            return True
        return False

    def test_registered_before_the_user_tracker_and_commands(self):
        app = Mock()
        handlers = BotHandlers(Mock(), Mock(), Mock(), _settings())
        handlers.register(app)
        gate = [call for call in app.add_handler.call_args_list if call.kwargs.get('group') == -2]
        self.assertEqual(len(gate), 1)
        self.assertIsInstance(gate[0].args[0], MessageHandler)
        self.assertIn('notify', handlers._command_names)

    async def test_main_chat_command_is_deleted_and_hinted_by_dm(self):
        handlers = _handlers()
        update, context = _update('/notify 50'), _context()
        self.assertTrue(await self._blocked(handlers, update, context))
        update.effective_message.delete.assert_awaited_once()
        context.bot.send_message.assert_awaited_once_with(
            chat_id=USER, text="Commands don't work in IMBS — send /notify here instead.")
        update.effective_message.reply_text.assert_not_awaited()

    async def test_hints_are_rate_limited_per_user(self):
        handlers = _handlers()
        context = _context()
        self.assertTrue(await self._blocked(handlers, _update('/info'), context))
        second = _update('/camera')
        self.assertTrue(await self._blocked(handlers, second, context))
        second.effective_message.delete.assert_awaited_once()
        context.bot.send_message.assert_awaited_once()
        # Someone else still gets their hint
        self.assertTrue(await self._blocked(handlers, _update('/info', user_id=3), context))
        self.assertEqual(context.bot.send_message.await_count, 2)

    async def test_refused_dm_falls_back_to_a_self_deleting_group_note(self):
        handlers = _handlers()
        update, context = _update('/claim 3'), _context(dm_fails=True)
        self.assertTrue(await self._blocked(handlers, update, context))
        note = context.bot.send_message.await_args_list[1].kwargs
        self.assertEqual(note['chat_id'], int(MAIN))
        self.assertEqual(note['message_thread_id'], 5)
        self.assertIn('/claim', note['text'])
        button = note['reply_markup'].inline_keyboard[0][0]
        self.assertEqual((button.text, button.url), ('Open DM', 'https://t.me/TeleBambuBot?start='))
        context.application.create_task.assert_called_once()

    async def test_group_note_deletes_itself(self):
        handlers = _handlers()
        context = _context(dm_fails=True)
        tasks = []
        context.application.create_task = tasks.append
        hint = SimpleNamespace(delete=AsyncMock())
        context.bot.send_message.side_effect = [Exception('Forbidden'), hint]
        await self._blocked(handlers, _update('/help'), context)
        with patch('bot.handlers.asyncio.sleep', AsyncMock()) as sleep:
            await tasks[0]
        sleep.assert_awaited_once_with(10)
        hint.delete.assert_awaited_once()

    async def test_dms_and_staff_chats_run_commands(self):
        handlers = _handlers(_settings(command_chats=['-100444']))
        for update in (_update('/notify 50', chat_id=USER, chat_type='private'),
                       _update('/notify 50', chat_id=STATUS), _update('/notify 50', chat_id=LOG),
                       _update('/notify 50', chat_id='-100444')):
            self.assertFalse(await self._blocked(handlers, update))
            update.effective_message.delete.assert_not_awaited()

    async def test_main_chat_is_not_whitelisted_through_the_status_chat(self):
        handlers = _handlers(_settings(status_chat_id=f'{MAIN}/7'))
        self.assertTrue(await self._blocked(handlers, _update('/info')))
        handlers.settings.command_chats = [MAIN]
        self.assertFalse(await self._blocked(handlers, _update('/info', user_id=3)))

    async def test_ignores_other_bots_unknown_commands_and_plain_text(self):
        handlers = _handlers()
        for text in ('/notify@OtherBot 50', '/weather', 'hello /notify', 'hi'):
            update = _update(text)
            self.assertFalse(await self._blocked(handlers, update), text)
            update.effective_message.delete.assert_not_awaited()
        self.assertTrue(await self._blocked(handlers, _update('/notify@telebambubot 50')))

    async def test_document_caption_commands_are_gated(self):
        update = _update('/print 3', caption=True)
        self.assertTrue(await self._blocked(_handlers(), update))
        update.effective_message.delete.assert_awaited_once()

    async def test_flag_off_lets_commands_through(self):
        handlers = _handlers(_settings(dm_only_commands=False))
        self.assertFalse(await self._blocked(handlers, _update('/notify 50')))

    async def test_owner_can_allow_or_deny_from_any_group_but_nothing_else(self):
        handlers = _handlers()
        self.assertFalse(await self._blocked(handlers, _update('/config allowhere', user_id=OWNER)))
        self.assertFalse(await self._blocked(handlers, _update('/config denyhere', user_id=OWNER)))
        self.assertTrue(await self._blocked(handlers, _update('/config', user_id=OWNER)))
        self.assertTrue(await self._blocked(handlers, _update('/config allowhere', user_id=USER)))


    def test_command_log_runs_before_the_gate(self):
        # Integration with log_commands: logging is in an earlier group than the gate, so commands
        # the gate stops (ApplicationHandlerStop ends later groups) are still logged
        app = Mock()
        handlers = BotHandlers(Mock(), Mock(), Mock(), _settings())
        handlers.register(app)
        groups = {call.args[0].callback: call.kwargs.get('group', 0) for call in app.add_handler.call_args_list}
        self.assertLess(groups[handlers.log_command], groups[handlers.gate_commands])
        self.assertLess(groups[handlers.gate_commands], groups[handlers.track_user])

    async def test_owner_can_use_config_here_in_any_group(self):
        # Integration with /config here: it is sent in the target chat, often the main chat
        handlers = _handlers()
        self.assertFalse(await self._blocked(handlers, _update('/config here chat_id', user_id=OWNER)))
        self.assertTrue(await self._blocked(handlers, _update('/config here chat_id', user_id=USER)))
        self.assertTrue(await self._blocked(handlers, _update('/config here update_interval', user_id=OWNER)))

class Config(unittest.IsolatedAsyncioTestCase):
    async def test_allowhere_and_denyhere_edit_command_chats_and_log(self):
        handlers = _handlers()
        update = _update('/config allowhere', user_id=OWNER)
        await handlers.cmd_config(update, SimpleNamespace(args=['allowhere']))
        self.assertEqual(handlers.settings.command_chats, [MAIN])
        handlers.settings.save.assert_called_once()
        self.assertIn(f'allowed commands in IMBS ({MAIN})', handlers.ms.log_message.await_args.args[0])

        await handlers.cmd_config(update, SimpleNamespace(args=['denyhere']))
        self.assertEqual(handlers.settings.command_chats, [])
        self.assertIn(f'removed commands in IMBS ({MAIN})', handlers.ms.log_message.await_args.args[0])

    async def test_allowhere_needs_a_group(self):
        handlers = _handlers()
        update = _update('/config allowhere', chat_id=OWNER, chat_type='private', user_id=OWNER)
        await handlers.cmd_config(update, SimpleNamespace(args=['allowhere']))
        self.assertEqual(handlers.settings.command_chats, [])
        handlers.settings.save.assert_not_called()

    async def test_set_command_chats_and_flag(self):
        handlers = _handlers()
        update = _update('/config', user_id=OWNER, chat_id=OWNER, chat_type='private')
        await handlers.cmd_config(update, SimpleNamespace(args=['set', 'command_chats', '-100444,', '-100555']))
        self.assertEqual(handlers.settings.command_chats, ['-100444', '-100555'])
        await handlers.cmd_config(update, SimpleNamespace(args=['set', 'command_chats', 'none']))
        self.assertEqual(handlers.settings.command_chats, [])
        await handlers.cmd_config(update, SimpleNamespace(args=['set', 'command_chats', 'main']))
        self.assertEqual(handlers.settings.command_chats, [])
        await handlers.cmd_config(update, SimpleNamespace(args=['set', 'dm_only_commands', 'false']))
        self.assertFalse(handlers.settings.dm_only_commands)
        self.assertEqual(handlers.settings.save.call_count, 3)

    def test_loaded_from_config_with_defaults(self):
        data = {'telegram_bot_token': 'x', 'chat_id': 1, 'status_chat_id': 2}
        loaded = settings_module._from_dict(data, 'config.yml')
        self.assertTrue(loaded.dm_only_commands)
        self.assertEqual(loaded.command_chats, [])
        loaded = settings_module._from_dict({**data, 'dm_only_commands': False, 'command_chats': [-100444]},
                                            'config.yml')
        self.assertFalse(loaded.dm_only_commands)
        self.assertEqual(loaded.command_chats, ['-100444'])

    def test_help_says_to_use_the_dm(self):
        self.assertIn('Use commands in a DM with the bot', ui.help_text(Role.USER))
        self.assertIn('/config allowhere|denyhere', ui.help_text(Role.OWNER))


if __name__ == '__main__':
    unittest.main()
