from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING

from telegram import Bot, InlineKeyboardMarkup, InputFile
from telegram.constants import MessageLimit, ParseMode
from telegram.error import BadRequest

from data import Storage
from data.storage import PrintSession
from . import ui
from .telegram_bot import BotContext
import config as cfg

if TYPE_CHECKING:
    from printers.manager import PrinterSnapshot

# Load error codes from JSON
_ERROR_CODES_FILE = os.path.join(os.path.dirname(__file__), '..', 'error_codes.json')
with open(_ERROR_CODES_FILE, 'r') as _f:
    ERROR_CODES: dict[str, str] = json.load(_f)

LOG_THROTTLE = 5  # seconds between messages to the log chat
FAIL_MESSAGE_TTL = 60 * 60 * 24  # delete failure notifications after 1 day


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
        self._last_log_time = 0.0
        self._log_buffer: list[str] = []
        self._background_tasks: set[asyncio.Task] = set()

    # --- Low-level helpers ---

    async def _send(self, chat_id, text: str, image: bytes | bytearray | None = None,
                    thread_id: int | None = None, reply_markup: InlineKeyboardMarkup | None = None):
        """Send a photo with caption if an image is given, otherwise a text message."""
        if image:
            return await self.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(bytes(image)),
                caption=text[:MessageLimit.CAPTION_LENGTH],
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
        return await self.bot.send_message(
            chat_id=chat_id,
            text=text[:MessageLimit.MAX_TEXT_LENGTH],
            message_thread_id=thread_id,
            reply_markup=reply_markup,
        )

    async def _send_main(self, text: str, image=None, reply_markup=None):
        return await self._send(self.ctx.chat_id, text, image, self.ctx.thread_id, reply_markup)

    async def _delete(self, chat_id, message_id: int):
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass  # Message may have already been deleted

    async def _delete_started_message(self, session: PrintSession | None):
        """Remove a print's "started printing" message to prevent spam."""
        if session:
            await self._delete(session.chat_id, session.message_id)

    def _spawn(self, coro):
        """Run a coroutine in the background, keeping a reference so it isn't garbage-collected."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # --- Print lifecycle ---

    async def send_print_started(self, printer_index: int, print_time: str, total_layers: int = 0) -> int:
        await self._delete_started_message(self.storage.get_print(printer_index))

        # Build the text from a provisional session so it matches later edits (unclaim etc.)
        draft = PrintSession(message_id=0, chat_id=self.ctx.chat_id, printer_index=printer_index,
                             print_time=print_time, total_layers=total_layers)
        msg = await self._send_main(ui.started_text(printer_index, draft), reply_markup=ui.claim_keyboard(printer_index))

        self.storage.start_print(printer_index, msg.message_id, self.ctx.chat_id, print_time, total_layers)
        return msg.message_id

    async def send_print_finished(self, printer_index: int, image: bytes | None):
        session = self.storage.get_print(printer_index)
        await self._delete_started_message(session)

        if session and session.claimed_by:
            message = f"Printer {printer_index + 1} has finished printing. ({session.claimed_username})"
        else:
            message = f"Printer {printer_index + 1} has finished printing."

        if session and session.claimed_by and session.dm_preference == "dm":
            await self._send(session.claimed_by, message, image)
        else:
            await self._send_main(message, image)

        self.storage.end_print(printer_index)

    async def send_print_failed(self, printer_index: int, error_code, image: bytes | None = None):
        """Send print failure notification to the print owner (if claimed) and bot owner.
        Deletes the 'started printing' message and cleans up the session."""
        session = self.storage.get_print(printer_index)
        await self._delete_started_message(session)

        message = f"Printer {printer_index + 1} failed!\n{lookup_error(error_code)}"
        targets = []  # (chat_id, thread_id, label)

        # Notify the print owner (claimer) based on their DM preference
        if session and session.claimed_by:
            if session.dm_preference == "dm":
                targets.append((session.claimed_by, None, 'claimer'))
            else:
                targets.append((self.ctx.chat_id, self.ctx.thread_id, 'claimer'))

        # Always notify the bot owner, avoiding a double-send if the claimer is the owner
        if not (session and session.claimed_by == cfg.OWNER_ID):
            targets.append((cfg.OWNER_ID, None, 'owner'))

        sent_messages = []
        for chat_id, thread_id, label in targets:
            try:
                msg = await self._send(chat_id, message, image, thread_id)
                sent_messages.append((chat_id, msg.message_id))
            except Exception as e:
                print(f'Failed to send fail notification to {label}: {e}')

        if session:
            self.storage.end_print(printer_index)

        # Delete fail messages after a delay so users can read them
        async def _delete_later():
            await asyncio.sleep(FAIL_MESSAGE_TTL)
            for chat_id, msg_id in sent_messages:
                await self._delete(chat_id, msg_id)

        self._spawn(_delete_later())

    # --- Claimer notifications (sent to the claimer's DM) ---

    async def _notify_claimer(self, printer_index: int, message: str, image: bytes | None):
        session = self.storage.get_print(printer_index)
        if session and session.claimed_by:
            await self._send(session.claimed_by, message, image, reply_markup=ui.unclaim_keyboard(printer_index))

    async def send_layer2_notification(self, printer_index: int, image: bytes | None = None):
        await self._notify_claimer(
            printer_index, f"Printer {printer_index + 1}: Layer 2 complete! Your print is progressing well.", image)
        self.storage.mark_layer2_notified(printer_index)

    async def send_custom_layer_notification(self, printer_index: int, image: bytes | None = None):
        session = self.storage.get_print(printer_index)
        if not session:
            return
        if session.notify_type == "percent":
            message = f"Printer {printer_index + 1}: {session.notify_original_value}% reached!"
        else:
            message = f"Printer {printer_index + 1}: Layer {session.notify_layer} reached!"
        await self._notify_claimer(printer_index, message, image)
        self.storage.mark_notify_layer_notified(printer_index)

    async def send_periodic_camera_notification(self, printer_index: int, snap: PrinterSnapshot,
                                                trigger_value: int | None = None, image: bytes | None = None):
        session = self.storage.get_print(printer_index)
        if not session:
            return
        if session.notify_every_type == "layers":
            detail = f"layer {snap.layer}/{snap.total_layers}"
        elif session.notify_every_type == "percent":
            detail = f"{snap.progress}% complete"
        else:
            detail = f"layer {snap.layer}/{snap.total_layers}, {snap.progress}% complete"

        suffix = "" if image else " Camera frame unavailable."
        await self._notify_claimer(printer_index, f"Printer {printer_index + 1}: Camera update ({detail}).{suffix}", image)
        self.storage.mark_notify_every_sent(printer_index, trigger_value)

    # --- Log chat ---

    async def log_message(self, message: str, image: bytes | None = None, stdout_only: bool = False):
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {message}')

        if stdout_only or not self.ctx.log_chat_id:
            return

        self._log_buffer.append(message)
        # Messages with an image are sent straight away so the image isn't dropped
        await self.flush_logs(image, force=image is not None)

    async def flush_logs(self, image: bytes | None = None, force: bool = False):
        """Send buffered log lines, at most once every LOG_THROTTLE seconds unless forced."""
        if not self._log_buffer:
            return
        now = time.time()
        if not force and now - self._last_log_time < LOG_THROTTLE:
            return

        text = '\n'.join(self._log_buffer)
        self._log_buffer.clear()
        self._last_log_time = now

        # Keep the most recent lines if the buffer outgrew Telegram's limits
        limit = MessageLimit.CAPTION_LENGTH if image else MessageLimit.MAX_TEXT_LENGTH
        try:
            await self._send(self.ctx.log_chat_id, text[-limit:], image, self.ctx.log_thread_id)
        except Exception as e:
            print(f'Failed to send log message: {e}')

    # --- Status message ---

    async def update_status_message(self, message: str):
        if message == self._prev_status_message:
            return

        message_id = self.storage.status_message_id
        if message_id is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=self.ctx.status_chat_id,
                    message_id=message_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                self._prev_status_message = message
                return
            except BadRequest as e:
                if 'not modified' in str(e).lower():
                    self._prev_status_message = message
                    return
                # Message was probably deleted; fall through and post a new one
                print(f'Could not edit status message, sending a new one: {e}')

        msg = await self.bot.send_message(
            chat_id=self.ctx.status_chat_id,
            text=message,
            message_thread_id=self.ctx.status_thread_id,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        self.storage.set_status_message_id(msg.message_id)
        self._prev_status_message = message
