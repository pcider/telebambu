"""Low filament alerts: AMS slots alert when low; the external spool (always 0%) never does."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bambulabs_api import GcodeState

from data.storage import PrintSession
from printers.manager import EXTERNAL_SPOOL_TRAY, FilamentTray
from printers.monitor import PrinterMonitor


def tray(tray_id: int, remain: int, label: str = 'AMS1 slot 1') -> FilamentTray:
    return FilamentTray(tray_id=tray_id, label=label, material='PLA', color='#000000',
                        remain=remain, active=True)


class LowFilament(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = PrintSession(100, '-100123', 4, claimed_by=42)
        self.storage = Mock()
        self.storage.get_print.return_value = self.session
        self.storage.mark_low_filament_alerted.side_effect = (
            lambda num, tray_id: self.session.low_filament_alerted.append(tray_id))
        self.pm = Mock()
        self.pm.snapshot.return_value = SimpleNamespace(state=GcodeState.RUNNING)
        self.ms = Mock(storage=self.storage, send_filament_notification=AsyncMock())
        self.monitor = PrinterMonitor(self.pm, self.ms, Mock())

    async def check(self, *trays: FilamentTray):
        for t in trays:
            self.pm.filament.return_value = [t]
            await self.monitor._check_low_filament(4)

    async def test_external_spool_reporting_0_percent_never_alerts(self):
        # The false alert: "External spool: PLA #000000 (0% left)"
        external = tray(EXTERNAL_SPOOL_TRAY, 0, 'External spool')
        await self.check(external, tray(EXTERNAL_SPOOL_TRAY, 50, 'External spool'), external)
        self.ms.send_filament_notification.assert_not_awaited()

    async def test_low_ams_slot_alerts_once(self):
        await self.check(tray(0, 5), tray(0, 3))
        self.ms.send_filament_notification.assert_awaited_once()
        self.assertIn('AMS1 slot 1: PLA #000000 (5% left)', self.ms.send_filament_notification.call_args.args[1])

    async def test_unknown_remaining_does_not_alert(self):
        await self.check(tray(0, 50), tray(0, -1))
        self.ms.send_filament_notification.assert_not_awaited()

    async def test_real_drop_alerts_once(self):
        await self.check(tray(0, 40), tray(0, 11), tray(0, 10), tray(0, 9), tray(0, 0))
        self.ms.send_filament_notification.assert_awaited_once()
        self.assertIn('10% left', self.ms.send_filament_notification.call_args.args[1])

    async def test_unclaimed_print_does_not_alert(self):
        self.session.claimed_by = None
        await self.check(tray(0, 40), tray(0, 5))
        self.ms.send_filament_notification.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
