"""Print lifecycle and claim events are posted to the log chat."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot.handlers import BotHandlers
from bot.messages import MessageService
from bot.telegram_bot import BotContext
from data.storage import PrintSession


def _settings():
    return Mock(disabled=Mock(return_value=set()))


class PrintEventLogs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = PrintSession(10, '-100123', 3, claimed_by=42, claimed_username='@claimer',
                                    file_name='Benchy', started_at=time.time() - 90 * 60)
        self.storage = Mock(owner_id=99)
        self.storage.get_print.return_value = self.session
        self.context = BotContext('-100123', 5, '-100123', 6, '-100999', None)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=11, chat_id=1)),
                                   send_photo=AsyncMock(), delete_message=AsyncMock())
        self.ms = MessageService(self.bot, self.context, self.storage)
        self.ms.log_message = AsyncMock()

    def logged(self) -> list[str]:
        return [c.args[0] for c in self.ms.log_message.await_args_list]

    async def test_started_logs_name_estimate_and_layers(self):
        self.storage.get_print.return_value = None
        await self.ms.send_print_started(3, '1h30m', 250, 'Benchy.gcode.3mf')
        self.assertEqual(self.logged(), ["Printer 3 started printing 'Benchy.gcode.3mf' (est. 1h30m, 250 layers)."])

    async def test_catchup_start_is_left_to_the_startup_log(self):
        self.storage.get_print.return_value = None
        await self.ms.send_print_started(3, '1h', 0, 'Benchy', catchup=True)
        self.ms.log_message.assert_not_awaited()

    async def test_finished_logs_claimer_and_duration(self):
        await self.ms.send_print_finished(3, None)
        self.assertEqual(self.logged(), ["Printer 3 finished printing ('Benchy', claimed by @claimer (42), took 1h30m)."])
        self.storage.end_print.assert_called_once_with(3)

    async def test_failed_logs_reason_and_code(self):
        self.session.claimed_by = None
        self.session.started_at = None
        await self.ms.send_print_failed(3, 50364419)
        line, = self.logged()
        self.assertTrue(line.startswith('Printer 3 failed: '), line)
        self.assertIn('(code: 50364419)', line)
        self.assertIn("('Benchy', claimed by nobody)", line)

    async def test_cancel_and_stop_at_are_not_called_failures(self):
        await self.ms.send_print_failed(3, 0)
        self.assertIn('Printer 3 was cancelled (', self.logged()[-1])
        self.session.stopped_at_layer = 40
        await self.ms.send_print_failed(3, 0)
        self.assertIn('Printer 3 was stopped at layer 40 (/stop_at) (', self.logged()[-1])

    async def test_log_failure_does_not_break_the_finish(self):
        self.ms.log_message.side_effect = RuntimeError('boom')
        await self.ms.send_print_finished(3, None)
        self.storage.end_print.assert_called_once_with(3)


class ClaimEventLogs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = Mock()
        self.claimed = PrintSession(10, '-100123', 3, claimed_by=42, claimed_username='@claimer')
        self.unclaimed = PrintSession(10, '-100123', 3)
        self.ms = Mock(log_event=AsyncMock(), log_message=AsyncMock())
        self.handlers = BotHandlers(self.storage, self.ms, Mock(), _settings())
        self.context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock(),
                                                           username='bambubot'))

    async def test_claim_button_logs_user_and_id(self):
        self.storage.get_print.return_value = self.unclaimed
        self.storage.claim_print.return_value = self.claimed
        query = SimpleNamespace(data='claim:3', from_user=SimpleNamespace(id=42, username='claimer'),
                                answer=AsyncMock())
        await self.handlers.cb_claim(SimpleNamespace(callback_query=query), self.context)
        self.ms.log_event.assert_awaited_once_with('Claim: @claimer (42) claimed Printer 3.')

    async def test_unclaim_logs_the_previous_claimer(self):
        self.storage.get_print.return_value = self.claimed
        self.storage.unclaim_print.return_value = self.unclaimed
        await self.handlers._unclaim(self.context, 3)
        self.ms.log_event.assert_awaited_once_with('Unclaim: @claimer (42) unclaimed Printer 3.')

    async def test_force_reclaim_and_unclaim_log_once(self):
        self.storage.get_print.return_value = self.claimed
        self.storage.claim_print.return_value = self.claimed
        self.storage.unclaim_print.return_value = self.unclaimed
        self.storage.display_name.return_value = '@other'
        self.handlers.is_admin = Mock(return_value=True)
        self.handlers._require_admin = AsyncMock(return_value=True)
        self.handlers._resolve_user = Mock(return_value=77)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1))

        await self.handlers.cmd_forcereclaim(update, SimpleNamespace(args=['3', '@other'], bot=self.context.bot))
        await self.handlers.cmd_forceunclaim(update, SimpleNamespace(args=['3'], bot=self.context.bot))
        self.ms.log_event.assert_not_awaited()
        self.assertEqual(self.ms.log_message.await_count, 2)
