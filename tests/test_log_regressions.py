"""Offline regressions for the last three successful boots in telebambu-prod.log."""
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bambulabs_api import GcodeState
from telegram.error import BadRequest, Forbidden, TimedOut
from telegram.request import RequestData
from telegram.request._requestparameter import RequestParameter

from bot.handlers import BotHandlers
from bot.messages import MessageService
from bot.telegram_bot import BotContext
from data.storage import PrintSession
from printers.manager import _CameraRetryNoiseFilter
from printers.monitor import PrinterMonitor


class LogRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = PrintSession(10, '-100123', 7, claimed_by=42,
                                    claimed_username='@claimer', dm_preference='dm',
                                    file_name='Design_Headphone_Stand_Swing_-_Quickprint_')
        self.storage = Mock()
        self.storage.get_print.return_value = self.session
        self.storage.owner_id = 99
        self.context = BotContext('-100123', 5, '-100123', 6, None, None)
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(),
                                   delete_message=AsyncMock())
        self.ms = MessageService(self.bot, self.context, self.storage)

    async def test_refresh_upload_has_matching_multipart_reference(self):
        query = SimpleNamespace(data='cam:7', from_user=SimpleNamespace(id=42),
                                answer=AsyncMock(), edit_message_media=AsyncMock(),
                                message=SimpleNamespace(reply_markup=None))
        pm = Mock(get_camera_frame=AsyncMock(return_value=b'jpeg-frame'))
        handlers = BotHandlers(self.storage, self.ms, pm, Mock())
        await handlers.cb_refresh_camera(SimpleNamespace(callback_query=query), None)
        media = query.edit_message_media.call_args.kwargs['media']
        # Exercise PTB's real request serializer: Telegram needs the JSON URI to
        # identify the same multipart field that carries the uploaded JPEG.
        request = RequestData([RequestParameter.from_input('media', media)])
        uri = request.parameters['media']['media']
        self.assertTrue(uri.startswith('attach://'))
        upload = request.multipart_data[uri.removeprefix('attach://')]
        self.assertEqual(upload, ('printer_7.jpg', b'jpeg-frame', 'image/jpeg'))

    async def test_startup_file_suffix_preserves_claim_and_notifications(self):
        pm = Mock()
        pm.file_name.return_value = self.session.file_name + '.gcode.3mf'
        pm.print_names.return_value = (pm.file_name.return_value, 'plate_1.gcode')
        ms = Mock(storage=self.storage, log_message=AsyncMock(),
                  send_print_finished=AsyncMock(), send_print_started=AsyncMock())
        monitor = PrinterMonitor(pm, ms, Mock())
        await monitor._reconcile_on_startup(7, GcodeState.RUNNING)
        ms.send_print_finished.assert_not_awaited()
        ms.send_print_started.assert_not_awaited()
        self.assertIn('resumed tracked print', ms.log_message.call_args.args[0])

    async def test_startup_different_project_still_announces_replacement(self):
        pm = Mock()
        pm.file_name.return_value = 'Another_project.gcode.3mf'
        pm.print_names.return_value = ('Another_project.gcode.3mf', 'plate_1.gcode')
        ms = Mock(storage=self.storage, log_message=AsyncMock(),
                  send_print_finished=AsyncMock(), send_print_started=AsyncMock())
        await PrinterMonitor(pm, ms, Mock())._reconcile_on_startup(7, GcodeState.RUNNING)
        ms.send_print_finished.assert_awaited_once_with(7, None, catchup=True)
        ms.send_print_started.assert_awaited_once()

    async def _reconcile(self, subtask, gcode_file):
        pm = Mock()
        pm.print_names.return_value = (subtask, gcode_file)
        pm.file_name.return_value = subtask or gcode_file
        ms = Mock(storage=self.storage, log_message=AsyncMock(),
                  send_print_finished=AsyncMock(), send_print_started=AsyncMock())
        await PrinterMonitor(pm, ms, Mock())._reconcile_on_startup(7, GcodeState.RUNNING)
        return ms

    async def test_startup_gcode_name_stored_then_subtask_appears(self):
        self.session.file_name = self.session.gcode_file = 'plate_1.gcode'
        ms = await self._reconcile('MyProject', 'plate_1.gcode')
        ms.send_print_finished.assert_not_awaited()

    async def test_startup_subtask_stored_then_missing_after_restart(self):
        self.session.file_name = self.session.subtask_name = 'MyProject'
        self.session.gcode_file = 'plate_1.gcode'
        ms = await self._reconcile('', 'plate_1.gcode')
        ms.send_print_finished.assert_not_awaited()

    async def test_startup_same_gcode_name_different_project_is_replaced(self):
        self.session.file_name = self.session.subtask_name = 'MyProject'
        self.session.gcode_file = 'plate_1.gcode'
        ms = await self._reconcile('OtherProject', 'plate_1.gcode')
        ms.send_print_finished.assert_awaited_once_with(7, None, catchup=True)

    async def test_startup_legacy_session_matches_either_current_name(self):
        self.session.file_name = 'plate_1.gcode'  # stored before subtask/gcode names were kept
        ms = await self._reconcile('MyProject', 'plate_1.gcode')
        ms.send_print_finished.assert_not_awaited()

    async def test_chat_not_found_dm_falls_back_and_finish_cleans_up(self):
        self.bot.send_message.side_effect = [BadRequest('Chat not found'), Mock()]
        await self.ms.send_print_finished(7, None)
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], '-100123')
        self.assertIn('/start', self.bot.send_message.call_args.kwargs['text'])
        self.storage.end_print.assert_called_once_with(7)

    async def test_refused_dm_is_remembered_until_start(self):
        self.bot.send_message.side_effect = [Forbidden('blocked'), Mock(), Mock(), Mock()]
        await self.ms.send_filament_notification(7, 'Low.')
        await self.ms.send_filament_notification(7, 'Low again.')
        # The second notification skips the DM and doesn't repeat the /start note
        second = self.bot.send_message.call_args_list[2].kwargs
        self.assertEqual(second['chat_id'], '-100123')
        self.assertNotIn('/start', second['text'])
        self.ms.mark_dm_reachable(42)
        await self.ms.send_filament_notification(7, 'Low once more.')
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 42)

    async def test_periodic_camera_never_floods_main_chat(self):
        self.session.notify_every_type = 'layers'
        snap = SimpleNamespace(layer=5, total_layers=100, progress=5)
        self.bot.send_photo.side_effect = Forbidden('blocked')
        self.bot.send_message.return_value = Mock()
        await self.ms.send_periodic_camera_notification(7, snap, 1, b'jpeg')
        # Only the text note reaches the group, not the photo
        self.bot.send_photo.assert_awaited_once()
        self.assertIn('/start', self.bot.send_message.call_args.kwargs['text'])
        self.bot.send_message.reset_mock()
        await self.ms.send_periodic_camera_notification(7, snap, 2, b'jpeg')
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_forbidden_dm_falls_back_without_private_controls(self):
        self.bot.send_message.side_effect = [Forbidden("bot can't initiate conversation with a user"),
                                              SimpleNamespace(chat_id='-100123', message_id=20)]
        await self.ms.send_layer2_notification(7)
        fallback = self.bot.send_message.call_args.kwargs
        self.assertEqual(fallback['chat_id'], '-100123')
        self.assertEqual(fallback['message_thread_id'], 5)
        self.assertIsNone(fallback['reply_markup'])
        self.assertIn('@claimer', fallback['text'])
        self.assertIn('/start', fallback['text'])

    async def test_finished_forbidden_dm_still_cleans_up_session(self):
        self.bot.send_message.side_effect = [Forbidden('blocked'), Mock()]
        await self.ms.send_print_finished(7, None)
        self.storage.end_print.assert_called_once_with(7)

    async def test_failed_fallback_cleanup_uses_actual_destination(self):
        self.bot.send_message.side_effect = [Forbidden('blocked'),
                                              SimpleNamespace(chat_id='-100123', message_id=20),
                                              SimpleNamespace(chat_id=99, message_id=21)]
        # Run delayed deletion immediately, without leaving sleeping background tasks.
        tasks = []
        self.ms._spawn = tasks.append
        await self.ms.send_print_failed(7, 0)
        from unittest.mock import patch
        with patch('bot.messages.asyncio.sleep', new=AsyncMock()):
            await tasks[0]
        self.bot.delete_message.assert_any_await(chat_id='-100123', message_id=20)
        self.bot.delete_message.assert_any_await(chat_id=99, message_id=21)
        self.storage.end_print.assert_called_once_with(7)

    async def test_timeout_does_not_trigger_duplicate_delivery_in_group(self):
        self.bot.send_message.side_effect = TimedOut()
        await self.ms.send_print_finished(7, None)
        self.assertEqual(self.bot.send_message.await_count, 1)
        # The session still ends, so a later startup doesn't announce the finish again
        self.storage.end_print.assert_called_once_with(7)

    def test_camera_timeout_noise_does_not_hide_authentication_errors(self):
        noise_filter = _CameraRetryNoiseFilter()
        for message, allowed in [('Error occurred: timed out', False),
                                  ('Error occurred: authentication failed', True),
                                  ('Unexpected camera protocol error', True)]:
            record = logging.LogRecord('bambulabs_api', logging.ERROR, '', 0, message, (), None)
            self.assertEqual(noise_filter.filter(record), allowed)


if __name__ == '__main__':
    unittest.main()
