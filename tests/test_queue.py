"""/queue: a one-shot DM to everyone waiting when a printer's print ends."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from bambulabs_api import GcodeState

from bot import ui
from bot.commands import get_commands
from bot.handlers import MAX_QUEUE_PER_PRINTER, BotHandlers
from bot.messages import MessageService
from data import Role, storage as storage_module
from data.storage import Storage
from printers.manager import EventType, PrinterEvent
from printers.monitor import PrinterMonitor
from settings import Settings


class _TempStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(storage_module, 'DATA_FILE', os.path.join(tmp.name, 'data.json'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.storage = Storage()


class QueueCommand(_TempStorage):
    def setUp(self):
        super().setUp()
        self.settings = Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123')
        self.pm = MagicMock()
        self.pm.__contains__.side_effect = lambda num: num in (1, 2, 3)
        self.pm.snapshot.return_value = SimpleNamespace(state=GcodeState.RUNNING)
        self.handlers = BotHandlers(self.storage, Mock(), self.pm, self.settings)

    def _run(self, command, args, user_id=42, chat_type='private', dm_fails=False):
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                                 effective_message=SimpleNamespace(reply_text=reply,
                                                                   chat=SimpleNamespace(type=chat_type)))
        send = AsyncMock(side_effect=Exception('Forbidden') if dm_fails else None)
        context = SimpleNamespace(args=args, bot=SimpleNamespace(send_message=send, username='bambubot'))
        return command(update, context), reply, send

    async def test_subscribes_in_dm_and_lists_and_cancels(self):
        coro, reply, _ = self._run(self.handlers.cmd_queue, ['2'])
        await coro
        reply.assert_awaited_once_with("I'll message you when Printer 2's current print ends.")
        self.assertEqual(self.storage.free_reminders, {2: [42]})

        coro, reply, _ = self._run(self.handlers.cmd_queue, [])
        await coro
        self.assertIn('free: 2.', reply.call_args.args[0])

        coro, reply, _ = self._run(self.handlers.cmd_unqueue, ['2'])
        await coro
        reply.assert_awaited_once_with('Cancelled your reminder for Printer 2.')
        self.assertEqual(self.storage.free_reminders, {})

    async def test_free_printer_is_not_subscribed(self):
        self.pm.snapshot.return_value = SimpleNamespace(state=GcodeState.FINISH)
        coro, reply, _ = self._run(self.handlers.cmd_queue, ['1'])
        await coro
        reply.assert_awaited_once_with('Printer 1 is free now.')
        self.assertEqual(self.storage.free_reminders, {})

    async def test_group_without_dm_gets_start_link_instead_of_subscribing(self):
        coro, reply, _ = self._run(self.handlers.cmd_queue, ['3'], chat_type='supergroup', dm_fails=True)
        await coro
        self.assertEqual(self.storage.free_reminders, {})
        markup = reply.call_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].url, 'https://t.me/bambubot?start=queue-3')

        # The deep link's /start finishes the job in the DM
        coro, reply, _ = self._run(self.handlers.cmd_start, ['queue-3'])
        self.handlers.ms.mark_dm_reachable = Mock()
        await coro
        self.assertEqual(self.storage.free_reminders, {3: [42]})

    async def test_group_with_dm_confirms_by_dm(self):
        coro, reply, send = self._run(self.handlers.cmd_queue, ['3'], chat_type='supergroup')
        await coro
        send.assert_awaited_once_with(chat_id=42, text="I'll message you when Printer 3's current print ends.")
        self.assertEqual(self.storage.free_reminders, {3: [42]})

    async def test_claim_is_unaffected_and_per_printer_cap(self):
        self.storage.start_print(1, 10, '-100123')
        self.storage.claim_print(1, 7, '@claimer')
        for uid in range(100, 100 + MAX_QUEUE_PER_PRINTER):
            self.storage.add_free_reminder(1, uid)
        coro, reply, _ = self._run(self.handlers.cmd_queue, ['1'])
        await coro
        self.assertIn('Too many people', reply.call_args.args[0])
        self.assertEqual(self.storage.get_print(1).claimed_by, 7)

    def test_one_flag_disables_both_commands(self):
        self.settings.disabled_commands['queue'] = True
        self.assertTrue(self.settings.is_disabled('unqueue'))
        names = {c.name for c in get_commands(Role.USER, self.settings.disabled())}
        self.assertFalse({'queue', 'unqueue'} & names)
        self.assertNotIn('queue', ui.help_text(Role.USER, self.settings.disabled()))
        self.assertIn('/unqueue', ui.help_text(Role.USER))


class PrinterFreeNotification(_TempStorage):
    async def test_print_end_dms_everyone_once_and_survives_restart(self):
        self.storage.add_free_reminder(2, 42)
        self.storage.add_free_reminder(2, 43)
        self.storage = Storage()  # reloaded from data.json, as after a restart
        self.assertEqual(self.storage.free_reminders, {2: [42, 43]})

        bot = Mock(send_message=AsyncMock(side_effect=[None, Exception('Forbidden')]))
        ms = MessageService(bot, SimpleNamespace(log_chat_id=None), self.storage)
        ms.send_print_failed = AsyncMock()
        monitor = PrinterMonitor(Mock(get_camera_frame=AsyncMock(return_value=None)), ms, Mock())

        await monitor._handle_event(PrinterEvent(EventType.PRINT_FAILED, 2, None, {'error_code': 0}))
        texts = [call.kwargs['text'] for call in bot.send_message.await_args_list]
        self.assertEqual(texts, ['Printer 2 is free now (print failed).'] * 2)
        self.assertEqual(Storage().free_reminders, {})

        await ms.send_printer_free(2)
        self.assertEqual(bot.send_message.await_count, 2)


if __name__ == '__main__':
    unittest.main()
