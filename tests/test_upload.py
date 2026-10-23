"""/upload copies a sliced 3MF to a printer without starting it."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bambulabs_api import GcodeState

from bot.handlers import BotHandlers
from data import Role
from settings import Settings


class Upload(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        storage = Mock(display_name=Mock(return_value='@admin'))
        storage.get_role.return_value = Role.ADMIN
        self.pm = Mock(upload=AsyncMock(), upload_and_print=AsyncMock())
        self.pm.__contains__ = Mock(return_value=True)
        self.handlers = BotHandlers(storage, Mock(log_message=AsyncMock()), self.pm, Mock())
        self.reply = AsyncMock()
        self.file = Mock(download_as_bytearray=AsyncMock(return_value=bytearray(b'3mf')))
        self.context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=self.file)))

    def _update(self, caption, file_name='My part (v2).3mf'):
        document = SimpleNamespace(file_name=file_name, file_size=3, file_id='f1')
        message = SimpleNamespace(caption=caption, document=document, reply_text=self.reply)
        return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1))

    async def test_uploads_without_starting(self):
        with patch('bot.handlers.files.sliced_plates', return_value=[1, 2]), \
                patch('bot.handlers.time.strftime', return_value='201026-143005') as strftime:
            await self.handlers.cmd_upload_caption(self._update('/upload 3', 'My part (v2).3MF'), self.context)
        strftime.assert_called_once_with('%d%m%y-%H%M%S')
        self.pm.upload.assert_awaited_once_with(3, 'My_part__v2_-201026-143005.3mf', b'3mf')
        self.pm.upload_and_print.assert_not_awaited()
        self.assertIn('Uploaded My_part__v2_-201026-143005.3mf to Printer 3 (sliced plates 1, 2)',
                      self.reply.call_args.args[0])

    async def test_download_failure_is_reported(self):
        self.context.bot.get_file.side_effect = RuntimeError('network down')
        await self.handlers.cmd_upload_caption(self._update('/upload 3'), self.context)
        self.assertIn("Couldn't download", self.reply.call_args.args[0])
        self.pm.upload.assert_not_awaited()

    async def test_rejects_unsliced_and_non_3mf_files(self):
        with patch('bot.handlers.files.sliced_plates', return_value=[]):
            await self.handlers.cmd_upload_caption(self._update('/upload 3'), self.context)
        self.assertIn('no sliced plates', self.reply.call_args.args[0])
        await self.handlers.cmd_upload_caption(self._update('/upload 3', 'part.stl'), self.context)
        self.assertIn('Only sliced .3mf', self.reply.call_args.args[0])
        self.pm.upload.assert_not_awaited()

    async def test_refused_while_printing_unless_forced(self):
        self.pm.get_online_printer.return_value.get_state.return_value = GcodeState.RUNNING
        with patch('bot.handlers.files.sliced_plates', return_value=[1]):
            await self.handlers.cmd_upload_caption(self._update('/upload 3'), self.context)
            self.assertIn('busy (RUNNING)', self.reply.call_args.args[0])
            self.assertIn('/upload 3 force', self.reply.call_args.args[0])
            self.pm.upload.assert_not_awaited()

            await self.handlers.cmd_upload_caption(self._update('/upload 3 FORCE'), self.context)
        self.pm.upload.assert_awaited_once()
        self.assertIn('(forced while RUNNING)', self.handlers.ms.log_message.call_args.args[0])

    async def test_upload_flag_disables_it(self):
        settings = Settings(telegram_bot_token='x', chat_id='1', status_chat_id='1')
        settings.disabled_commands['upload'] = True
        self.handlers.settings = settings
        await self.handlers._unless_disabled('upload', self.handlers.cmd_upload_caption)(
            self._update('/upload 3'), self.context)
        self.reply.assert_awaited_once_with('/upload is disabled on this bot.')
        self.pm.upload.assert_not_awaited()

    async def test_plate_or_filament_options_are_rejected(self):
        await self.handlers.cmd_upload_caption(self._update('/upload 3 2 ams'), self.context)
        self.assertIn('Usage: send a sliced .3mf with the caption /upload', self.reply.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
