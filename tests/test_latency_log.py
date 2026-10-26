"""Stale printer / slow tick warnings (latency_warn_seconds)."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.handlers import BotHandlers
from data import Role
from printers.manager import PrinterManager, PrinterSlot, _track_messages
from printers.monitor import PrinterMonitor
from settings import Settings, load


class LatencyCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pm = Mock()
        self.ms = Mock(storage=Mock(), log_message=AsyncMock())
        self.settings = SimpleNamespace(latency_warn_seconds=10)
        self.monitor = PrinterMonitor(self.pm, self.ms, self.settings)

    def logged(self):
        return [c.args[0] for c in self.ms.log_message.await_args_list]

    async def check(self, age, now):
        self.pm.seconds_since_message.return_value = age
        with patch('printers.monitor.time.monotonic', return_value=now):
            await self.monitor._check_latency(3)

    async def test_logs_once_when_stale_and_once_on_recovery(self):
        await self.check(5, now=100)
        self.assertEqual(self.logged(), [])
        await self.check(12.34, now=112.34)  # last update at 100
        await self.check(17, now=117)
        await self.check(30, now=130)
        self.assertEqual(self.logged(), ['Printer 3: no update for 12.3 s'])
        await self.check(0.5, now=145.5)  # new update at 145
        self.assertEqual(self.logged()[1], 'Printer 3: updates resumed after 45.0 s')
        await self.check(1, now=146)
        self.assertEqual(len(self.logged()), 2)

    async def test_offline_printer_is_not_reported(self):
        await self.check(20, now=120)
        await self.check(None, now=200)  # went offline: reconnect_if_needed reports it
        await self.check(None, now=300)
        await self.check(1, now=301)  # back online with fresh data
        self.assertEqual(self.logged(), ['Printer 3: no update for 20.0 s'])

    async def test_disabled(self):
        self.settings.latency_warn_seconds = None
        await self.check(999, now=1000)
        self.assertEqual(self.logged(), [])

    async def test_slow_tick_is_logged(self):
        await self.monitor._check_tick_lag(3)
        await self.monitor._check_tick_lag(11.26)
        self.assertEqual(self.logged(), ['Monitor tick took 11.3 s'])


class MessageTracking(unittest.TestCase):
    def test_mqtt_messages_update_last_message_at(self):
        slot = PrinterSlot(ip='1.2.3.4', access_code='x', serial='s')
        slot.printer = SimpleNamespace(mqtt_client=SimpleNamespace(on_message_handler=None))
        _track_messages(slot)
        with patch('printers.manager.time.monotonic', return_value=42.0):
            slot.printer.mqtt_client.on_message_handler(None, None, None, None)
        self.assertEqual(slot.last_message_at, 42.0)

    def test_seconds_since_message_is_none_when_offline(self):
        pm = PrinterManager({})
        pm.slots[1] = PrinterSlot(ip='1.2.3.4', access_code='x', serial='s', last_message_at=10.0)
        with patch.object(pm, 'get_online_printer', return_value=None):
            self.assertIsNone(pm.seconds_since_message(1))
        with patch.object(pm, 'get_online_printer', return_value=object()), \
                patch('printers.manager.time.monotonic', return_value=25.0):
            self.assertEqual(pm.seconds_since_message(1), 15.0)


class LatencyConfig(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.yml')
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        self.settings = Settings(telegram_bot_token='t', chat_id='1', status_chat_id='1', path=self.path)
        storage = Mock(get_role=Mock(return_value=Role.OWNER), display_name=Mock(return_value='Owner'))
        self.ms = Mock(log_message=AsyncMock())
        self.handlers = BotHandlers(storage, self.ms, Mock(), self.settings)

    async def config_set(self, value):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 effective_message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=['set', 'latency_warn_seconds', value])
        await self.handlers.cmd_config(update, context)
        return update.effective_message.reply_text.await_args.args[0]

    def test_default_and_missing_key(self):
        self.assertEqual(self.settings.latency_warn_seconds, 10)
        with open(self.path, 'w') as f:
            f.write("telegram_bot_token: t\nchat_id: '1'\nstatus_chat_id: '1'\n")
        self.assertEqual(load(self.path).latency_warn_seconds, 10)

    async def test_set_saves_value(self):
        self.assertEqual(await self.config_set('2.5'), 'latency_warn_seconds set to 2.5.')
        self.assertEqual(load(self.path).latency_warn_seconds, 2.5)
        self.assertEqual(await self.config_set('off'), 'latency_warn_seconds set to None.')
        self.assertIsNone(load(self.path).latency_warn_seconds)

    async def test_rejects_bad_values(self):
        for raw in ('0', '-1', 'abc', 'nan', 'inf'):
            self.assertIn('must be a positive number', await self.config_set(raw))
        self.assertEqual(self.settings.latency_warn_seconds, 10)


if __name__ == '__main__':
    unittest.main()
