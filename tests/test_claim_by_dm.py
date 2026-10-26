"""Claiming happens in the bot's DM: the main chat button is a deep link, /start claim-N explains /claim N."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram.constants import ParseMode

from bot import ui
from bot.handlers import BotHandlers
from bot.messages import MessageService
from data.storage import PrintSession
from settings import Settings

LINK = 'https://t.me/telebambu_bot?start=claim-3'


def _handlers(session):
    settings = Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123')
    storage = Mock(get_print=Mock(return_value=session))
    pm = Mock(snapshot=Mock(return_value=SimpleNamespace(progress=40, time_left='1h 5m', layer=12, total_layers=80)))
    return BotHandlers(storage, Mock(mark_dm_reachable=Mock()), pm, settings)


def _context(*args):
    return SimpleNamespace(args=list(args), bot=SimpleNamespace(username='telebambu_bot', send_message=AsyncMock(),
                                                                edit_message_text=AsyncMock()))


def _update(user_id=42):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                           effective_message=SimpleNamespace(reply_text=AsyncMock()))


class ClaimKeyboard(unittest.TestCase):
    def test_button_is_a_deep_link_to_the_dm(self):
        button = ui.claim_keyboard('telebambu_bot', 3).inline_keyboard[0][0]
        self.assertEqual(button.text, 'Claim in DM')
        self.assertEqual(button.url, LINK)
        self.assertIsNone(button.callback_data)


class ClaimByDm(unittest.IsolatedAsyncioTestCase):
    async def test_print_started_message_has_the_deep_link_button(self):
        bot = Mock(username='telebambu_bot', send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
        storage = Mock(get_print=Mock(return_value=None))
        ms = MessageService(bot, SimpleNamespace(chat_id='-100123', status_chat_id='-100123'), storage)
        ms._send_main = AsyncMock(return_value=SimpleNamespace(message_id=7))
        await ms.send_print_started(3, '1h', 80)
        button = ms._send_main.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.url, LINK)

    async def test_start_link_for_unclaimed_print_asks_for_claim_command(self):
        handlers = _handlers(PrintSession(10, '-100123', 3))
        handlers._claim = AsyncMock()
        update, context = _update(), _context('claim-3')
        await handlers.cmd_start(update, context)
        handlers._claim.assert_not_awaited()
        handlers.storage.claim_print.assert_not_called()
        context.bot.send_message.assert_not_awaited()
        reply = update.effective_message.reply_text
        text = reply.call_args.args[0]
        self.assertIn('<code>/claim 3</code>', text)
        self.assertIn('Progress: 40%', text)
        self.assertEqual(reply.call_args.kwargs['parse_mode'], ParseMode.HTML)

    async def test_start_link_for_own_claim_sends_settings(self):
        handlers = _handlers(PrintSession(10, '-100123', 3, claimed_by=42, claimed_username='@me'))
        update, context = _update(), _context('claim-3')
        await handlers.cmd_start(update, context)
        context.bot.send_message.assert_awaited_once()
        self.assertEqual(context.bot.send_message.call_args.kwargs['chat_id'], 42)

    async def test_start_link_for_someone_elses_claim(self):
        handlers = _handlers(PrintSession(10, '-100123', 3, claimed_by=7, claimed_username='@other'))
        update, context = _update(), _context('claim-3')
        await handlers.cmd_start(update, context)
        update.effective_message.reply_text.assert_awaited_once_with('You are not the claimer of this print.')

    async def test_legacy_claim_button_opens_dm_without_claiming(self):
        handlers = _handlers(PrintSession(10, '-100123', 3))
        handlers._claim = AsyncMock()
        query = SimpleNamespace(data='claim:3', from_user=SimpleNamespace(id=42), answer=AsyncMock())
        await handlers.cb_claim(SimpleNamespace(callback_query=query), _context())
        query.answer.assert_awaited_once_with(url=LINK)
        handlers._claim.assert_not_awaited()

    async def test_unclaim_restores_the_deep_link_button(self):
        session = PrintSession(10, '-100123', 3)
        handlers = _handlers(session)
        handlers.storage.unclaim_print.return_value = session
        context = _context()
        await handlers._unclaim(context, 3)
        markup = context.bot.edit_message_text.call_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].url, LINK)


if __name__ == '__main__':
    unittest.main()
