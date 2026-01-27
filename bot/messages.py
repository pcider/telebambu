import time
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto
from telegram.constants import ParseMode

from data import Storage
from .telegram_bot import BotContext


@dataclass
class LivestreamInfo:
    message_id: int
    chat_id: int
    printer_index: int
    last_update: float = 0


class MessageService:
    def __init__(self, bot: Bot, context: BotContext, storage: Storage):
        self.bot = bot
        self.ctx = context
        self.storage = storage
        self._prev_status_message = ''
        self._last_log_time = 0
        self._message_buffer = ''
        # Track active livestream per printer: printer_index -> LivestreamInfo
        self._active_livestreams: dict[int, LivestreamInfo] = {}

    def format_print_time(self, total_mins: int) -> str:
        hrs = total_mins // 60
        mins = total_mins % 60
        return f'{hrs}h{mins}m' if hrs > 0 else f'{mins}m'

    async def send_print_started(self, printer_index: int, print_time: str, total_layers: int = 0) -> int:
        # Delete previous "started printing" message for this printer to prevent spam
        old_session = self.storage.get_print(printer_index)
        if old_session:
            try:
                await self.bot.delete_message(
                    chat_id=old_session.chat_id,
                    message_id=old_session.message_id
                )
            except Exception:
                pass  # Message may have already been deleted

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Claim Print", callback_data=f"claim_{printer_index}")]
        ])

        message = f"Printer {printer_index + 1} has started printing. (time: {print_time}, layers: {total_layers})"

        msg = await self.bot.send_message(
            chat_id=self.ctx.chat_id,
            text=message,
            message_thread_id=self.ctx.thread_id,
            reply_markup=keyboard
        )

        self.storage.start_print(printer_index, msg.message_id, self.ctx.chat_id, print_time)
        return msg.message_id

    async def send_print_finished(self, printer_index: int, image: bytes | bytearray | None):
        if isinstance(image, bytearray):
            image = bytes(image)

        session = self.storage.get_print(printer_index)

        # Delete the "started printing" message to prevent spam
        if session:
            try:
                await self.bot.delete_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id
                )
            except Exception:
                pass  # Message may have already been deleted

        message = f"Printer {printer_index + 1} has finished printing."

        if session and session.claimed_by:
            message = f"Printer {printer_index + 1} has finished printing. ({session.claimed_username})"

            if session.dm_preference == "dm":
                # Send to DM only
                if image:
                    await self.bot.send_photo(
                        chat_id=session.claimed_by,
                        photo=InputFile(image),
                        caption=message
                    )
                else:
                    await self.bot.send_message(
                        chat_id=session.claimed_by,
                        text=message
                    )
                # End the print session
                self.storage.end_print(printer_index)
                return

        # Send to main chat (default behavior)
        if image:
            await self.bot.send_photo(
                chat_id=self.ctx.chat_id,
                photo=InputFile(image),
                caption=message,
                message_thread_id=self.ctx.thread_id
            )
        else:
            await self.bot.send_message(
                chat_id=self.ctx.chat_id,
                text=message,
                message_thread_id=self.ctx.thread_id
            )

        self.storage.end_print(printer_index)

    async def send_layer2_notification(self, printer_index: int, image: bytes | bytearray | None = None):
        session = self.storage.get_print(printer_index)
        if not session or not session.claimed_by:
            return

        if not session.layer2_notify or session.layer2_notified:
            return

        if isinstance(image, bytearray):
            image = bytes(image)

        message = f"Printer {printer_index + 1}: Layer 2 complete! Your print is progressing well."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim_{printer_index}")]
        ])

        if image:
            await self.bot.send_photo(
                chat_id=session.claimed_by,
                photo=InputFile(image),
                caption=message,
                reply_markup=keyboard
            )
        else:
            await self.bot.send_message(
                chat_id=session.claimed_by,
                text=message,
                reply_markup=keyboard
            )

        self.storage.mark_layer2_notified(printer_index)

    async def send_custom_layer_notification(self, printer_index: int, current_layer: int, image: bytes | bytearray | None = None):
        session = self.storage.get_print(printer_index)
        if not session or not session.claimed_by:
            return

        if not session.notify_layer or session.notify_layer_notified:
            return

        if current_layer < session.notify_layer:
            return

        if isinstance(image, bytearray):
            image = bytes(image)

        # Show message based on notification type
        if session.notify_type == "percent":
            message = f"Printer {printer_index + 1}: {session.notify_original_value}% reached!"
        else:
            message = f"Printer {printer_index + 1}: Layer {session.notify_layer} reached!"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim_{printer_index}")]
        ])

        if image:
            await self.bot.send_photo(
                chat_id=session.claimed_by,
                photo=InputFile(image),
                caption=message,
                reply_markup=keyboard
            )
        else:
            await self.bot.send_message(
                chat_id=session.claimed_by,
                text=message,
                reply_markup=keyboard
            )

        self.storage.mark_notify_layer_notified(printer_index)

    async def send_update_message(self, message: str, image: bytes | bytearray | None = None):
        if isinstance(image, bytearray):
            image = bytes(image)

        if image:
            await self.bot.send_photo(
                chat_id=self.ctx.chat_id,
                photo=InputFile(image),
                caption=message,
                message_thread_id=self.ctx.thread_id
            )
        else:
            await self.bot.send_message(
                chat_id=self.ctx.chat_id,
                text=message,
                message_thread_id=self.ctx.thread_id
            )

    async def log_message(self, message: str, image: bytes | bytearray | None = None, stdout_only: bool = False):
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {message}')

        if stdout_only or not self.ctx.log_chat_id:
            return

        cur_time = time.time()
        self._message_buffer += f'\n{message}'

        if cur_time - self._last_log_time < 5:
            return

        self._last_log_time = cur_time

        if isinstance(image, bytearray):
            image = bytes(image)

        if image:
            await self.bot.send_photo(
                chat_id=self.ctx.log_chat_id,
                photo=InputFile(image),
                caption=self._message_buffer
            )
        else:
            await self.bot.send_message(
                chat_id=self.ctx.log_chat_id,
                text=self._message_buffer
            )

        self._message_buffer = ''

    async def update_status_message(self, message: str):
        if message == self._prev_status_message:
            return

        self._prev_status_message = message

        if self.storage.status_message_id is None:
            msg = await self.bot.send_message(
                chat_id=self.ctx.status_chat_id,
                text=message,
                message_thread_id=self.ctx.status_thread_id,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            self.storage.set_status_message_id(msg.message_id)
        else:
            # try:
            await self.bot.edit_message_text(
                chat_id=self.ctx.status_chat_id,
                message_id=self.storage.status_message_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            # except Exception:
                # Message might have been deleted, create a new one
                # msg = await self.bot.send_message(
                #     chat_id=self.ctx.status_chat_id,
                #     text=message,
                #     message_thread_id=self.ctx.status_thread_id,
                #     parse_mode=ParseMode.MARKDOWN_V2
                # )
                # self.storage.set_status_message_id(msg.message_id)

    async def start_livestream(self, printer_index: int, chat_id: int, image: bytes) -> int:
        """Start a new livestream for a printer. Returns the message ID."""
        # Stop any existing livestream for this printer
        await self.stop_livestream(printer_index)

        caption = f"Printer {printer_index + 1} Livestream\nUpdated: {time.strftime('%H:%M:%S')}"
        msg = await self.bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(image, filename=f"livestream_{printer_index + 1}.jpg"),
            caption=caption
        )

        self._active_livestreams[printer_index] = LivestreamInfo(
            message_id=msg.message_id,
            chat_id=chat_id,
            printer_index=printer_index,
            last_update=time.time()
        )

        return msg.message_id

    async def stop_livestream(self, printer_index: int):
        """Stop the livestream for a printer and update its caption."""
        if printer_index not in self._active_livestreams:
            return

        info = self._active_livestreams.pop(printer_index)
        try:
            await self.bot.edit_message_caption(
                chat_id=info.chat_id,
                message_id=info.message_id,
                caption=f"Printer {printer_index + 1} Livestream\nStopped at {time.strftime('%H:%M:%S')}"
            )
        except Exception:
            pass  # Message may have been deleted

    async def update_livestreams(self, get_frame_fn):
        """Update all active livestreams with new images."""
        for printer_index, info in list(self._active_livestreams.items()):
            frame = get_frame_fn(printer_index)
            if not frame:
                continue

            if isinstance(frame, bytearray):
                frame = bytes(frame)

            try:
                caption = f"Printer {printer_index + 1} Livestream\nUpdated: {time.strftime('%H:%M:%S')}"
                await self.bot.edit_message_media(
                    chat_id=info.chat_id,
                    message_id=info.message_id,
                    media=InputMediaPhoto(media=frame, caption=caption)
                )
                info.last_update = time.time()
            except Exception:
                # Message may have been deleted, remove from tracking
                self._active_livestreams.pop(printer_index, None)

    def has_active_livestream(self, printer_index: int) -> bool:
        return printer_index in self._active_livestreams
