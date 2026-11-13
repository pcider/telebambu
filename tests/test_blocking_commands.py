"""Printer commands run off the event loop, so one stalled printer can't freeze the bot."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bambulabs_api import GcodeState

from bot.handlers import BotHandlers
from data import Role
from printers import manager as manager_module
from printers.manager import PrinterManager, PrinterTimeout
from printers.timing import LoopLagProbe
from settings import PrinterConfig, Settings


class FakePrinter:
    """Stands in for bambulabs_api.Printer; commands sleep like a stalled MQTT publish."""

    def __init__(self, delay: float = 0.0, state: GcodeState = GcodeState.IDLE):
        self.delay = delay
        self.state = state
        self.light = 'off'
        self.calls: list[str] = []
        self.mqtt_client = SimpleNamespace(set_print_speed_lvl=self._command('speed'))

    def _command(self, name: str):
        def command(*args):
            self.calls.append(name)
            time.sleep(self.delay)  # blocks its thread, like wait_for_publish()
            return True
        command.__name__ = name
        return command

    def __getattr__(self, name):
        if name in ('home_printer', 'pause_print', 'resume_print', 'skip_objects', 'start_print'):
            return self._command(name)
        raise AttributeError(name)

    def turn_light_on(self):
        self._command('light_on')()
        self.light = 'on'
        return True

    def turn_light_off(self):
        self._command('light_off')()
        self.light = 'off'
        return True

    def get_light_state(self):
        return self.light

    def get_state(self):
        return self.state

    def mqtt_client_connected(self):
        return True

    def mqtt_client_ready(self):
        return True


def make_handlers(printers: dict[int, FakePrinter]):
    pm = PrinterManager({num: PrinterConfig(ip='10.0.0.1', access_code='x', serial='s') for num in printers})
    for num, printer in printers.items():
        pm.slots[num].printer = printer
    storage = Mock()
    storage.get_role.return_value = Role.OWNER
    storage.claimed_printers.return_value = []
    storage.display_name.return_value = '@admin'
    storage.get_print.return_value = None
    ms = Mock(log_message=AsyncMock())
    settings = Settings(telegram_bot_token='x', chat_id='-100', status_chat_id='-100')
    return BotHandlers(storage, ms, pm, settings), pm


def make_update(*args: str):
    reply = AsyncMock()
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply),
                             effective_user=SimpleNamespace(id=1))
    return update, SimpleNamespace(args=list(args)), reply


class BlockingCommands(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_printer_times_out_without_freezing_the_bot(self):
        handlers, _pm = make_handlers({4: FakePrinter(delay=0.5)})
        update, context, reply = make_update('4')
        probe = LoopLagProbe(interval=0.01)
        probe.start()
        await asyncio.sleep(0.02)
        with patch.object(manager_module, 'BLOCKING_CALL_TIMEOUT', 0.1):
            await handlers.cmd_home(update, context)
        probe.stop()
        self.assertLess(probe.take_max(), 0.08)  # the loop kept running while the printer stalled
        self.assertIn("Printer 4 didn't respond within 0.1s", reply.await_args.args[0])

    async def test_home_sends_to_printers_at_once_and_skips_busy_ones(self):
        printers = {1: FakePrinter(delay=0.2), 2: FakePrinter(delay=0.2), 3: FakePrinter(delay=0.2),
                    4: FakePrinter(state=GcodeState.RUNNING)}
        handlers, _pm = make_handlers(printers)
        update, context, reply = make_update('1', '2', '3', '4')
        started = time.monotonic()
        await handlers.cmd_home(update, context)
        self.assertLess(time.monotonic() - started, 0.45)  # about one delay, not three
        self.assertEqual(reply.await_args.args[0].splitlines(), [
            'Printer 1 is homing.', 'Printer 2 is homing.', 'Printer 3 is homing.',
            'Printer 4 is busy (RUNNING), not homing.'])
        self.assertEqual(printers[4].calls, [])
        handlers.ms.log_message.assert_awaited_once()
        self.assertIn('homed Printer(s) 1, 2, 3', handlers.ms.log_message.await_args.args[0])

    async def test_light_toggles_through_the_thread(self):
        printer = FakePrinter()
        handlers, _pm = make_handlers({4: printer})
        update, context, reply = make_update('4')
        await handlers.cmd_light(update, context)
        reply.assert_awaited_once_with('Printer 4 light turned ON.')
        self.assertEqual(printer.calls, ['light_on'])

    async def test_light_timeout_is_reported(self):
        handlers, _pm = make_handlers({4: FakePrinter(delay=0.5)})
        update, context, reply = make_update('4')
        with patch.object(manager_module, 'BLOCKING_CALL_TIMEOUT', 0.1):
            await handlers.cmd_light(update, context)
        self.assertIn("didn't respond", reply.await_args.args[0])

    async def test_pause_success_and_timeout(self):
        printer = FakePrinter(state=GcodeState.RUNNING)
        handlers, _pm = make_handlers({4: printer})
        update, context, reply = make_update('4')
        await handlers.cmd_pause(update, context)
        self.assertEqual(printer.calls, ['pause_print'])
        handlers.ms.log_message.assert_awaited()
        self.assertIn('paused Printer 4', handlers.ms.log_message.await_args_list[0].args[0])

        printer.delay = 0.5
        update, context, reply = make_update('4')
        with patch.object(manager_module, 'BLOCKING_CALL_TIMEOUT', 0.1):
            await handlers.cmd_pause(update, context)
        self.assertIn("didn't respond", reply.await_args.args[0])

    async def test_speed_and_skip_wrappers_run_in_a_thread(self):
        printer = FakePrinter(delay=0.5)
        _handlers, pm = make_handlers({4: printer})
        with patch.object(manager_module, 'BLOCKING_CALL_TIMEOUT', 0.1):
            with self.assertRaises(PrinterTimeout):
                await pm.set_speed(4, 3)
            with self.assertRaises(PrinterTimeout):
                await pm.skip_objects(4, [1, 2])
        printer.delay = 0
        self.assertTrue(await pm.set_speed(4, 3))
        self.assertTrue(await pm.skip_objects(4, [1, 2]))


if __name__ == '__main__':
    unittest.main()
