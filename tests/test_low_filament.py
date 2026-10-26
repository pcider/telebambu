"""Low filament alerts are muted by default and only logged with the raw tray report."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bambulabs_api import GcodeState

from data.storage import PrintSession
from printers.manager import parse_filament
from printers.monitor import PrinterMonitor

PRINT_DATA = {'ams': {'tray_now': '1', 'ams': [{'id': '0', 'tray': [
    {'id': '0', 'tray_type': 'PLA', 'remain': 80},
    {'id': '1', 'tray_type': 'PETG', 'remain': 0, 'tag_uid': '0000000000000000'},
]}]}}


class LowFilament(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = PrintSession(10, '-100123', 7, claimed_by=42, claimed_username='@claimer')
        self.storage = Mock()
        self.storage.get_print.return_value = self.session
        self.pm = Mock()
        self.pm.snapshot.return_value = SimpleNamespace(state=GcodeState.RUNNING)
        self.pm.filament.return_value = parse_filament(PRINT_DATA)
        self.ms = Mock(storage=self.storage, log_message=AsyncMock(), send_filament_notification=AsyncMock())
        self.settings = SimpleNamespace(low_filament_alerts=False)
        self.monitor = PrinterMonitor(self.pm, self.ms, self.settings)

    def test_tray_keeps_raw_report(self):
        tray = next(t for t in parse_filament(PRINT_DATA) if t.active)
        self.assertEqual(tray.raw['tag_uid'], '0000000000000000')

    async def test_muted_by_default_logs_raw_report_once(self):
        await self.monitor._check_low_filament(7)
        self.ms.send_filament_notification.assert_not_awaited()
        text = self.ms.log_message.call_args.args[0]
        self.assertIn('alert not sent', text)
        self.assertIn('"remain": 0', text)
        self.storage.mark_low_filament_alerted.assert_called_once_with(7, 1)

    async def test_enabled_sends_alert(self):
        self.settings.low_filament_alerts = True
        await self.monitor._check_low_filament(7)
        self.ms.send_filament_notification.assert_awaited_once()
        self.ms.log_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
