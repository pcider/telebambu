"""Message texts and inline keyboards shared by handlers, notifications and the monitor."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from data.storage import PrintSession

HELP_TEXT = (
    "Available commands:\n"
    "/help - Show this help message\n"
    "/info [printer] - Show info about your print\n"
    "/notify [printer] <layer> - Send a one-time notification at a layer\n"
    "/notify [printer] <percent>% - Send a one-time notification at a percentage\n"
    "/notify_every [printer] layers|time|percent <number> - Send recurring camera snapshots\n"
    "/notify_every [printer] off - Disable recurring camera snapshots\n"
    "/camera [printer] - View camera image from your printer\n"
    "/light [printer] - Toggle your printer's light\n"
    "/unclaim [printer] - Unclaim your print\n\n"
    "Note: [printer] is required when you have multiple prints claimed."
)

NO_CLAIM = "You don't have an active print claimed."
SESSION_ENDED = "This print session has ended."
CLAIM_DM_PROMPT = "Where would you like to receive the finished print image?"


def _print_details(session: PrintSession) -> str:
    details = []
    if session.print_time:
        details.append(f"time: {session.print_time}")
    if session.total_layers:
        details.append(f"layers: {session.total_layers}")
    return f" ({', '.join(details)})" if details else ""


def started_text(printer_index: int, session: PrintSession) -> str:
    """Main chat text for an unclaimed print."""
    return f"Printer {printer_index + 1} has started printing.{_print_details(session)}"


def claimed_text(printer_index: int, session: PrintSession) -> str:
    """Main chat text for a claimed print."""
    return f"Printer {printer_index + 1} started by {session.claimed_username}{_print_details(session)}"


def claim_keyboard(printer_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Claim Print", callback_data=f"claim_{printer_index}")]])


def unclaim_keyboard(printer_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim_{printer_index}")]])


def start_dm_keyboard(bot_username: str, printer_index: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=claim_{printer_index}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("Start DM with bot", url=url)]])


def _unclaim_help_row(printer_index: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim_{printer_index}"),
        InlineKeyboardButton("Help", callback_data="help"),
    ]


def dm_preference_keyboard(printer_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dm_pref_{printer_index}_chat"),
            InlineKeyboardButton("Send to DM only", callback_data=f"dm_pref_{printer_index}_dm"),
        ],
        _unclaim_help_row(printer_index),
    ])


def settings_message(printer_index: int, session: PrintSession) -> tuple[str, InlineKeyboardMarkup]:
    printer_num = printer_index + 1
    destination = "main chat" if session.dm_preference == "chat" else "here privately"
    layer2_status = "ON" if session.layer2_notify else "OFF"

    text = (
        f"Settings for Printer {printer_num}:\n"
        f"- Finished image: {destination}\n"
        f"- Layer 2 notification: {layer2_status}\n\n"
        f"You can use /camera {printer_num} to check on your print while it's active."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Layer 2 Notify: {layer2_status}", callback_data=f"layer2_toggle_{printer_index}")],
        _unclaim_help_row(printer_index),
    ])
    return text, keyboard
