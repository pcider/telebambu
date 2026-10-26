import asyncio
import html
import ipaddress
import math
import re
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import Callable

from telegram import Update, CallbackQuery, ForceReply, InputFile, InputMediaPhoto, MessageEntity
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          TypeHandler, filters)
from bambulabs_api import GcodeState

from data import Role, Storage, can_assign
from data.storage import PrintSession
from printers import EXTERNAL_SPOOL_TRAY, PrinterManager, SPEED_LEVELS
from printers import files
from . import ui
from .messages import MessageService
from .telegram_bot import get_bot_context, parse_chat_id, set_command_menu
from settings import DISABLEABLE_COMMANDS, LINKED_COMMANDS, PrinterConfig, Settings

NOTIFY_USAGE = "Usage: /notify [printer] <layer> or /notify [printer] <percent>%"
NOTIFY_EVERY_USAGE = "Usage: /notify_every [printer] layers <number>, time <minutes>, percent <number>, or off"
CONFIG_USAGE = ("Usage:\n"
                "/config - Show settings\n"
                "/config set <key> <value>\n"
                "/config here <chat_id|status_chat_id|log_chat_id> - Use this chat and topic\n"
                "/config disable <command> or /config enable <command>")
MIN_UPDATE_INTERVAL = 1  # seconds
OFF_VALUES = ("none", "off", "null")


def _parse_chat(raw: str) -> str:
    parse_chat_id(raw)  # raises ValueError with a message for the owner
    return raw


def _parse_optional_chat(raw: str) -> str | None:
    return None if raw.lower() in OFF_VALUES else _parse_chat(raw)


def _parse_thread(raw: str) -> int | None:
    if raw.lower() in OFF_VALUES:
        return None
    if not raw.isdigit():
        raise ValueError(f"'{raw}' is not a topic (thread) id; use a number, or none for no topic.")
    return int(raw)


def _seconds(value: float) -> float:
    return int(value) if value.is_integer() else value


def _parse_feedback_thread(raw: str) -> int | None:
    if raw.lower() in OFF_VALUES:
        return None
    if not raw.isdigit():
        raise ValueError("feedback_thread_id must be a forum topic id (a number), or 'none'.")
    return int(raw)


def _parse_update_interval(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = 0
    # nan passes a < check and makes asyncio.sleep return at once; inf is allowed (see README)
    if math.isnan(value) or value < MIN_UPDATE_INTERVAL:
        raise ValueError(f"update_interval must be a number of seconds, at least {MIN_UPDATE_INTERVAL}.")
    return _seconds(value)


def _parse_stats_log_interval(raw: str) -> float | None:
    if raw.lower() in OFF_VALUES:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = 0
    if math.isnan(value) or value <= 0:
        raise ValueError("stats_log_interval must be a positive number of seconds, or none to turn it off.")
    return _seconds(value)


def _parse_latency_warn_seconds(raw: str) -> float | None:
    if raw.lower() in OFF_VALUES:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = 0
    if not 0 < value < math.inf:  # also rejects nan
        raise ValueError("latency_warn_seconds must be a positive number of seconds, or off.")
    return _seconds(value)


def _parse_ruined_report_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = 0
    if not math.isfinite(value) or value <= 0:
        raise ValueError("ruined_report_timeout must be a positive number of seconds.")
    return _seconds(value)


def _parse_bool(raw: str, key: str) -> bool:
    value = raw.lower()
    if value in ("true", "on", "yes", "1"):
        return True
    if value in ("false", "off", "no", "0"):
        return False
    raise ValueError(f"{key} must be true or false.")


def _bool(key: str) -> Callable[[str], bool]:
    return partial(_parse_bool, key=key)


@dataclass(frozen=True)
class ConfigKey:
    """A setting /config set can change. With ``target``, it's the topic (thread) id part of
    that chat target setting instead of a Settings attribute of its own."""
    parse: Callable[[str], object]  # raises ValueError with a message for the owner
    target: str | None = None


# Settings the owner can change at runtime, in /config order; chat targets are 'chat_id' or
# 'chat_id/thread_id'. The bot token, owner_id (see /staff) and printers (see /printer) aren't here.
CONFIG_KEYS = {
    "chat_id": ConfigKey(_parse_chat),
    "thread_id": ConfigKey(_parse_thread, target="chat_id"),
    "status_chat_id": ConfigKey(_parse_chat),
    "status_thread_id": ConfigKey(_parse_thread, target="status_chat_id"),
    "log_chat_id": ConfigKey(_parse_optional_chat),
    "log_thread_id": ConfigKey(_parse_thread, target="log_chat_id"),
    # Feedback (/reportbug, /suggestfeature); feedback_thread_id is a setting of its own that
    # overrides any topic in feedback_chat_id, unlike the *_thread_id keys above
    "feedback_chat_id": ConfigKey(_parse_optional_chat),
    "feedback_thread_id": ConfigKey(_parse_feedback_thread),
    "update_interval": ConfigKey(_parse_update_interval),
    "stats_log_interval": ConfigKey(_parse_stats_log_interval),
    "latency_warn_seconds": ConfigKey(_parse_latency_warn_seconds),
    "ruined_report_timeout": ConfigKey(_parse_ruined_report_timeout),
    "auto_restart_printers": ConfigKey(_bool("auto_restart_printers")),
    "low_filament_alerts": ConfigKey(_bool("low_filament_alerts")),
    "log_commands": ConfigKey(_bool("log_commands")),
}
# Keys /config here sets to the chat and topic it's sent in
CHAT_TARGET_KEYS = ("chat_id", "status_chat_id", "log_chat_id")


def get_config_value(settings: Settings, key: str):
    target = CONFIG_KEYS[key].target
    if target is None:
        return getattr(settings, key)
    return parse_chat_id(getattr(settings, target))[1]


def set_config_value(settings: Settings, key: str, value):
    """Change a config key in memory. Raises ValueError for a topic id with no chat to go with."""
    target = CONFIG_KEYS[key].target
    if target is None:
        setattr(settings, key, value)
        return
    chat, _ = parse_chat_id(getattr(settings, target))
    if chat is None:
        raise ValueError(f"Set {target} before {key}.")
    setattr(settings, target, chat if value is None else f"{chat}/{value}")
PRINTER_USAGE = (
    "Usage:\n"
    "/printer list\n"
    "/printer add <printer> <mac> <ip> <code> <serial_no>\n"
    "/printer edit <printer> <mac|ip|code|serial_no> <value>\n"
    "/printer del <printer>"
)
# /printer edit field names -> PrinterConfig attributes
PRINTER_FIELDS = {"mac": "mac", "ip": "ip", "code": "access_code", "serial_no": "serial"}
# Minimum gap between two /beep tunes on the same printer
BEEP_COOLDOWN = 30  # seconds
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
NOTIFY_USER_USAGE = ("Usage: /notify_user <printer> <user> [user ...] to add, "
                     "/notify_user <printer> remove <user> [user ...] to remove, or /notify_user <printer> to list")
PRINT_USAGE = ("Usage: send a sliced .3mf with the caption /print <printer> [plate] [ams|ext], "
               "or reply to one with that command. Filament N is printed from AMS tray N.")
# Telegram bots can only download files up to 20 MB
MAX_PRINT_FILE_SIZE = 20 * 1024 * 1024
# States in which a printer can start a new print
READY_STATES = (GcodeState.IDLE, GcodeState.FINISH, GcodeState.FAILED)
STAFF_USAGE = "Usage: /staff list [user] or /staff edit <owner|admin|mod|user> <user>"
# /reportbug and /suggestfeature: command -> header of the message sent to the feedback chat
FEEDBACK_HEADERS = {"reportbug": "🐞 Bug report", "suggestfeature": "💡 Feature suggestion"}
MAX_FEEDBACK_LENGTH = 2000
FEEDBACK_COOLDOWN = 60  # seconds between feedback messages from one user
RUINED_CONFIRM_TTL = 5  # seconds before the "told the claimer" confirmation is deleted


@dataclass
class _RuinedQuestion:
    """A "what's wrong with the print?" question waiting for the button presser's reply (memory only)."""
    printer_num: int
    claimer_id: int
    presser_id: int
    chat_id: int | None = None
    message_id: int | None = None
    timeout: asyncio.Task | None = None
QUEUE_USAGE = "Usage: /queue <printer> to get a DM when its print ends, /queue to list, /unqueue <printer> to cancel"
# States in which a printer has no print underway, so /queue has nothing to wait for
FREE_STATES = (GcodeState.IDLE, GcodeState.FINISH, GcodeState.FAILED)
MAX_QUEUE_PER_PRINTER = 20
MAX_QUEUE_PER_USER = 10


# Buttons sent before printers were 1-based, e.g. ``claim_0`` or ``dm_pref_0_chat``
_LEGACY_CALLBACK = re.compile(r"^[a-z_]+?_(\d+)(?:_(chat|dm))?$")


def _parse_callback(query: CallbackQuery) -> tuple[int, str | None]:
    """Return ``(printer_num, option)`` from callback data such as ``claim:1`` or ``dmpref:1:chat``.

    Buttons sent before printers were 1-based used ``claim_0`` / ``dm_pref_0_chat``
    with a 0-based index; those are renumbered so old messages keep working.
    """
    legacy = _LEGACY_CALLBACK.match(query.data)
    if legacy:
        return int(legacy[1]) + 1, legacy[2]
    parts = query.data.split(":")
    return int(parts[1]), parts[2] if len(parts) > 2 else None


def _callback_pattern(action: str, legacy_action: str, option: str = "") -> str:
    legacy_option = option.replace(":", "_")
    return rf"^({action}:\d+{option}|{legacy_action}_\d+{legacy_option})$"


def _mention(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


def _logged_command(text: str) -> str:
    """A command as written to the log chat, without secrets: /printer arguments can hold
    access codes and serial numbers, so only its subcommand is kept."""
    command, _, rest = text.partition(" ")
    if command.split("@", 1)[0].lower() != "/printer":
        return text
    args = rest.replace(",", " ").split()
    if not args:
        return command
    return f"{command} {args[0]}" + (" [redacted]" if len(args) > 1 else "")


def _replied_to(message):
    """The message this one deliberately replies to, if any.

    In a forum topic, every message that isn't a real reply still "replies" to the
    topic's creation message, which would otherwise point at whoever created the topic.
    (Outside topics, message_thread_id is just the first message of a reply chain, so
    it's only compared for topic messages.)
    """
    reply_to = getattr(message, "reply_to_message", None)
    if not reply_to or reply_to.forum_topic_created:
        return None
    if message.is_topic_message and reply_to.message_id == message.message_thread_id:
        return None
    return reply_to


class BotHandlers:
    def __init__(self, storage: Storage, message_service: MessageService, printer_manager: PrinterManager,
                 settings: Settings):
        self.storage = storage
        self.ms = message_service
        self.pm = printer_manager
        self.settings = settings
        # Printer number -> time of its last /beep
        self._last_beep: dict[int, float] = {}
        # user id -> time.monotonic() of their last feedback message; in memory only
        self._last_feedback: dict[int, float] = {}
        # Printer number -> its pending ruined print question, one per printer
        self._ruined_questions: dict[int, _RuinedQuestion] = {}
        # Failure notifications follow the same disable flag (read at startup, like the command menu)
        message_service.ruined_button = not settings.is_disabled("ruined_report")

    def register(self, app: Application):
        # Runs before every other handler so @usernames can be resolved later
        app.add_handler(TypeHandler(Update, self.track_user), group=-1)
        # Its own group, as only the first matching handler in a group runs
        app.add_handler(TypeHandler(Update, self.log_command), group=-2)

        commands = {
            "start": self.cmd_start,
            "help": self.cmd_help,
            "info": self.cmd_info,
            "notify": self.cmd_notify,
            "notify_every": self.cmd_notify_every,
            "notify_user": self.cmd_notify_user,
            "stop_at": self.cmd_stop_at,
            "pause": self.cmd_pause,
            "resume": self.cmd_resume,
            "speed": self.cmd_speed,
            "skip": self.cmd_skip,
            "camera": self.cmd_camera,
            "light": self.cmd_light,
            "claim": self.cmd_claim,
            "unclaim": self.cmd_unclaim,
            "anonymous": self.cmd_anonymous,
            "forcereclaim": self.cmd_forcereclaim,
            "forceunclaim": self.cmd_forceunclaim,
            "restart": self.cmd_restart,
            "restartall": self.cmd_restartall,
            "home": self.cmd_home,
            "beep": self.cmd_beep,
            "print": self.cmd_print,
            "queue": self.cmd_queue,
            "unqueue": self.cmd_unqueue,
            "staff": self.cmd_staff,
            "reportbug": self.cmd_reportbug,
            "suggestfeature": self.cmd_suggestfeature,
            "config": self.cmd_config,
            "printer": self.cmd_printer,
        }
        for name, callback in commands.items():
            if name in DISABLEABLE_COMMANDS or name in LINKED_COMMANDS:
                callback = self._unless_disabled(name, callback)
            app.add_handler(CommandHandler(name, callback))
        # Commands in a document's caption don't reach CommandHandler
        app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/print(@\w+)?(\s|$)"),
                                       self._unless_disabled("print", self.cmd_print_caption)))

        callbacks = {
            _callback_pattern("claim", "claim"): self.cb_claim,
            _callback_pattern("dmpref", "dm_pref", ":(chat|dm)"): self.cb_dm_preference,
            _callback_pattern("layer2", "layer2_toggle"): self.cb_layer2_toggle,
            r"^cooldown:\d+$": self.cb_cooldown_toggle,
            _callback_pattern("unclaim", "unclaim"): self.cb_unclaim,
            r"^anon:\d+$": self.cb_anonymous_toggle,
            r"^cam:\d+$": self.cb_refresh_camera,
            r"^ruined:\d+:\d+$": self.cb_report_ruined,
            r"^help$": self.cb_help,
        }
        for pattern, callback in callbacks.items():
            app.add_handler(CallbackQueryHandler(callback, pattern=pattern))
        # Its own group, so replies still reach any other message handler
        app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, self.on_ruined_reply), group=1)

    def _unless_disabled(self, name: str, callback):
        """Wrap a command so it refuses to run while config.yml disables it."""
        async def guarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if self.settings.is_disabled(name):
                await update.effective_message.reply_text(f"/{name} is disabled on this bot.")
                return
            await callback(update, context)
        return guarded

    # --- Users & roles ---

    async def track_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user and not user.is_bot:
            self.storage.remember_user(user.id, user.username, user.full_name)

    async def log_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Post commands and button presses to the log chat when log_commands is on.

        Runs before the command itself, so commands that are then refused are logged too.
        """
        if not self.settings.log_commands:
            return
        try:
            await self._log_command(update, context)
        except Exception as e:
            print(f"Failed to log command: {e}")

    async def _log_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if update.callback_query:
            kind, text, message = "Button", update.callback_query.data, update.callback_query.message
        else:
            message = update.message or update.edited_message
            text = message and (message.text or message.caption)
            if not text or not text.startswith("/"):
                return
            # Commands meant for another bot in the group
            target = text.split(maxsplit=1)[0].partition("@")[2]
            if target and target.lower() != (context.bot.username or "").lower():
                return
            kind, text = "Command", _logged_command(text)
        if user is None or text is None:
            return

        chat = update.effective_chat
        if chat is None:
            where = "unknown chat"
        elif chat.type == "private":
            where = "DM"
        else:
            where = f"{chat.title} ({chat.id}"
            if message is not None and getattr(message, "is_topic_message", False):
                where += f", thread {message.message_thread_id}"
            where += ")"
        await self.ms.log_message(f"{kind}: {_mention(user)} ({user.id}) in {where}: {text}")

    def is_admin(self, user_id: int) -> bool:
        return self.storage.get_role(user_id) >= Role.ADMIN

    async def _require_admin(self, update: Update) -> bool:
        if self.is_admin(update.effective_user.id):
            return True
        await update.effective_message.reply_text("Only admins can use this command.")
        return False

    def _resolve_user(self, update: Update, token: str | None) -> int | None:
        """Find the user a command refers to: a numeric id, a known @username,
        a text mention, or (with no token) the author of the replied-to message."""
        message = update.effective_message
        if token is None:
            reply_to = _replied_to(message)
            if reply_to and reply_to.from_user and not reply_to.from_user.is_bot:
                return reply_to.from_user.id
            return None
        if token.isdigit():
            return int(token)
        if token.startswith("@"):
            return self.storage.find_user(token)
        # Users without a username are mentioned by name, which arrives as a text mention entity
        for entity in getattr(message, "entities", None) or ():
            if entity.type == MessageEntity.TEXT_MENTION and message.parse_entity(entity) == token:
                return entity.user.id
        return None

    def _unknown_user_text(self, token: str | None) -> str:
        if token is None:
            return "Please mention a user (@username or user id), or reply to one of their messages."
        return f"I don't know {token} yet. They need to send a message where the bot can see it first, or use their user id."

    # --- Shared logic ---

    async def _resolve_printer(self, update: Update, args: list[str], *, takes_value: bool = False,
                               admin_any: bool = False, usage: str = "") -> tuple[int, list[str]] | None:
        """Work out which printer a command targets, replying with an error if it can't.

        A leading number is read as the printer when the command has no value
        argument, or when a value follows it (``/notify 2 50`` vs ``/notify 50``).
        Without one, the user's single claimed printer is used. With
        ``admin_any``, admins may target any printer.

        Returns ``(printer_num, remaining_args)``, or None if a reply was sent.
        """
        reply = update.effective_message.reply_text
        user_id = update.effective_user.id
        is_admin = admin_any and self.is_admin(user_id)
        claimed = self.storage.claimed_printers(user_id)

        if not claimed and not is_admin:
            await reply(ui.NO_CLAIM)
            return None

        if args and args[0].isdigit() and (not takes_value or len(args) >= 2):
            printer_num = int(args[0])
            if printer_num not in self.pm:
                await reply(f"Invalid printer number. Use {self.pm.numbers_text()}")
                return None
            if not is_admin and printer_num not in claimed:
                claimed_list = ", ".join(str(num) for num in claimed)
                await reply(f"You haven't claimed Printer {printer_num}. Your printer(s): {claimed_list}")
                return None
            return printer_num, args[1:]

        if args and not takes_value:
            await reply("Please provide a valid printer number.")
            return None

        if len(claimed) == 1:
            return claimed[0], args

        if claimed:
            printer_list = ", ".join(str(num) for num in claimed)
            await reply(f"You have multiple prints claimed ({printer_list}). {usage}".strip())
        else:
            await reply(f"{usage}\nAvailable printers: {self.pm.numbers_text()}".strip())
        return None

    def _print_status(self, printer_num: int) -> str:
        snap = self.pm.snapshot(printer_num)
        if not snap:
            return ""
        return (
            f"\n\nCurrent status:\n- Progress: {snap.progress}%\n"
            f"- Time remaining: {snap.time_left}\n- Layer: {snap.layer}/{snap.total_layers}"
        )

    async def _send_claim_dm(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, printer_num: int):
        """DM the claimer asking where they want the finished print image. Raises if the DM fails."""
        await context.bot.send_message(
            chat_id=user_id,
            text=f"You claimed Printer {printer_num}!{self._print_status(printer_num)}\n\n{ui.CLAIM_DM_PROMPT}",
            reply_markup=ui.dm_preference_keyboard(printer_num),
        )

    async def _claim(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, printer_num: int,
                     log: bool = True) -> bool:
        """Claim an unclaimed print for a user, DM them its settings and update the main chat message.

        Returns False if the DM couldn't be delivered (the user hasn't started the bot yet),
        in which case the main chat message asks them to. ``log=False`` is for callers that log their own line.
        """
        session = self.storage.claim_print(printer_num, user_id, username)
        if log:
            await self.ms.log_event(f"Claim: {username} ({user_id}) claimed Printer {printer_num}.")
        text = ui.claimed_text(printer_num, session)
        reply_markup = None
        try:
            await self._send_claim_dm(context, user_id, printer_num)
            dm_sent = True
        except Exception:
            dm_sent = False
            name = ui.public_claimer_name(session)
            text += f"\n\n{name[:1].upper() + name[1:]}, please start a conversation with the bot to configure your print settings:"
            reply_markup = ui.start_dm_keyboard(context.bot.username, printer_num)
        if session.anonymous:
            # The main chat doesn't show who claimed it, so keep a record in the log chat
            await self.ms.log_message(f"{username} claimed Printer {printer_num} anonymously.")
        reply_markup = self._claimed_keyboard(printer_num, user_id, reply_markup)

        try:
            await context.bot.edit_message_text(chat_id=session.chat_id, message_id=session.message_id,
                                                text=text, reply_markup=reply_markup)
        except Exception as e:
            print(f'Could not update main chat message for Printer {printer_num} claim: {e}')
        return dm_sent

    def _claimed_keyboard(self, printer_num: int, claimer_id: int, keyboard=None):
        """A claimed print's main chat keyboard, with the "Report ruined print" button unless disabled."""
        if self.settings.is_disabled("ruined_report"):
            return keyboard
        return ui.with_ruined_button(printer_num, claimer_id, keyboard)

    async def _unclaim(self, context: ContextTypes.DEFAULT_TYPE, printer_num: int, log: bool = True) -> str:
        """Unclaim a print, restore the Claim button in the main chat and return a reply text."""
        old = self.storage.get_print(printer_num)
        session = self.storage.unclaim_print(printer_num)
        if log and old and old.claimed_by:
            await self.ms.log_event(f"Unclaim: {old.claimed_username} ({old.claimed_by}) unclaimed Printer {printer_num}.")
        try:
            await context.bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.message_id,
                text=ui.started_text(printer_num, session),
                reply_markup=ui.claim_keyboard(context.bot.username, printer_num),
            )
            return f"You have unclaimed Printer {printer_num}."
        except Exception:
            return f"Unclaimed Printer {printer_num}, but could not update the main chat message."

    async def _restart(self, printer_num: int) -> str:
        try:
            await self.pm.restart(printer_num)
            return f"Printer {printer_num} reconnection initiated."
        except Exception as e:
            return f"Failed to restart Printer {printer_num}: {e}"

    # --- Commands ---

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles deep links from the "Claim in DM" and "Start DM with bot" buttons."""
        user = update.effective_user
        reply = update.effective_message.reply_text
        arg = context.args[0] if context.args else ""
        # Opening the DM lets the bot message them again after a refused DM
        self.ms.mark_dm_reachable(user.id)

        queue = re.fullmatch(r"queue-(\d+)", arg)
        if queue and not self.settings.is_disabled("queue"):
            await self._queue(update, context, int(queue[1]))
            return

        match = re.fullmatch(r"claim([-_])(\d+)", arg)
        if not match:
            await reply("Welcome! Use the Claim in DM buttons in the main chat, or /claim <printer>, to claim prints.")
            return

        # "claim_N" links predate 1-based printer numbers
        printer_num = int(match[2]) + (1 if match[1] == "_" else 0)
        session = self.storage.get_print(printer_num)
        if not session:
            await reply(ui.SESSION_ENDED)
            return
        if not session.claimed_by:
            # Claiming is an explicit step in the DM, not a side effect of opening it
            status = html.escape(self._print_status(printer_num))
            await reply(f"Send <code>/claim {printer_num}</code> to claim Printer {printer_num}.{status}",
                        parse_mode=ParseMode.HTML)
            return
        if session.claimed_by != user.id:
            await reply("You are not the claimer of this print.")
            return

        await self._send_claim_dm(context, user.id, printer_num)

        # Update the main chat message to remove the "Start DM" button
        try:
            await context.bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.message_id,
                text=ui.claimed_text(printer_num, session),
                reply_markup=self._claimed_keyboard(printer_num, user.id),
            )
        except Exception as e:
            print(f'Exception: could not edit main chat message {session.message_id} on /start deep link: {e}')

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(ui.help_text(self.storage.get_role(update.effective_user.id),
                                                               self.settings.disabled()))

    async def cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self._info_summary(update)
            return
        resolved = await self._resolve_printer(update, context.args, usage="Usage: /info <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        reply = update.effective_message.reply_text

        snap = self.pm.snapshot(printer_num)
        if not snap:
            await reply(f"Printer {printer_num} is not connected.")
            return

        info_text = (
            f"Printer {printer_num} Info:\n"
            f"- Status: {snap.state}\n"
            f"- Progress: {snap.progress}%\n"
            f"- Time remaining: {snap.time_left}\n"
            f"- Layer: {snap.layer}/{snap.total_layers}\n"
        )
        details = self.pm.details(printer_num)
        if details:
            if details.file_name:
                info_text += f"- File: {details.file_name}\n"
            info_text += (f"- Temperatures: nozzle {ui.temp(details.nozzle_temp)}, bed {ui.temp(details.bed_temp)}, "
                          f"chamber {ui.temp(details.chamber_temp)}\n"
                          f"- Speed: {details.speed_level} ({details.speed_percent}%)\n")
        trays = self.pm.filament(printer_num)
        if trays:
            info_text += "- Filament:\n" + "".join(
                f"  - {tray.describe()}{' (in use)' if tray.active else ''}\n" for tray in trays)

        session = self.storage.get_print(printer_num)
        if session and session.notify_layer and not session.notify_layer_notified:
            if session.notify_type == "percent":
                info_text += f"- Notification: {session.notify_original_value}% (layer {session.notify_layer})\n"
            else:
                info_text += f"- Notification: layer {session.notify_layer}\n"
        if session and session.stop_at_layer:
            info_text += f"- Stops at layer {session.stop_at_layer}\n"
        if session and session.notify_every_type:
            info_text += f"- Recurring snapshots: every {ui.every_text(session.notify_every_type, session.notify_every_value)}\n"

        await reply(info_text)

    async def _info_summary(self, update: Update):
        """One status line per print the user claimed or watches, styled like the status message."""
        user_id = update.effective_user.id
        claimed = self.storage.claimed_printers(user_id)
        watched = self.storage.watched_printers(user_id)
        if not claimed and not watched:
            await update.effective_message.reply_text(ui.NO_CLAIM)
            return

        lines = []
        for num in claimed + watched:
            session = self.storage.get_print(num)
            line = self.pm.status_line(num) if num in self.pm else f"{num}: REMOVED"
            extras = ui.session_extras(session)
            if num in watched:
                extras.insert(0, f"watching {ui.public_claimer_name(session)}'s print")
            lines.append(line + (f" [{'; '.join(extras)}]" if extras else ""))
        await update.effective_message.reply_text(ui.code_block("Your prints:", lines), parse_mode=ParseMode.MARKDOWN_V2)

    async def cmd_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set a layer or percentage to be notified at."""
        reply = update.effective_message.reply_text
        if self.storage.claimed_printers(update.effective_user.id) and not context.args:
            await reply(f"{NOTIFY_USAGE}\nExamples: /notify 50 or /notify 2 75%")
            return

        resolved = await self._resolve_printer(update, context.args, takes_value=True,
                                               usage="Usage: /notify <printer> <layer|percent%>")
        if not resolved:
            return
        printer_num, args = resolved
        if not args:
            await reply(NOTIFY_USAGE)
            return

        arg = args[0]
        if arg.endswith('%'):
            try:
                percent = int(arg[:-1])
            except ValueError:
                await reply("Please provide a valid percentage.")
                return
            if not 1 <= percent <= 100:
                await reply("Percentage must be between 1 and 100.")
                return

            # Convert percent to a target layer using the print's total layers
            snap = self.pm.snapshot(printer_num)
            if not snap:
                await reply(f"Printer {printer_num} is not connected.")
                return
            if snap.total_layers <= 0:
                await reply("Cannot determine total layers for this print.")
                return

            target_layer = max(1, (percent * snap.total_layers) // 100)
            self.storage.set_notify_layer(printer_num, target_layer, notify_type="percent", original_value=percent)
            await reply(f"You will be notified when {percent}% is reached "
                        f"(layer {target_layer}/{snap.total_layers}) on Printer {printer_num}.")
            return

        try:
            layer = int(arg)
        except ValueError:
            await reply("Please provide a valid layer number or percentage.")
            return
        if layer < 1:
            await reply("Layer must be a positive number.")
            return

        self.storage.set_notify_layer(printer_num, layer, notify_type="layer", original_value=layer)
        await reply(f"You will be notified when layer {layer} is reached on Printer {printer_num}.")

    async def cmd_notify_every(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send recurring camera snapshots during a print."""
        reply = update.effective_message.reply_text
        if self.storage.claimed_printers(update.effective_user.id) and not context.args:
            await reply(f"{NOTIFY_EVERY_USAGE}\nExamples: /notify_every layers 5, /notify_every time 30")
            return

        resolved = await self._resolve_printer(update, context.args, takes_value=True,
                                               usage="Usage: /notify_every <printer> <layers|time|percent> <number>")
        if not resolved:
            return
        printer_num, args = resolved

        if args and args[0].lower() == "off":
            self.storage.clear_notify_every(printer_num)
            await reply(f"Recurring camera notifications disabled for Printer {printer_num}.")
            return

        if len(args) != 2 or args[0].lower() not in ui.NOTIFY_EVERY_UNITS:
            await reply(NOTIFY_EVERY_USAGE)
            return

        notify_type = args[0].lower()
        try:
            value = int(args[1])
        except ValueError:
            value = 0
        if value < 1:
            await reply("The interval must be a positive whole number.")
            return
        if notify_type == "percent" and value > 100:
            await reply("Percentage interval must be between 1 and 100.")
            return

        # Start counting from the current position so we don't immediately fire
        initial_value = 0
        snap = self.pm.snapshot(printer_num)
        if snap and notify_type == "layers":
            initial_value = snap.layer // value
        elif snap and notify_type == "percent":
            initial_value = snap.progress // value

        self.storage.set_notify_every(printer_num, notify_type, value, initial_value)
        await reply(f"Recurring camera notifications set every {ui.every_text(notify_type, value)} "
                    f"on Printer {printer_num}.")

    async def _pause_or_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE, pause: bool):
        """Pause a running print or resume a paused one (claimer or admin)."""
        verb = "pause" if pause else "resume"
        resolved = await self._resolve_printer(update, context.args, admin_any=True, usage=f"Usage: /{verb} <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        reply = update.effective_message.reply_text
        printer = self.pm.get_online_printer(printer_num)
        if not printer:
            await reply(f"Printer {printer_num} is not connected.")
            return

        needed = GcodeState.RUNNING if pause else GcodeState.PAUSE
        state = printer.get_state()
        if state != needed:
            await reply(f"Printer {printer_num} can't be {verb}d while it is {state.name}.")
            return
        try:
            ok = printer.pause_print() if pause else printer.resume_print()
        except Exception as e:
            ok, error = False, e
        else:
            error = None
        if not ok:
            await reply(f"Failed to {verb} Printer {printer_num}" + (f": {error}" if error else "."))
            return

        actor = self.storage.display_name(update.effective_user.id)
        await self.ms.log_message(f"{actor} {verb}d Printer {printer_num}.")
        # Let the claimer know when someone else (an admin) paused or resumed their print
        session = self.storage.get_print(printer_num)
        if session and session.claimed_by and session.claimed_by != update.effective_user.id:
            try:
                await context.bot.send_message(chat_id=session.claimed_by, text=f"{actor} {verb}d Printer {printer_num}.")
            except Exception:
                pass
        await reply(f"Printer {printer_num} {verb}d.")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._pause_or_resume(update, context, pause=True)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._pause_or_resume(update, context, pause=False)

    async def cmd_speed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show or change a print's speed profile (claimer or admin)."""
        reply = update.effective_message.reply_text
        levels = "|".join(SPEED_LEVELS.values())
        usage = f"Usage: /speed [printer] <{levels}>"
        resolved = await self._resolve_printer(update, context.args, takes_value=True, admin_any=True, usage=usage)
        if not resolved:
            return
        printer_num, args = resolved
        printer = self.pm.get_online_printer(printer_num)
        details = self.pm.details(printer_num)
        if not printer or not details:
            await reply(f"Printer {printer_num} is not connected.")
            return
        if not args:
            await reply(f"Printer {printer_num} speed: {details.speed_level} ({details.speed_percent}%)\n{usage}")
            return

        choice = args[0].lower()
        level = int(choice) if choice.isdigit() else next((lvl for lvl, name in SPEED_LEVELS.items() if name == choice), None)
        if level not in SPEED_LEVELS or len(args) > 1:
            await reply(usage)
            return
        state = printer.get_state()
        if state not in (GcodeState.RUNNING, GcodeState.PAUSE):
            await reply(f"Printer {printer_num} isn't printing, so its speed can't be changed.")
            return
        if not self.pm.set_speed(printer_num, level):
            await reply(f"Failed to change Printer {printer_num}'s speed.")
            return
        await self.ms.log_message(f"{self.storage.display_name(update.effective_user.id)} set Printer {printer_num} "
                                  f"speed to {SPEED_LEVELS[level]}.")
        await reply(f"Printer {printer_num} speed set to {SPEED_LEVELS[level]}.")

    async def cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List the objects on the plate, or stop printing some of them (claimer or admin)."""
        reply = update.effective_message.reply_text
        usage = "Usage: /skip [printer] to list objects, /skip <printer> <object> [object ...] to skip them"
        # The printer is always the first number, so "/skip 2" can never skip an object by mistake
        resolved = await self._resolve_printer(update, context.args, admin_any=True, usage=usage)
        if not resolved:
            return
        printer_num, args = resolved
        printer = self.pm.get_online_printer(printer_num)
        if not printer:
            await reply(f"Printer {printer_num} is not connected.")
            return
        if printer.get_state() not in (GcodeState.RUNNING, GcodeState.PAUSE):
            await reply(f"Printer {printer_num} isn't printing.")
            return
        if any(not arg.isdigit() for arg in args):
            await reply(usage)
            return

        try:
            objects = await self.pm.print_objects(printer_num)
        except Exception as e:
            await reply(f"Couldn't read the objects on Printer {printer_num}: {e}")
            return
        skipped = set(self.pm.skipped_objects(printer_num))

        if not args:
            await reply(ui.objects_text(printer_num, objects, skipped))
            return

        choices = list(dict.fromkeys(int(arg) for arg in args))
        invalid = [n for n in choices if not 1 <= n <= len(objects)]
        if invalid:
            await reply(f"Invalid object number(s): {', '.join(map(str, invalid))}. Use /skip {printer_num} to list them.")
            return
        chosen = [objects[n - 1] for n in choices if objects[n - 1].identify_id not in skipped]
        if not chosen:
            await reply("Those objects are already skipped.")
            return
        if skipped | {obj.identify_id for obj in chosen} >= {obj.identify_id for obj in objects}:
            await reply("That would skip every object. Use /stop_at or stop the print instead.")
            return
        if not self.pm.skip_objects(printer_num, [obj.identify_id for obj in chosen]):
            await reply(f"Failed to skip objects on Printer {printer_num}.")
            return

        names = ", ".join(obj.name for obj in chosen)
        await self.ms.log_message(f"{self.storage.display_name(update.effective_user.id)} skipped {names} "
                                  f"on Printer {printer_num}.")
        await reply(f"Skipping {names} on Printer {printer_num}.")

    async def cmd_stop_at(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop a print automatically once it reaches a layer (claimer or admin)."""
        reply = update.effective_message.reply_text
        usage = "Usage: /stop_at [printer] <layer> or /stop_at [printer] off"
        resolved = await self._resolve_printer(update, context.args, takes_value=True, admin_any=True, usage=usage)
        if not resolved:
            return
        printer_num, args = resolved
        session = self.storage.get_print(printer_num)
        if not session:
            await reply(f"Printer {printer_num} has no active print.")
            return
        if len(args) != 1:
            current = f"Stops at layer {session.stop_at_layer}." if session.stop_at_layer else "No stop layer set."
            await reply(f"{current}\n{usage}")
            return

        if args[0].lower() == "off":
            self.storage.set_stop_at(printer_num, None)
            await reply(f"Printer {printer_num} will no longer stop early.")
            return
        if not args[0].isdigit() or int(args[0]) < 1:
            await reply("Layer must be a positive number.")
            return
        layer = int(args[0])
        snap = self.pm.snapshot(printer_num)
        if snap and snap.layer >= layer:
            await reply(f"Printer {printer_num} is already at layer {snap.layer}.")
            return
        if snap and snap.total_layers and layer > snap.total_layers:
            await reply(f"This print only has {snap.total_layers} layers.")
            return

        self.storage.set_stop_at(printer_num, layer)
        await self.ms.log_message(f"{self.storage.display_name(update.effective_user.id)} set Printer {printer_num} "
                                  f"to stop at layer {layer}.")
        await reply(f"Printer {printer_num} will stop the print when it reaches layer {layer}. "
                    f"Use /stop_at {printer_num} off to cancel.")

    async def cmd_notify_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Let the claimer or a watcher of a print add or remove other users to notify."""
        reply = update.effective_message.reply_text
        args = context.args
        if not args or not args[0].isdigit():
            await reply(NOTIFY_USER_USAGE)
            return
        printer_num = int(args[0])
        session = self.storage.get_print(printer_num)
        if not session or not session.claimed_by:
            await reply(f"Printer {printer_num} doesn't have a claimed print.")
            return

        actor = update.effective_user.id
        if actor != session.claimed_by and actor not in session.watchers and not self.is_admin(actor):
            await reply(f"Only the claimer of Printer {printer_num} or people it notifies can do that.")
            return

        tokens = args[1:]
        if not tokens and not _replied_to(update.effective_message):
            await reply(ui.watchers_text(printer_num, session, self.storage))
            return

        removing = bool(tokens) and tokens[0].lower() == "remove"
        if removing:
            tokens = tokens[1:]
        user_ids, unknown = [], []
        for token in tokens or [None]:
            user_id = self._resolve_user(update, token)
            if user_id is None:
                unknown.append(token or "(reply)")
            else:
                user_ids.append(user_id)

        lines = []
        actor_name = self.storage.display_name(actor)
        if removing:
            removed = self.storage.remove_watchers(printer_num, user_ids)
            if removed:
                lines.append(f"Stopped notifying {self._names(removed)} about Printer {printer_num}.")
            if len(removed) < len(user_ids):
                lines.append("Some of those users weren't being notified.")
        else:
            added = self.storage.add_watchers(printer_num, user_ids)
            if added:
                lines.append(f"{self._names(added)} will now be notified about Printer {printer_num}.")
            if len(added) < len(user_ids):
                lines.append("Skipped users who are already notified (or are the claimer).")
            unreachable = []
            for user_id in added:
                try:
                    await context.bot.send_message(chat_id=user_id, text=(
                        f"{actor_name} added you to Printer {printer_num}'s notifications. "
                        f"Use /notify_user {printer_num} remove {self.storage.display_name(user_id)} to stop them."))
                except Exception:
                    unreachable.append(user_id)
            if unreachable:
                lines.append(f"I can't DM {self._names(unreachable)} yet. They need to start a chat with "
                             f"@{context.bot.username} to receive notifications.")
        if unknown:
            lines.append(self._unknown_user_text(unknown[0]) if unknown == ["(reply)"]
                         else f"Unknown user(s): {', '.join(unknown)}. They need to message where the bot can see it first.")
        await reply("\n".join(lines) or NOTIFY_USER_USAGE)

    def _names(self, user_ids: list[int]) -> str:
        return ", ".join(self.storage.display_name(uid) for uid in user_ids)

    async def cmd_camera(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admins can view any printer, claimers can view their printers."""
        resolved = await self._resolve_printer(update, context.args, admin_any=True, usage="Usage: /camera <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        message = update.effective_message

        frame = await self.pm.get_camera_frame(printer_num)
        if not frame:
            await message.reply_text(f"Printer {printer_num} is not connected or has no camera frame.")
            return

        # In the claimer's DM, offer a button to swap in a fresh frame later
        session = self.storage.get_print(printer_num)
        in_claimer_dm = (message.chat.type == "private" and session is not None
                         and session.claimed_by == update.effective_user.id)
        await message.reply_photo(
            photo=InputFile(frame, filename=f"printer_{printer_num}.jpg"),
            caption=f"Camera image from Printer {printer_num}",
            reply_markup=ui.camera_keyboard(printer_num) if in_claimer_dm else None,
        )

    async def cmd_light(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle the printer light (admin or claimer)."""
        resolved = await self._resolve_printer(update, context.args, admin_any=True, usage="Usage: /light <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        reply = update.effective_message.reply_text

        if not self.pm.get_online_printer(printer_num):
            await reply(f"Printer {printer_num} is not connected.")
            return

        try:
            turn_on = not self.pm.is_light_on(printer_num)
            self.pm.set_light(printer_num, turn_on)
            await reply(f"Printer {printer_num} light turned {'ON' if turn_on else 'OFF'}.")
        except Exception as e:
            await reply(f"Failed to toggle light: {e}")

    async def cmd_claim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Claim an unclaimed print, usually in the DM opened by the "Claim in DM" button."""
        reply = update.effective_message.reply_text
        args = context.args
        if len(args) != 1 or not args[0].isdigit():
            await reply(f"Usage: /claim <printer>\nAvailable printers: {self.pm.numbers_text()}")
            return
        printer_num = int(args[0])
        if printer_num not in self.pm:
            await reply(f"Invalid printer number. Use {self.pm.numbers_text()}")
            return

        user = update.effective_user
        session = self.storage.get_print(printer_num)
        if not session:
            await reply(f"Printer {printer_num} has no active print to claim.")
            return
        if session.claimed_by == user.id:
            await reply(f"You have already claimed Printer {printer_num}.")
            return
        if session.claimed_by:
            await reply(f"Printer {printer_num} is already claimed by {ui.public_claimer_name(session)}.")
            return

        if await self._claim(context, user.id, _mention(user), printer_num):
            await reply(f"You claimed Printer {printer_num}. Check your DMs for its settings.")
        else:
            await reply(f"You claimed Printer {printer_num}. Start a conversation with the bot to configure it:",
                        reply_markup=ui.start_dm_keyboard(context.bot.username, printer_num))

    async def cmd_forcereclaim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only: hand a print to another user, whether or not it is claimed."""
        reply = update.effective_message.reply_text
        if not await self._require_admin(update):
            return
        args = context.args
        if not args or not args[0].isdigit() or len(args) > 2:
            await reply("Usage: /forcereclaim <printer> <user>")
            return
        printer_num = int(args[0])
        session = self.storage.get_print(printer_num)
        if not session:
            await reply(f"Printer {printer_num} has no active print.")
            return
        token = args[1] if len(args) > 1 else None
        new_claimer = self._resolve_user(update, token)
        if new_claimer is None:
            await reply(self._unknown_user_text(token))
            return
        if session.claimed_by == new_claimer:
            await reply(f"{ui.public_claimer_name(session)} already has Printer {printer_num}.")
            return

        old_claimer, old_name = session.claimed_by, session.claimed_username
        new_name = self.storage.display_name(new_claimer)
        self.storage.unclaim_print(printer_num)
        dm_sent = await self._claim(context, new_claimer, new_name, printer_num, log=False)

        admin_name = self.storage.display_name(update.effective_user.id)
        if old_claimer:
            try:
                new_public = ui.public_claimer_name(self.storage.get_print(printer_num))
                await context.bot.send_message(chat_id=old_claimer,
                                               text=f"{admin_name} reassigned Printer {printer_num} to {new_public}.")
            except Exception:
                pass
        await self.ms.log_message(f"Force reclaim: {admin_name} gave Printer {printer_num} "
                                  f"from {old_name or 'nobody'} to {new_name}.")
        await reply(f"Printer {printer_num} now belongs to {new_name}."
                    + ("" if dm_sent else f" They need to start a chat with @{context.bot.username} to get its notifications."))

    async def cmd_forceunclaim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only: unclaim someone else's print, so anyone can claim it again."""
        reply = update.effective_message.reply_text
        if not await self._require_admin(update):
            return
        args = context.args
        if len(args) != 1 or not args[0].isdigit():
            await reply("Usage: /forceunclaim <printer>")
            return
        printer_num = int(args[0])
        session = self.storage.get_print(printer_num)
        if not session:
            await reply(f"Printer {printer_num} has no active print.")
            return
        if not session.claimed_by:
            await reply(f"Printer {printer_num} isn't claimed.")
            return
        old_claimer, old_name = session.claimed_by, session.claimed_username
        old_public = ui.public_claimer_name(session)
        unclaimed = await self._unclaim(context, printer_num, log=False)

        admin_name = self.storage.display_name(update.effective_user.id)
        try:
            await context.bot.send_message(chat_id=old_claimer, text=f"{admin_name} unclaimed Printer {printer_num}.")
        except Exception:
            pass
        await self.ms.log_message(f"Force unclaim: {admin_name} unclaimed Printer {printer_num} from {old_name}.")
        # _unclaim's reply is worded for the claimer; keep its warning if the main chat wasn't updated
        await reply(f"Unclaimed Printer {printer_num} from {old_public}."
                    if unclaimed.startswith("You have") else unclaimed)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """One-shot DM when a printer's current print ends. Not a booking: everyone asking is told at once."""
        reply = update.effective_message.reply_text
        args = context.args
        if not args:
            waiting = self.storage.free_reminders_for(update.effective_user.id)
            if waiting:
                await reply(f"I'll message you when these printers are free: {', '.join(map(str, waiting))}. "
                            f"Cancel with /unqueue <printer>.")
            else:
                await reply(f"You have no /queue reminders.\n{QUEUE_USAGE}")
            return
        if len(args) != 1 or not args[0].isdigit():
            await reply(QUEUE_USAGE)
            return
        await self._queue(update, context, int(args[0]))

    async def _queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE, printer_num: int):
        reply = update.effective_message.reply_text
        user_id = update.effective_user.id
        if printer_num not in self.pm:
            await reply(f"Invalid printer number. Use {self.pm.numbers_text()}")
            return
        # An offline printer's state is unknown, so the reminder is kept for when it reports again
        snap = self.pm.snapshot(printer_num)
        if snap and snap.state in FREE_STATES:
            await reply(f"Printer {printer_num} is free now.")
            return
        if printer_num in self.storage.free_reminders_for(user_id):
            await reply(f"I'll already message you when Printer {printer_num}'s current print ends.")
            return
        if len(self.storage.free_reminders_for(user_id)) >= MAX_QUEUE_PER_USER:
            await reply(f"You can have at most {MAX_QUEUE_PER_USER} /queue reminders. Cancel one with /unqueue <printer>.")
            return
        if len(self.storage.free_reminders.get(printer_num, ())) >= MAX_QUEUE_PER_PRINTER:
            await reply(f"Too many people are already waiting for Printer {printer_num}.")
            return

        text = f"I'll message you when Printer {printer_num}'s current print ends."
        if update.effective_message.chat.type != "private":
            # The reminder is a DM, so check the bot can DM them before agreeing to it
            try:
                await context.bot.send_message(chat_id=user_id, text=text)
            except Exception:
                await reply("I can't DM you yet. Start a conversation with the bot to get the reminder:",
                            reply_markup=ui.queue_dm_keyboard(context.bot.username, printer_num))
                return
            text = f"{text} (sent you a DM)"
        self.storage.add_free_reminder(printer_num, user_id)
        await reply(text)

    async def cmd_unqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        reply = update.effective_message.reply_text
        args = context.args
        if len(args) != 1 or not args[0].isdigit():
            await reply("Usage: /unqueue <printer>")
            return
        printer_num = int(args[0])
        if self.storage.remove_free_reminder(printer_num, update.effective_user.id):
            await reply(f"Cancelled your reminder for Printer {printer_num}.")
        else:
            await reply(f"You don't have a reminder for Printer {printer_num}.")

    async def cmd_unclaim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unclaim the print and revert the main chat message."""
        resolved = await self._resolve_printer(update, context.args, usage="Usage: /unclaim <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        await update.effective_message.reply_text(await self._unclaim(context, printer_num))

    async def cmd_anonymous(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Hide or show the user's name on their current and future prints."""
        message = update.effective_message
        # Saying "/anonymous on" in a group would give away who it is
        if message.chat.type != "private":
            await message.reply_text("Please use /anonymous in a private chat with the bot.")
            return
        user_id = update.effective_user.id
        arg = context.args[0].lower() if context.args else ""
        if arg not in ("", "on", "off"):
            await message.reply_text("Usage: /anonymous [on|off]")
            return
        if arg:
            await self._set_anonymous(context, user_id, arg == "on")
        if self.storage.is_anonymous(user_id):
            await message.reply_text("Anonymous is ON: other users see your prints as claimed by "
                                     f"{ui.ANONYMOUS_NAME}. Admins can still see who claimed them in the log chat. "
                                     "Turn it off with /anonymous off.")
        else:
            await message.reply_text("Anonymous is OFF: your name is shown on your prints. "
                                     "Turn it on with /anonymous on.")

    async def _set_anonymous(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, enabled: bool):
        """Save the setting and update the main chat messages of the user's current claims."""
        for num in self.storage.set_anonymous(user_id, enabled):
            session = self.storage.get_print(num)
            try:
                # Keep the "Report ruined print" button
                await context.bot.edit_message_text(chat_id=session.chat_id, message_id=session.message_id,
                                                    text=ui.claimed_text(num, session),
                                                    reply_markup=self._claimed_keyboard(num, user_id))
            except Exception as e:
                print(f'Could not update main chat message for Printer {num} anonymity: {e}')
        await self.ms.log_message(f"{self.storage.display_name(user_id)} turned anonymous "
                                  f"{'on' if enabled else 'off'}.")

    async def _restart_many(self, update: Update, printer_nums: list[int]):
        """Restart printers concurrently and reply with one line per printer."""
        actor = self.storage.display_name(update.effective_user.id)
        await self.ms.log_message(f"{actor} restarted Printer(s) {', '.join(map(str, printer_nums))}.")
        results = await asyncio.gather(*(self._restart(num) for num in printer_nums))
        await update.effective_message.reply_text("\n".join(results))

    async def _parse_printer_list(self, update: Update, args: list[str], command: str) -> list[int] | None:
        """Printer numbers given to a multi-printer command, replying with usage or an error if invalid."""
        reply = update.effective_message.reply_text
        if not args:
            await reply(f"Usage: {command} <printer> [printer ...]\nAvailable printers: {self.pm.numbers_text()}")
            return None
        invalid = [arg for arg in args if not arg.isdigit() or int(arg) not in self.pm]
        if invalid:
            await reply(f"Invalid printer number(s): {', '.join(invalid)}. Use {self.pm.numbers_text()}")
            return None
        # dict.fromkeys drops repeats while keeping the order given
        return list(dict.fromkeys(int(arg) for arg in args))

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only: reboot one or more printers and reconnect."""
        if not await self._require_admin(update):
            return
        nums = await self._parse_printer_list(update, context.args, "/restart")
        if nums:
            await self._restart_many(update, nums)

    async def cmd_restartall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only: reboot every printer."""
        if not await self._require_admin(update):
            return
        await self._restart_many(update, self.pm.numbers())

    async def cmd_home(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only: home the axes of idle printers."""
        reply = update.effective_message.reply_text
        if not await self._require_admin(update):
            return
        nums = await self._parse_printer_list(update, context.args, "/home")
        if not nums:
            return

        results = []
        homed = []
        for num in nums:
            printer = self.pm.get_online_printer(num)
            if not printer:
                results.append(f"Printer {num} is not connected.")
            elif printer.get_state() in (GcodeState.RUNNING, GcodeState.PAUSE, GcodeState.PREPARE):
                # Homing moves the toolhead and bed, which would ruin a print in progress
                results.append(f"Printer {num} is busy ({printer.get_state().name}), not homing.")
            elif printer.home_printer():
                homed.append(num)
                results.append(f"Printer {num} is homing.")
            else:
                results.append(f"Failed to home Printer {num}.")
        if homed:
            await self.ms.log_message(f"{self.storage.display_name(update.effective_user.id)} homed "
                                      f"Printer(s) {', '.join(map(str, homed))}.")
        await reply("\n".join(results))

    async def cmd_beep(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Easter egg (anyone, not in /help): play a tune on an idle printer's buzzer."""
        reply = update.effective_message.reply_text
        args = context.args
        if len(args) != 1 or not args[0].isdigit():
            await reply(f"Usage: /beep <printer>\nAvailable printers: {self.pm.numbers_text()}")
            return
        printer_num = int(args[0])
        if printer_num not in self.pm:
            await reply(f"Invalid printer number. Use {self.pm.numbers_text()}")
            return

        printer = self.pm.get_online_printer(printer_num)
        if not printer:
            await reply(f"Printer {printer_num} is not connected.")
            return
        if printer.get_state() not in (GcodeState.IDLE, GcodeState.FINISH):
            await reply(f"Printer {printer_num} is busy.")
            return
        wait = BEEP_COOLDOWN - (time.time() - self._last_beep.get(printer_num, 0))
        if wait > 0:
            await reply(f"Printer {printer_num} just beeped. Try again in {math.ceil(wait)}s.")
            return

        try:
            sent = self.pm.beep(printer_num)
        except Exception as e:
            await reply(f"Failed to beep: {e}")
            return
        if not sent:
            await reply(f"Failed to beep Printer {printer_num}.")
            return
        self._last_beep[printer_num] = time.time()
        await self.ms.log_message(f"{self.storage.display_name(update.effective_user.id)} beeped Printer {printer_num}.")
        await reply(f"Beep! Printer {printer_num} is playing a tune.")

    async def cmd_print(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only (testing): print a 3MF from a replied-to document."""
        reply_to = _replied_to(update.effective_message)
        await self._print_document(update, context, context.args, reply_to.document if reply_to else None)

    async def cmd_print_caption(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only (testing): print a 3MF sent with /print in its caption."""
        message = update.effective_message
        await self._print_document(update, context, message.caption.split()[1:], message.document)

    async def _print_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str], document):
        reply = update.effective_message.reply_text
        if not await self._require_admin(update):
            return
        if not document or not args or not args[0].isdigit():
            await reply(PRINT_USAGE)
            return
        printer_num = int(args[0])
        options = [arg.lower() for arg in args[1:]]
        plate = next((int(opt) for opt in options if opt.isdigit()), None)
        use_ams = None if not {"ams", "ext"} & set(options) else "ams" in options
        if any(not (opt.isdigit() or opt in ("ams", "ext")) for opt in options):
            await reply(PRINT_USAGE)
            return

        name = document.file_name or "print.3mf"
        if not name.lower().endswith(".3mf"):
            await reply("Only sliced .3mf files can be printed.")
            return
        if document.file_size and document.file_size > MAX_PRINT_FILE_SIZE:
            await reply("That file is too big. Telegram only lets bots download files up to 20 MB.")
            return
        if printer_num not in self.pm:
            await reply(f"Invalid printer number. Use {self.pm.numbers_text()}")
            return
        printer = self.pm.get_online_printer(printer_num)
        if not printer:
            await reply(f"Printer {printer_num} is not connected.")
            return
        if printer.get_state() not in READY_STATES:
            await reply(f"Printer {printer_num} is busy ({printer.get_state().name}).")
            return

        data = bytes(await (await context.bot.get_file(document.file_id)).download_as_bytearray())
        try:
            plates = files.sliced_plates(data)
            if not plates:
                raise ValueError("this 3MF has no sliced plates. Export it with File > Export > Export plate sliced file")
            if plate is None and len(plates) > 1:
                raise ValueError(f"it has several plates ({', '.join(map(str, plates))}), add the plate number")
            plate = plate or plates[0]
            if plate not in plates:
                raise ValueError(f"plate {plate} isn't sliced in this file (sliced: {', '.join(map(str, plates))})")
            mapping = files.ams_mapping(data, plate)
        except ValueError as e:
            await reply(f"Can't print {name}: {e}.")
            return
        if use_ams is None:
            use_ams = any(tray.tray_id != EXTERNAL_SPOOL_TRAY for tray in self.pm.filament(printer_num))

        # Keep the name simple for the printer's FTP server and file browser
        filename = re.sub(r"[^\w.-]", "_", name)
        await reply(f"Uploading {name} to Printer {printer_num}...")
        try:
            await self.pm.upload_and_print(printer_num, filename, data, plate, use_ams, mapping if use_ams else [0])
        except Exception as e:
            await reply(f"Failed to start {name} on Printer {printer_num}: {e}")
            return
        source = "AMS" if use_ams else "external spool"
        await self.ms.log_message(f"{self.storage.display_name(update.effective_user.id)} started {name} "
                                  f"(plate {plate}, {source}) on Printer {printer_num}.")
        await reply(f"Started {name} (plate {plate}, {source}) on Printer {printer_num}.")

    async def cmd_staff(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List staff roles (anyone) or change someone's role (owner/admin, by rank)."""
        reply = update.effective_message.reply_text
        args = context.args
        action = args[0].lower() if args else "list"

        if action == "list":
            if len(args) > 1 or _replied_to(update.effective_message):
                token = args[1] if len(args) > 1 else None
                user_id = self._resolve_user(update, token)
                if user_id is None:
                    await reply(self._unknown_user_text(token))
                    return
                await reply(f"{self.storage.display_name(user_id)} is {self.storage.get_role(user_id).label}.")
                return
            await reply(ui.staff_list_text(self.storage))
            return

        if action != "edit" or len(args) < 2:
            await reply(STAFF_USAGE)
            return

        new_role = Role.parse(args[1])
        if new_role is None:
            await reply(f"Unknown role '{args[1]}'. Roles: owner, admin, mod, user.")
            return
        token = args[2] if len(args) > 2 else None
        target = self._resolve_user(update, token)
        if target is None:
            await reply(self._unknown_user_text(token))
            return

        actor = update.effective_user.id
        actor_role = self.storage.get_role(actor)
        current = self.storage.get_role(target)
        name = self.storage.display_name(target)
        if target == actor:
            await reply("You can't change your own role." if new_role != Role.OWNER else "You are already the owner.")
            return
        if current == new_role:
            await reply(f"{name} is already {new_role.label}.")
            return
        if not can_assign(actor_role, current, new_role):
            if actor_role == Role.ADMIN:
                await reply("Admins can only add or remove mods.")
            elif actor_role == Role.OWNER:
                await reply("You can't change that role.")
            else:
                await reply("Only the owner and admins can change roles.")
            return

        actor_name = self.storage.display_name(actor)
        if new_role == Role.OWNER:
            # Handing over ownership can't be undone by the old owner, so ask for confirmation
            if len(args) < 4 or args[3].lower() != "confirm":
                await reply(f"This makes {name} the owner and you an admin, and only they can undo it.\n"
                            f"To continue, send: /staff edit owner {args[2] if token else target} confirm")
                return
            self.storage.transfer_ownership(target)
            await self.ms.log_message(f"Role change: {actor_name} transferred ownership to {name}.")
            await reply(f"Ownership transferred to {name}. You are now an admin.")
            return

        self.storage.set_role(target, new_role)
        await self.ms.log_message(f"Role change: {actor_name} changed {name} from {current.label} to {new_role.label}.")
        await reply(f"{name} is now {new_role.label}.")

    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Owner only: show or change settings, chat targets and disabled commands."""
        message = update.effective_message
        reply = message.reply_text
        if self.storage.get_role(update.effective_user.id) != Role.OWNER:
            await reply("Only the owner can use this command.")
            return

        args = context.args
        if not args:
            values = {key: get_config_value(self.settings, key) for key in CONFIG_KEYS}
            await reply(ui.config_text(values, self.settings.disabled()))
            return
        action = args[0].lower()
        if action in ("disable", "enable") and len(args) == 2:
            await self._config_toggle_command(update, context, args[1].lower().lstrip("/"), action == "disable")
            return
        if action == "here" and len(args) == 2 and args[1].lower() in CHAT_TARGET_KEYS:
            key = args[1].lower()
            # Outside forum topics message_thread_id is a reply chain, not a topic
            thread = message.message_thread_id if message.is_topic_message else None
            raw = f"{message.chat_id}/{thread}" if thread else str(message.chat_id)
        elif action == "set" and len(args) == 3 and args[1].lower() in CONFIG_KEYS:
            key, raw = args[1].lower(), args[2]
        else:
            await reply(CONFIG_USAGE)
            return

        old = get_config_value(self.settings, key)
        try:
            value = CONFIG_KEYS[key].parse(raw)
            set_config_value(self.settings, key, value)
        except ValueError as e:
            await reply(str(e))
            return
        self.settings.save()
        # Point outgoing messages at the new chats straight away
        self.ms.ctx = get_bot_context(self.settings)
        if key in ("status_chat_id", "status_thread_id"):
            self.ms.reset_status_message()
        if key == "chat_id":
            # Admin and owner command menus are scoped to the main chat
            await self._refresh_command_menu(context)
        await self.ms.log_message(f"Config change: {self.storage.display_name(update.effective_user.id)} "
                                  f"set {key} from {old} to {value}.")
        await reply(f"{key} set to {value}.")

    async def _config_toggle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, command: str,
                                     disable: bool):
        reply = update.effective_message.reply_text
        if command not in DISABLEABLE_COMMANDS:
            await reply(f"/{command} can't be turned off. Commands that can: "
                        + ", ".join(f"/{name}" for name in DISABLEABLE_COMMANDS))
            return
        state = "disabled" if disable else "enabled"
        if self.settings.is_disabled(command) == disable:
            await reply(f"/{command} is already {state}.")
            return
        self.settings.disabled_commands[command] = disable
        self.settings.save()
        if command == "staff":
            # Same as at startup: a disabled /staff leaves the owner as the only role
            self.storage.roles_enabled = not disable
        if command == "ruined_report":
            # Failure notifications check this flag rather than settings
            self.ms.ruined_button = not disable
        await self._refresh_command_menu(context)
        await self.ms.log_message(f"Config change: {self.storage.display_name(update.effective_user.id)} "
                                  f"{state} /{command}.")
        await reply(f"/{command} {state}.")

    async def _refresh_command_menu(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await set_command_menu(context.bot, self.storage, self.settings)
        except Exception as e:
            # The setting is saved either way; the menu catches up on the next restart
            print(f"Failed to refresh command menu: {e}")

    @staticmethod
    def _printer_field_error(field: str, value: str) -> str | None:
        if field == "ip":
            try:
                ipaddress.ip_address(value)
            except ValueError:
                return f"'{value}' is not a valid IP address."
        elif field == "mac" and not MAC_RE.match(value):
            return f"'{value}' is not a valid MAC address (e.g. AA:BB:CC:DD:EE:FF)."
        elif not value:
            return f"{field} can't be empty."
        return None

    async def _forget_secret(self, update: Update):
        """Delete a command containing an access code from group chats (needs delete rights).

        Returns the reply function to use afterwards: quoting a deleted message fails, so it
        replies in the same chat and topic without quoting.
        """
        message = update.effective_message
        if message.chat.type != "private":
            try:
                await message.delete()
            except Exception:
                pass
        return partial(message.reply_text, do_quote=False)

    async def cmd_printer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin only: list, add, edit or delete printers in config.yml."""
        reply = update.effective_message.reply_text
        if not await self._require_admin(update):
            return
        # Allow "/printer add 3 mac, ip, code, serial" as well as space separated values
        args = " ".join(context.args).replace(",", " ").split()
        action = args[0].lower() if args else ""

        if action == "list":
            await reply(ui.printer_list_text(self.settings.printers, self.pm))
            return
        if action not in ("add", "edit", "del") or len(args) < 2 or not args[1].isdigit() or int(args[1]) < 1:
            await reply(PRINTER_USAGE)
            return

        num = int(args[1])
        actor = self.storage.display_name(update.effective_user.id)
        printers = self.settings.printers

        if action == "add":
            if len(args) != 6:
                await reply(PRINTER_USAGE)
                return
            if num in printers:
                await reply(f"Printer {num} already exists. Use /printer edit or /printer del.")
                return
            config = PrinterConfig(mac=args[2], ip=args[3], access_code=args[4], serial=args[5])
            for field in ("mac", "ip"):
                error = self._printer_field_error(field, getattr(config, field))
                if error:
                    await reply(error)
                    return
            reply = await self._forget_secret(update)
            printers[num] = config
            self.settings.save()
            await self.pm.add_printer(num, config, self.ms.log_message)
            await self.ms.log_message(f"Printer change: {actor} added Printer {num} ({config.ip}).")
            await reply(f"Printer {num} added ({config.ip}).")
            return

        if num not in printers:
            await reply(f"Printer {num} doesn't exist. Printers: {self.pm.numbers_text()}")
            return

        if action == "del":
            del printers[num]
            self.settings.save()
            await self.pm.remove_printer(num)
            if self.storage.end_print(num):
                await self.ms.log_message(f"Printer {num}'s active print session was closed.")
            await self.ms.log_message(f"Printer change: {actor} removed Printer {num}.")
            await reply(f"Printer {num} removed.")
            return

        # edit
        if len(args) != 4 or args[2].lower() not in PRINTER_FIELDS:
            await reply(PRINTER_USAGE)
            return
        field, value = args[2].lower(), args[3]
        error = self._printer_field_error(field, value)
        if error:
            await reply(error)
            return
        if field == "code":
            reply = await self._forget_secret(update)
        config = replace(printers[num], **{PRINTER_FIELDS[field]: value})
        printers[num] = config
        self.settings.save()
        # The MAC isn't used for the connection, so only reconnect for the other fields
        if field != "mac":
            await self.pm.replace_printer(num, config, self.ms.log_message)
        shown = "****" if field == "code" else value
        await self.ms.log_message(f"Printer change: {actor} set Printer {num} {field} to {shown}.")
        await reply(f"Printer {num} {field} set to {shown}." + (" Reconnecting..." if field != "mac" else ""))

    # --- Feedback ---

    async def cmd_reportbug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_feedback(update, context, "reportbug")

    async def cmd_suggestfeature(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_feedback(update, context, "suggestfeature")

    def _feedback_target(self) -> tuple[str | None, int | None]:
        """feedback_chat_id (with feedback_thread_id as its topic), or the log chat when unset."""
        if not self.settings.feedback_chat_id:
            return self.ms.ctx.log_chat_id, self.ms.ctx.log_thread_id
        chat_id, thread_id = parse_chat_id(self.settings.feedback_chat_id)
        return chat_id, self.settings.feedback_thread_id or thread_id

    async def _send_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE, command: str):
        """Post a /reportbug or /suggestfeature message to the feedback chat."""
        message = update.effective_message
        reply = message.reply_text
        # The text after the command, keeping its line breaks; otherwise the replied-to message's text
        parts = (message.text or "").split(None, 1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            reply_to = _replied_to(message)
            text = ((reply_to.text or reply_to.caption or "") if reply_to else "").strip()
        if not text:
            await reply(f"Usage: /{command} <text>, or reply to a message with /{command}")
            return
        if len(text) > MAX_FEEDBACK_LENGTH:
            await reply(f"That's too long ({len(text)} characters). Please keep it under {MAX_FEEDBACK_LENGTH}.")
            return

        chat_id, thread_id = self._feedback_target()
        if not chat_id:
            await reply("Feedback isn't set up on this bot.")
            return

        user = update.effective_user
        now = time.monotonic()
        last = self._last_feedback.get(user.id)
        if last is not None and now - last < FEEDBACK_COOLDOWN:
            await reply(f"Please wait {math.ceil(FEEDBACK_COOLDOWN - (now - last))} s before sending more feedback.")
            return

        chat = message.chat
        source = "private chat" if chat.type == "private" else f"{chat.title or chat.id} ({chat.id})"
        post = f"{FEEDBACK_HEADERS[command]}\nFrom: {_mention(user)} ({user.id})\nChat: {source}\n\n{text}"
        try:
            await context.bot.send_message(chat_id=chat_id, text=post, message_thread_id=thread_id)
        except Exception as e:
            print(f"Failed to send /{command} feedback: {e}")
            await reply("Sorry, your feedback couldn't be sent. Please try again later.")
            return
        self._last_feedback[user.id] = now
        await reply("Thanks, sent to the maintainers.")

    # --- Callback buttons ---

    async def cb_claim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Legacy "Claim Print" buttons on old messages: open the DM instead of claiming from the group."""
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        await query.answer(url=ui.claim_link(context.bot.username, printer_num))

    async def _claimer_session(self, query: CallbackQuery, printer_num: int):
        """Return the session if the button presser is its claimer, otherwise answer the query and return None."""
        session = self.storage.get_print(printer_num)
        if not session:
            await query.answer()
            await query.edit_message_text(ui.SESSION_ENDED)
            return None
        if session.claimed_by != query.from_user.id:
            await query.answer("You are not the claimer of this print.", show_alert=True)
            return None
        await query.answer()
        return session

    async def cb_dm_preference(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, preference = _parse_callback(query)
        if not await self._claimer_session(query, printer_num):
            return

        self.storage.set_dm_preference(printer_num, preference)
        text, keyboard = self._settings_message(printer_num)
        await query.edit_message_text(text, reply_markup=keyboard)

    async def cb_layer2_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        session = await self._claimer_session(query, printer_num)
        if not session:
            return

        self.storage.set_layer2_notify(printer_num, not session.layer2_notify)
        text, keyboard = self._settings_message(printer_num)
        await query.edit_message_text(text, reply_markup=keyboard)

    async def cb_anonymous_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        if self.settings.is_disabled("anonymous"):
            await query.answer("/anonymous is disabled on this bot.", show_alert=True)
            return
        session = await self._claimer_session(query, printer_num)
        if not session:
            return

        await self._set_anonymous(context, query.from_user.id, not session.anonymous)
        text, keyboard = self._settings_message(printer_num)
        await query.edit_message_text(text, reply_markup=keyboard)

    async def cb_cooldown_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        session = await self._claimer_session(query, printer_num)
        if not session:
            return

        self.storage.set_cooldown_notify(printer_num, not session.cooldown_notify)
        text, keyboard = self._settings_message(printer_num)
        await query.edit_message_text(text, reply_markup=keyboard)

    def _settings_message(self, printer_num: int):
        return ui.settings_message(printer_num, self.storage.get_print(printer_num),
                                   anonymous_toggle=not self.settings.is_disabled("anonymous"))

    async def cb_unclaim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        if not await self._claimer_session(query, printer_num):
            return
        reply_text = await self._unclaim(context, printer_num)
        # Unclaim buttons also appear on photo notifications, which can't be edited as text
        if query.message and query.message.text:
            await query.edit_message_text(reply_text)
        elif query.message:
            await query.message.reply_text(reply_text)

    async def cb_refresh_camera(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Replace a camera image in the claimer's DM with a fresh frame."""
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        session = self.storage.get_print(printer_num)
        if not session or session.claimed_by != query.from_user.id:
            await query.answer("You can only refresh the camera of a print you have claimed.", show_alert=True)
            return
        # Capturing takes a few seconds; answer first so the button doesn't time out
        await query.answer("Fetching a fresh image...")

        frame = await self.pm.get_camera_frame(printer_num)
        if not frame:
            await query.message.reply_text(f"Printer {printer_num} is not connected or has no camera frame.")
            return
        await query.edit_message_media(
            media=InputMediaPhoto(InputFile(frame, filename=f"printer_{printer_num}.jpg", attach=True),
                                  caption=f"Camera image from Printer {printer_num} (updated {time.strftime('%H:%M:%S')})"),
            reply_markup=query.message.reply_markup,
        )

    async def cb_report_ruined(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ask the presser what's wrong with a claimed print. Their reply is passed on to the claimer."""
        query = update.callback_query
        if self.settings.is_disabled("ruined_report"):
            await query.answer("Reporting ruined prints is disabled on this bot.", show_alert=True)
            return
        printer_num, claimer = _parse_callback(query)
        if printer_num in self._ruined_questions:
            await query.answer(f"Someone is already reporting Printer {printer_num}'s print.", show_alert=True)
            return
        presser = query.from_user
        # Reserve the printer before sending, since updates are handled concurrently
        pending = _RuinedQuestion(printer_num, int(claimer), presser.id)
        self._ruined_questions[printer_num] = pending
        await query.answer()

        # ForceReply(selective) only opens the reply for users mentioned in the text
        name = html.escape(_mention(presser))
        mention = name if presser.username else f'<a href="tg://user?id={presser.id}">{name}</a>'
        try:
            question = await query.message.reply_text(
                f"{mention}, what's wrong with Printer {printer_num}'s print? Reply to this message with the cause.",
                parse_mode=ParseMode.HTML, reply_markup=ForceReply(selective=True))
        except Exception as e:
            del self._ruined_questions[printer_num]
            print(f'Could not ask about ruined Printer {printer_num}: {e}')
            return
        pending.chat_id, pending.message_id = question.chat_id, question.message_id
        pending.timeout = asyncio.create_task(self._expire_ruined_question(pending))

    async def _expire_ruined_question(self, pending: _RuinedQuestion):
        """Delete an unanswered question after ruined_report_timeout, without telling the claimer."""
        await asyncio.sleep(self.settings.ruined_report_timeout)
        if self._ruined_questions.get(pending.printer_num) is pending:
            del self._ruined_questions[pending.printer_num]
            await self.ms._delete(pending.chat_id, pending.message_id)

    async def on_ruined_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pass the presser's reply to a ruined print question on to the claimer. Other replies are ignored."""
        message = update.effective_message
        reply_to = getattr(message, "reply_to_message", None)
        if not reply_to or not update.effective_user:
            return
        pending = next((q for q in self._ruined_questions.values()
                        if q.message_id == reply_to.message_id and q.chat_id == message.chat_id), None)
        if not pending or pending.presser_id != update.effective_user.id:
            return
        del self._ruined_questions[pending.printer_num]
        pending.timeout.cancel()

        printer_num = pending.printer_num
        cause = (message.text or message.caption or "").strip() or "no cause given"
        session = self.storage.get_print(printer_num)
        if not session or session.claimed_by != pending.claimer_id:
            # Failure messages keep the button after the session has ended
            session = PrintSession(0, message.chat_id, printer_num, claimed_by=pending.claimer_id,
                                   claimed_username=self.storage.display_name(pending.claimer_id),
                                   anonymous=self.storage.is_anonymous(pending.claimer_id))
        presser = self.storage.display_name(pending.presser_id)
        try:
            await self.ms.send_ruined_report(
                session, f"⚠️ {presser} reports your print on Printer {printer_num} is ruined: {cause}")
            # Posted in the main chat, so an anonymous claimer stays anonymous
            confirm = f"Thanks, {ui.public_claimer_name(session)} has been told about Printer {printer_num}."
        except Exception as e:
            print(f'Failed to send ruined print report for Printer {printer_num}: {e}')
            confirm = f"Couldn't reach {ui.public_claimer_name(session)} about Printer {printer_num}."
        await self.ms.log_message(f"Ruined print: {presser} reported {session.claimed_username}'s print "
                                  f"on Printer {printer_num}: {cause}")

        # Keep the main chat clean: the question, the reply (needs delete rights) and, shortly, the confirmation
        await self.ms._delete(pending.chat_id, pending.message_id)
        await self.ms._delete(message.chat_id, message.message_id)
        try:
            sent = await message.reply_text(confirm, do_quote=False)
            self.ms.delete_later(sent.chat_id, sent.message_id, RUINED_CONFIRM_TTL)
        except Exception as e:
            print(f'Could not confirm ruined print report for Printer {printer_num}: {e}')

    async def cb_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(ui.help_text(self.storage.get_role(query.from_user.id),
                                                    self.settings.disabled()))


def setup_handlers(app: Application, storage: Storage, message_service: MessageService, printer_manager: PrinterManager,
                   settings: Settings):
    BotHandlers(storage, message_service, printer_manager, settings).register(app)
