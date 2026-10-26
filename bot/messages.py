from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from telegram import Bot, InlineKeyboardMarkup, InputFile
from telegram.constants import MessageLimit, ParseMode
from telegram.error import BadRequest, Forbidden

from data import Storage
from data.storage import PrintSession
from errors import lookup_error
from printers.manager import format_duration
from . import ui
from .telegram_bot import BotContext

if TYPE_CHECKING:
    from printers.manager import PrinterSnapshot

LOG_THROTTLE = 5  # seconds between messages to the log chat
FAIL_MESSAGE_TTL = 60 * 60 * 24  # delete failure notifications after 1 day


def _is_cancel(error_code, reason: str) -> bool:
    """Whether a FAILED state's error code is a cancel (or no error), as a requested stop gives."""
    return not error_code or reason == "Printing was cancelled."


def _ended_details(session: PrintSession | None) -> str:
    """The file, claimer and duration of an ended print, for its log chat line."""
    file_name = session.file_name if session else None
    claimer = f"{session.claimed_username} ({session.claimed_by})" if session and session.claimed_by else "nobody"
    details = f"'{file_name or 'unknown'}', claimed by {claimer}"
    if session and session.started_at:
        details += f", took {format_duration(round((time.time() - session.started_at) / 60))}"
    return details


class MessageService:
    def __init__(self, bot: Bot, context: BotContext, storage: Storage):
        self.bot = bot
        self.ctx = context
        self.storage = storage
        self._prev_status_message = ''
        self._last_log_time = 0.0
        self._log_buffer: list[str] = []
        self._background_tasks: set[asyncio.Task] = set()
        # Claimers whose DM Telegram refused; their notifications go to the main chat until they /start
        self._dm_unreachable: set[int] = set()

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

    async def _send_claimer_dm(self, session: PrintSession, text: str, image=None, reply_markup=None,
                               group_fallback: bool = True):
        """DM the claimer, posting in the main chat instead when Telegram refuses the DM.
        Returns None if nothing was sent (no fallback wanted)."""
        user_id = session.claimed_by
        if user_id not in self._dm_unreachable:
            try:
                return await self._send(user_id, text, image, reply_markup=reply_markup)
            except (Forbidden, BadRequest) as e:
                # A bot cannot start a DM; the user must /start it first (or unblock it).
                # Other BadRequests (e.g. a bad caption) would fail in the main chat too.
                if isinstance(e, BadRequest) and 'chat not found' not in str(e).lower():
                    raise
                print(f'DM to {user_id} refused, using the main chat until they /start the bot: {e}')
                self._dm_unreachable.add(user_id)
                # Don't forward DM-only camera/unclaim controls into the shared chat.
                name = session.claimed_username or f'user {user_id}'
                note = f'For {name}: DM unavailable. Open the bot and send /start, or unblock it.'
                if not group_fallback:
                    return await self._send_main(note)
                return await self._send_main(f'{text}\n{note}', image)
        # Already told to /start: skip the doomed DM and don't repeat the note
        if group_fallback:
            return await self._send_main(text, image)
        return None

    def mark_dm_reachable(self, user_id: int):
        """The user has opened a DM with the bot, so DM them again."""
        self._dm_unreachable.discard(user_id)

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

    async def send_print_started(self, printer_num: int, print_time: str, total_layers: int = 0,
                                 file_name: str | None = None, catchup: bool = False,
                                 names: tuple[str, str] = ('', '')) -> int:
        await self._delete_started_message(self.storage.get_print(printer_num))
        if not catchup:  # The startup catch-up logs its own line
            layers = f", {total_layers} layers" if total_layers else ""
            await self.log_event(f"Printer {printer_num} started printing '{file_name or 'unknown'}' "
                                 f"(est. {print_time}{layers}).")

        # Build the text from a provisional session so it matches later edits (unclaim etc.)
        draft = PrintSession(message_id=0, chat_id=self.ctx.chat_id, printer_num=printer_num,
                             print_time=print_time, total_layers=total_layers, file_name=file_name,
                             catchup=catchup)
        msg = await self._send_main(ui.started_text(printer_num, draft), reply_markup=ui.claim_keyboard(printer_num))

        subtask, gcode_file = names
        self.storage.start_print(printer_num, msg.message_id, self.ctx.chat_id, print_time, total_layers, file_name,
                                 catchup, subtask or None, gcode_file or None)
        return msg.message_id

    async def send_print_finished(self, printer_num: int, image: bytes | None, catchup: bool = False):
        session = self.storage.get_print(printer_num)
        await self._delete_started_message(session)

        # A print that ran to the end finished, even if a /stop_at stop was sent and didn't take
        message = f"Printer {printer_num} has finished printing."
        if session and session.claimed_by:
            message += f" ({session.claimed_username})"
        if catchup:
            message = ui.CATCHUP_TAG + message

        try:
            if session and session.claimed_by and session.dm_preference == "dm":
                await self._send_claimer_dm(session, message, image)
            else:
                await self._send_main(message, image)
        except Exception as e:
            # Still end the session, or a later startup catch-up would announce the finish again
            print(f'Failed to send finish notification for Printer {printer_num}: {e}')
        await self._notify_watchers(session, message, image)
        if not catchup:
            await self.log_event(f"Printer {printer_num} finished printing ({_ended_details(session)}).")

        self.storage.end_print(printer_num)

    async def send_print_failed(self, printer_num: int, error_code, image: bytes | None = None,
                                catchup: bool = False):
        """Send print failure notification to the print owner (if claimed) and bot owner.
        Deletes the 'started printing' message and cleans up the session."""
        session = self.storage.get_print(printer_num)
        await self._delete_started_message(session)

        reason = lookup_error(error_code)
        if session and session.stopped_at_layer and _is_cancel(error_code, reason):
            # Stopping a print on request ends it the same way as a failure
            message = f"Printer {printer_num} was stopped at layer {session.stopped_at_layer} as requested."
        else:
            # Includes a /stop_at stop that didn't take, followed by a real failure
            message = f"Printer {printer_num} failed!\n{reason}"
        if not catchup:
            if session and session.stopped_at_layer and _is_cancel(error_code, reason):
                outcome = f"was stopped at layer {session.stopped_at_layer} (/stop_at)"
            elif _is_cancel(error_code, reason):
                outcome = "was cancelled"
            else:
                outcome = f"failed: {reason} (code: {error_code})"
            await self.log_event(f"Printer {printer_num} {outcome} ({_ended_details(session)}).")
        if catchup:
            message = ui.CATCHUP_TAG + message
        targets = []  # (chat_id, thread_id, label)

        # Notify the print owner (claimer) based on their DM preference
        if session and session.claimed_by:
            if session.dm_preference == "dm":
                targets.append((session.claimed_by, None, 'claimer'))
            else:
                targets.append((self.ctx.chat_id, self.ctx.thread_id, 'claimer'))

        # Always notify the bot owner, avoiding a double-send if the claimer is the owner
        owner_id = self.storage.owner_id
        if owner_id and not (session and session.claimed_by == owner_id):
            targets.append((owner_id, None, 'owner'))

        # Watchers opted in to this print's notifications, so they always get a DM
        for watcher in session.watchers if session else ():
            if watcher != owner_id:
                targets.append((watcher, None, 'watcher'))

        sent_messages = []
        for chat_id, thread_id, label in targets:
            try:
                if label == 'claimer' and thread_id is None and session.dm_preference == 'dm':
                    msg = await self._send_claimer_dm(session, message, image)
                else:
                    msg = await self._send(chat_id, message, image, thread_id)
                if msg:
                    sent_messages.append((msg.chat_id, msg.message_id))
            except Exception as e:
                print(f'Failed to send fail notification to {label}: {e}')

        if session:
            self.storage.end_print(printer_num)

        # Delete fail messages after a delay so users can read them
        async def _delete_later():
            await asyncio.sleep(FAIL_MESSAGE_TTL)
            for chat_id, msg_id in sent_messages:
                await self._delete(chat_id, msg_id)

        self._spawn(_delete_later())

    # --- Claimer notifications (sent to the claimer's and watchers' DMs) ---

    async def _notify_watchers(self, session: PrintSession | None, message: str, image: bytes | None):
        """DM a print's watchers. One unreachable watcher doesn't stop the others."""
        for watcher in session.watchers if session else ():
            try:
                await self._send(watcher, message, image)
            except Exception as e:
                print(f'Failed to notify watcher {watcher} of Printer {session.printer_num}: {e}')

    async def _notify_claimer(self, printer_num: int, message: str, image: bytes | None,
                              group_fallback: bool = True):
        session = self.storage.get_print(printer_num)
        if session and session.claimed_by:
            try:
                # Camera images get a refresh button so the claimer can check again from the DM
                keyboard = ui.camera_keyboard(printer_num, unclaim=True) if image else ui.unclaim_keyboard(printer_num)
                await self._send_claimer_dm(session, message, image, reply_markup=keyboard,
                                            group_fallback=group_fallback)
            except Exception as e:
                print(f'Failed to notify claimer of Printer {printer_num}: {e}')
            await self._notify_watchers(session, message, image)

    async def send_layer2_notification(self, printer_num: int, image: bytes | None = None):
        await self._notify_claimer(
            printer_num, f"Printer {printer_num}: Layer 2 complete! Your print is progressing well.", image)
        self.storage.mark_layer2_notified(printer_num)

    async def send_filament_notification(self, printer_num: int, message: str, image: bytes | None = None):
        await self._notify_claimer(printer_num, f"Printer {printer_num}: {message}", image)

    async def send_custom_layer_notification(self, printer_num: int, image: bytes | None = None):
        session = self.storage.get_print(printer_num)
        if not session:
            return
        if session.notify_type == "percent":
            message = f"Printer {printer_num}: {session.notify_original_value}% reached!"
        else:
            message = f"Printer {printer_num}: Layer {session.notify_layer} reached!"
        await self._notify_claimer(printer_num, message, image)
        self.storage.mark_notify_layer_notified(printer_num)

    async def send_periodic_camera_notification(self, printer_num: int, snap: PrinterSnapshot,
                                                trigger_value: int | None = None, image: bytes | None = None):
        session = self.storage.get_print(printer_num)
        if not session:
            return
        if session.notify_every_type == "layers":
            detail = f"layer {snap.layer}/{snap.total_layers}"
        elif session.notify_every_type == "percent":
            detail = f"{snap.progress}% complete"
        else:
            detail = f"layer {snap.layer}/{snap.total_layers}, {snap.progress}% complete"

        suffix = "" if image else " Camera frame unavailable."
        # Frequent updates would flood the main chat, so they're only ever sent by DM
        await self._notify_claimer(printer_num, f"Printer {printer_num}: Camera update ({detail}).{suffix}", image,
                                   group_fallback=False)
        self.storage.mark_notify_every_sent(printer_num, trigger_value)

    # --- Log chat ---

    async def log_event(self, message: str):
        """Log a print or claim event. A failed log never stops the flow around it."""
        try:
            await self.log_message(message)
        except Exception as e:
            print(f'Failed to log event: {e}')

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

    def reset_status_message(self):
        """Forget the status message, e.g. after the status chat changes, so a new one is posted."""
        self.storage.set_status_message_id(None)
        self._prev_status_message = ''

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
