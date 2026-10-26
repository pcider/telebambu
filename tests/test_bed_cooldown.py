"""Telling a finished print's claimer when the bed has cooled enough to remove it."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bambulabs_api import GcodeState
from telegram.error import Forbidden

from bot import ui
from bot.messages import MessageService
from data import storage as storage_module
from data.storage import BedCooldown, Storage
from printers.manager import EventType, FilamentTray, PrinterEvent
from printers.monitor import BED_COOLDOWN_TIMEOUT, PrinterMonitor, bed_removal_temp


class RemovalTemps(unittest.TestCase):
    def test_lookup_by_prefix_case_insensitive(self):
        self.assertEqual(bed_removal_temp('PLA'), 35)
        self.assertEqual(bed_removal_temp('pla-cf'), 35)
        self.assertEqual(bed_removal_temp('PETG'), 40)
        self.assertEqual(bed_removal_temp('ABS'), 50)
        self.assertEqual(bed_removal_temp('PC'), 60)
        self.assertEqual(bed_removal_temp('PCTG'), 40)  # longest prefix wins over PC
        self.assertEqual(bed_removal_temp('PA-CF'), 50)
        self.assertEqual(bed_removal_temp('unknown'), 40)


class BedCooldownFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(storage_module, 'DATA_FILE', os.path.join(tmp.name, 'data.json'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.storage = Storage()
        self.storage.start_print(3, 10, '-100123')
        self.storage.claim_print(3, 42, '@claimer')

        self.printer = Mock()
        self.printer.get_state.return_value = GcodeState.FINISH
        self.printer.get_bed_temperature.return_value = 60.0
        self.pm = Mock()
        self.pm.get_online_printer.return_value = self.printer
        self.pm.filament.return_value = []
        self.pm.get_camera_frame = AsyncMock(return_value=None)
        self.bot = Mock(send_message=AsyncMock(), send_photo=AsyncMock())
        ctx = SimpleNamespace(chat_id='-100123', thread_id=None)
        self.ms = MessageService(self.bot, ctx, self.storage)
        self.monitor = PrinterMonitor(self.pm, self.ms, Mock())

    async def _finish(self):
        await self.monitor._handle_event(PrinterEvent(EventType.PRINT_FINISHED, 3, self.printer))

    def _dms(self):
        return [c.kwargs['text'] for c in self.bot.send_message.await_args_list if c.kwargs['chat_id'] == 42]

    async def test_finish_records_the_used_filament_and_dms_once_when_cool(self):
        self.monitor._used_material[3] = 'PLA'
        await self._finish()
        self.assertIsNone(self.storage.get_print(3))
        self.assertEqual(self.storage.get_bed_cooldown(3).material, 'PLA')
        self.assertEqual(Storage().get_bed_cooldown(3).user_id, 42)  # survives a restart

        await self.monitor._check_bed_cooldown(3)
        self.assertEqual(self._dms(), [])

        self.printer.get_bed_temperature.return_value = 33.2
        await self.monitor._check_bed_cooldown(3)
        await self.monitor._check_bed_cooldown(3)
        self.assertEqual(self._dms(), ["Printer 3's bed has cooled to 33 °C (PLA) — your print should come off easily now."])
        self.assertIsNone(self.storage.get_bed_cooldown(3))

    async def test_falls_back_to_the_active_tray_then_unknown(self):
        self.pm.filament.return_value = [FilamentTray(254, 'External spool', 'ABS', '', -1, active=True)]
        await self._finish()
        self.assertEqual(self.storage.get_bed_cooldown(3).material, 'ABS')

        self.storage.start_print(3, 11, '-100123')
        self.storage.claim_print(3, 42, '@claimer')
        self.pm.filament.return_value = []
        await self._finish()
        self.assertEqual(self.storage.get_bed_cooldown(3).material, 'unknown')

    async def test_unclaimed_or_opted_out_prints_are_not_watched(self):
        self.storage.set_cooldown_notify(3, False)
        await self._finish()
        self.assertIsNone(self.storage.get_bed_cooldown(3))
        self.assertFalse(self.storage.user_preferences[42].cooldown_notify)

        # The opt-out is the claimer's default for later prints
        self.storage.start_print(3, 11, '-100123')
        self.storage.claim_print(3, 42, '@claimer')
        self.assertFalse(self.storage.get_print(3).cooldown_notify)

        self.storage.start_print(3, 12, '-100123')
        await self._finish()
        self.assertIsNone(self.storage.get_bed_cooldown(3))

    async def test_new_print_or_timeout_stops_watching(self):
        await self._finish()
        self.printer.get_state.return_value = GcodeState.PREPARE
        await self.monitor._check_bed_cooldown(3)
        self.assertIsNone(self.storage.get_bed_cooldown(3))

        self.storage.start_bed_cooldown(3, 42, '@claimer', 'PLA')
        self.storage.start_print(3, 11, '-100123')
        self.assertIsNone(self.storage.get_bed_cooldown(3))

        self.storage.start_bed_cooldown(3, 42, '@claimer', 'PLA')
        self.storage.bed_cooldowns[3].finished_at = time.time() - BED_COOLDOWN_TIMEOUT - 1
        self.printer.get_state.return_value = GcodeState.FINISH
        self.printer.get_bed_temperature.return_value = 20.0
        await self.monitor._check_bed_cooldown(3)
        self.assertIsNone(self.storage.get_bed_cooldown(3))
        self.assertEqual(self._dms(), [])

    async def test_offline_printer_keeps_waiting(self):
        await self._finish()
        self.pm.get_online_printer.return_value = None
        await self.monitor._check_bed_cooldown(3)
        self.assertIsNotNone(self.storage.get_bed_cooldown(3))

    async def test_refused_dm_falls_back_to_the_main_chat(self):
        self.bot.send_message.side_effect = [Forbidden('blocked'), None]
        await self.ms.send_bed_cooldown_notification(BedCooldown(3, 42, '@claimer', 'PETG'), 39.6)
        main = self.bot.send_message.await_args_list[1].kwargs
        self.assertEqual(main['chat_id'], '-100123')
        self.assertTrue(main['text'].startswith("Printer 3's bed has cooled to 40 °C (PETG)"))
        self.assertIn('DM unavailable', main['text'])


class SettingsToggle(unittest.TestCase):
    def test_settings_message_shows_the_toggle(self):
        session = storage_module.PrintSession(10, '-100123', 3, claimed_by=42, cooldown_notify=False)
        text, keyboard = ui.settings_message(3, session)
        self.assertIn('Bed cooled notification: OFF', text)
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        self.assertIn('cooldown:3', [b.callback_data for b in buttons])


if __name__ == '__main__':
    unittest.main()
