"""The /beep easter egg: plays the buzzer on idle printers, with a per-printer cooldown."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bambulabs_api import GcodeState

from bot import handlers as handlers_module
from bot import ui
from bot.handlers import BEEP_COOLDOWN, BotHandlers
from data import Role
from printers import manager as manager_module
from printers.manager import PrinterManager
from settings import PrinterConfig, Settings


def _handlers(state=GcodeState.IDLE, online=True):
    printer = Mock(get_state=Mock(return_value=state))
    pm = Mock(numbers_text=Mock(return_value='1-3'), beep=Mock(return_value=True),
              get_online_printer=Mock(return_value=printer if online else None))
    pm.__contains__ = Mock(side_effect=lambda num: 1 <= num <= 3)
    storage = Mock(display_name=Mock(return_value='@someone'))
    settings = Settings(telegram_bot_token='x', chat_id='-100123', status_chat_id='-100123')
    return BotHandlers(storage, Mock(log_message=AsyncMock()), pm, settings)


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=7),
                           effective_message=SimpleNamespace(reply_text=AsyncMock()))


class BeepCommand(unittest.IsolatedAsyncioTestCase):
    async def test_idle_printer_beeps_and_is_logged(self):
        handlers = _handlers()
        update = _update()
        await handlers.cmd_beep(update, SimpleNamespace(args=['2']))
        handlers.pm.beep.assert_called_once_with(2)
        handlers.ms.log_message.assert_awaited_once_with('@someone beeped Printer 2.')
        self.assertIn('Beep!', update.effective_message.reply_text.call_args.args[0])

    async def test_busy_printer_is_refused(self):
        for state in (GcodeState.RUNNING, GcodeState.PAUSE, GcodeState.PREPARE, GcodeState.FAILED,
                      GcodeState.UNKNOWN):
            handlers = _handlers(state)
            update = _update()
            await handlers.cmd_beep(update, SimpleNamespace(args=['1']))
            handlers.pm.beep.assert_not_called()
            update.effective_message.reply_text.assert_awaited_once_with('Printer 1 is busy.')

    async def test_finished_printer_beeps(self):
        handlers = _handlers(GcodeState.FINISH)
        await handlers.cmd_beep(_update(), SimpleNamespace(args=['1']))
        handlers.pm.beep.assert_called_once_with(1)

    async def test_cooldown_is_per_printer(self):
        handlers = _handlers()
        with patch.object(handlers_module.time, 'time', return_value=1000.0):
            await handlers.cmd_beep(_update(), SimpleNamespace(args=['1']))
            update = _update()
            await handlers.cmd_beep(update, SimpleNamespace(args=['1']))
            self.assertEqual(update.effective_message.reply_text.call_args.args[0],
                             f'Printer 1 just beeped. Try again in {BEEP_COOLDOWN}s.')
            await handlers.cmd_beep(_update(), SimpleNamespace(args=['2']))
        with patch.object(handlers_module.time, 'time', return_value=1000.0 + BEEP_COOLDOWN):
            await handlers.cmd_beep(_update(), SimpleNamespace(args=['1']))
        self.assertEqual([c.args for c in handlers.pm.beep.call_args_list], [(1,), (2,), (1,)])

    async def test_bad_arguments_and_offline_printer(self):
        for args in ([], ['x'], ['1', '2'], ['9']):
            handlers = _handlers()
            await handlers.cmd_beep(_update(), SimpleNamespace(args=args))
            handlers.pm.beep.assert_not_called()
        handlers = _handlers(online=False)
        update = _update()
        await handlers.cmd_beep(update, SimpleNamespace(args=['1']))
        handlers.pm.beep.assert_not_called()
        update.effective_message.reply_text.assert_awaited_once_with('Printer 1 is not connected.')

    async def test_failed_send_does_not_start_cooldown(self):
        handlers = _handlers()
        handlers.pm.beep.return_value = False
        await handlers.cmd_beep(_update(), SimpleNamespace(args=['1']))
        handlers.ms.log_message.assert_not_awaited()
        self.assertNotIn(1, handlers._last_beep)

    def test_not_in_help(self):
        self.assertNotIn('/beep', ui.help_text(Role.OWNER, frozenset()))


class ManagerBeep(unittest.TestCase):
    def test_sends_m1006_tune_without_gcode_check(self):
        pm = PrinterManager({1: PrinterConfig(ip='1.2.3.4', access_code='x', serial='s', mac='m')})
        printer = Mock(gcode=Mock(return_value=True))
        with patch.object(pm, 'get_online_printer', return_value=printer):
            self.assertTrue(pm.beep(1))
        printer.gcode.assert_called_once_with(manager_module.BEEP_GCODE, gcode_check=False)
        self.assertEqual(manager_module.BEEP_GCODE[0], 'M1006 S1')
        self.assertEqual(manager_module.BEEP_GCODE[-1], 'M1006 W')

    def test_offline_printer_does_not_beep(self):
        pm = PrinterManager({})
        with patch.object(pm, 'get_online_printer', return_value=None):
            self.assertFalse(pm.beep(1))


if __name__ == '__main__':
    unittest.main()
