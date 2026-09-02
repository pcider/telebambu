import asyncio
import json
import os
import time
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode

from data import Storage
from .telegram_bot import BotContext
import config as cfg

# Load error codes from JSON
_ERROR_CODES_FILE = os.path.join(os.path.dirname(__file__), '..', 'error_codes.json')
with open(_ERROR_CODES_FILE, 'r') as _f:
    ERROR_CODES: dict[str, str] = json.load(_f)


def lookup_error(code) -> str:
    """Look up an error code and return a human-readable description."""
    if code is None:
        return "Unknown error"
    hex_code = f"{code:08X}" if isinstance(code, int) else str(code).replace("-", "").replace(" ", "")
    return ERROR_CODES.get(hex_code, f"Unknown error (code: {hex_code})")


class MessageService:
    def __init__(self, bot: Bot, context: BotContext, storage: Storage):
        self.bot = bot
        self.ctx = context
        self.storage = storage
        self._prev_status_message = ''
        self._last_log_time = 0
        self._message_buffer = ''

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

    async def send_print_failed(self, printer_index: int, error_code, image: bytes | bytearray | None = None):
        """Send print failure notification to the print owner (if claimed) and bot owner.
        Deletes the 'started printing' message and cleans up the session."""
        if isinstance(image, bytearray):
            image = bytes(image)

        session = self.storage.get_print(printer_index)
        error_desc = lookup_error(error_code)
        message = f"Printer {printer_index + 1} failed!\n{error_desc}"

        # Delete the "started printing" message
        if session:
            try:
                await self.bot.delete_message(
                    chat_id=session.chat_id,
                    message_id=session.message_id
                )
            except Exception:
                pass

        sent_messages = []

        # Notify the print owner (claimer) based on their DM preference
        if session and session.claimed_by:
            try:
                if session.dm_preference == "dm":
                    target_chat = session.claimed_by
                    thread_id = None
                else:
                    target_chat = self.ctx.chat_id
                    thread_id = self.ctx.thread_id

                if image:
                    msg = await self.bot.send_photo(
                        chat_id=target_chat,
                        photo=InputFile(image),
                        caption=message,
                        message_thread_id=thread_id
                    )
                else:
                    msg = await self.bot.send_message(
                        chat_id=target_chat,
                        text=message,
                        message_thread_id=thread_id
                    )
                sent_messages.append((target_chat, msg.message_id))
            except Exception as e:
                print(f'Failed to send fail notification to claimer: {e}')

        # Always notify the bot owner
        owner_id = cfg.OWNER_ID
        # Avoid double-sending if the claimer is the owner
        if not (session and session.claimed_by == owner_id):
            try:
                if image:
                    msg = await self.bot.send_photo(
                        chat_id=owner_id,
                        photo=InputFile(image),
                        caption=message
                    )
                else:
                    msg = await self.bot.send_message(
                        chat_id=owner_id,
                        text=message
                    )
                sent_messages.append((owner_id, msg.message_id))
            except Exception as e:
                print(f'Failed to send fail notification to owner: {e}')

        # End the print session
        if session:
            self.storage.end_print(printer_index)

        # Delete fail messages after a delay so users can read them
        async def _delete_later():
            await asyncio.sleep(60*60*24)  # 1 day
            for chat_id, msg_id in sent_messages:
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass

        asyncio.create_task(_delete_later())

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
                caption=self._message_buffer,
                message_thread_id=self.ctx.log_thread_id
            )
        else:
            await self.bot.send_message(
                chat_id=self.ctx.log_chat_id,
                text=self._message_buffer,
                message_thread_id=self.ctx.log_thread_id
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
