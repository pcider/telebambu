"""Message texts and inline keyboards shared by handlers, notifications and the monitor."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from data import Role
from data.storage import PrintSession, Storage

USER_HELP = (
    "Use commands in a DM with the bot. In groups they only work in the staff chats.\n\n"
    "Available commands:\n"
    "/help - Show this help message\n"
    "/info - Summary of your claimed and watched prints\n"
    "/info <printer> - Details about a print\n"
    "/claim <printer> - Claim an unclaimed print\n"
    "/unclaim [printer] - Unclaim your print\n"
    "/anonymous [on|off] - Hide your name on your prints from other users (in a DM with the bot)\n"
    "/notify [printer] <layer> - Send a one-time notification at a layer\n"
    "/notify [printer] <percent>% - Send a one-time notification at a percentage\n"
    "/notify_every [printer] layers|time|percent <number> - Send recurring camera snapshots\n"
    "/notify_every [printer] off - Disable recurring camera snapshots\n"
    "/notify_user <printer> [remove] <user> [user ...] - Notify other users about a print\n"
    "/stop_at [printer] <layer> - Stop your print at a layer (or 'off')\n"
    "/pause [printer] - Pause your print\n"
    "/resume [printer] - Resume your paused print\n"
    "/speed [printer] <silent|standard|sport|ludicrous> - Change your print's speed\n"
    "/skip [printer] - List the objects on your plate\n"
    "/skip <printer> <object> [object ...] - Stop printing those objects\n"
    "/camera [printer] - View camera image from your printer\n"
    "/light [printer] - Toggle your printer's light\n"
    "/queue <printer> - Get a DM when the printer's current print ends\n"
    "/queue - List your /queue reminders\n"
    "/unqueue <printer> - Cancel a /queue reminder\n"
    "/staff [list [user]] - See who the owner, admins and mods are\n"
    "/reportbug <text> - Report a bug to the maintainers (or reply to a message)\n"
    "/suggestfeature <text> - Suggest a feature to the maintainers (or reply to a message)\n\n"
    "Note: [printer] is required when you have multiple prints claimed. "
    "Users can be given as @username, user id, or by replying to their message."
)

ADMIN_HELP = (
    "Admin commands:\n"
    "/camera, /light, /stop_at, /pause, /resume, /speed, /skip <printer> - Work on any printer\n"
    "/forcereclaim <printer> <user> - Give a print to someone else\n"
    "/forceunclaim <printer> - Unclaim someone else's print\n"
    "/restart <printer> [printer ...] - Reboot printers\n"
    "/restartall - Reboot every printer\n"
    "/home <printer> [printer ...] - Home the axes of idle printers\n"
    "/print <printer> [plate] [ams|ext] - Print a sliced .3mf sent with (or replied to by) this command (testing)\n"
    "/printer list - Show configured printers\n"
    "/printer add <printer> <mac> <ip> <code> <serial_no> - Add a printer\n"
    "/printer edit <printer> <mac|ip|code|serial_no> <value> - Change a printer\n"
    "/printer del <printer> - Remove a printer\n"
    "/staff edit <mod|user> <user> - Add or remove mods"
)

OWNER_HELP = (
    "Owner commands:\n"
    "/staff edit <admin|mod|user> <user> - Add or remove admins and mods\n"
    "/staff edit owner <user> - Transfer ownership (you become an admin)\n"
    "/config - Show settings and disabled commands\n"
    "/config set <key> <value> - Change a setting\n"
    "/config here <chat_id|status_chat_id|log_chat_id> - Use the chat and topic you send it in\n"
    "/config disable <command> / /config enable <command> - Turn a command off or on\n"
    "/config set dm_only_commands <true|false> - Only take commands in DMs and command chats\n"
    "/config set command_chats <id,id,...|none> - Extra group chats where commands work\n"
    "/config allowhere|denyhere - Add or remove the group you send it in"
)

# Shown under the user commands unless disabled_commands turns the button off
RUINED_HELP = ("The \"Report ruined print\" button on a claimed print in the main chat tells its claimer "
               "the print has failed (spaghetti, detached, ...). Reply to the bot's question with the cause.")


def _without_disabled(text: str, disabled: frozenset[str]) -> str:
    """Help text without the lines for disabled commands."""
    for command in ("speed", "skip"):
        if command in disabled:
            text = text.replace(f", /{command}", "")
    return "\n".join(line for line in text.split("\n")
                     if line.split(" ", 1)[0].lstrip("/") not in disabled)


def help_text(role: Role, disabled: frozenset[str] = frozenset()) -> str:
    """Help listing only the commands the user's role can use."""
    sections = [USER_HELP]
    if "ruined_report" not in disabled:
        sections.append(RUINED_HELP)
    if role >= Role.ADMIN:
        sections.append(ADMIN_HELP)
    if role == Role.OWNER:
        sections.append(OWNER_HELP)
    if disabled:
        sections = [_without_disabled(section, disabled) for section in sections]
    if role > Role.USER:
        sections.append(f"Your role: {role.label}")
    return "\n\n".join(sections)


NO_CLAIM = "You don't have an active print claimed."
SESSION_ENDED = "This print session has ended."
CLAIM_DM_PROMPT = "Where would you like to receive the finished print image?"
# Marks messages about prints the bot only noticed after a restart
CATCHUP_TAG = "[catchup] "
# Shown instead of the claimer's name when they chose /anonymous
ANONYMOUS_NAME = "an anonymous user"


def public_claimer_name(session: PrintSession) -> str:
    """The claimer's name for anything other users can see. Only the log chat uses the real name."""
    if session.anonymous:
        return ANONYMOUS_NAME
    return session.claimed_username or f"user {session.claimed_by}"


def _print_details(session: PrintSession) -> str:
    details = []
    # if session.file_name:
    #     details.append(f"file: {session.file_name}")
    if session.print_time:
        details.append(f"time: {session.print_time}")
    if session.total_layers:
        details.append(f"layers: {session.total_layers}")
    return f" ({', '.join(details)})" if details else ""


def started_text(printer_num: int, session: PrintSession) -> str:
    """Main chat text for an unclaimed print."""
    tag = CATCHUP_TAG if session.catchup else ""
    return f"{tag}Printer {printer_num} has started printing.{_print_details(session)}"


def claimed_text(printer_num: int, session: PrintSession) -> str:
    """Main chat text for a claimed print."""
    tag = CATCHUP_TAG if session.catchup else ""
    return f"{tag}Printer {printer_num} started by {public_claimer_name(session)}{_print_details(session)}"


def claim_link(bot_username: str, printer_num: int) -> str:
    """Deep link that opens the bot's DM with /start claim-N."""
    return f"https://t.me/{bot_username}?start=claim-{printer_num}"


def claim_keyboard(bot_username: str, printer_num: int) -> InlineKeyboardMarkup:
    """Claiming happens in the DM, so the claimer has started the bot and can get notifications."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("Claim in DM", url=claim_link(bot_username, printer_num))]])


def unclaim_keyboard(printer_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim:{printer_num}")]])


def refresh_camera_button(printer_num: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("Refresh Camera", callback_data=f"cam:{printer_num}")


def camera_keyboard(printer_num: int, unclaim: bool = False) -> InlineKeyboardMarkup:
    """Keyboard for camera images sent to the claimer's DM."""
    row = [refresh_camera_button(printer_num)]
    if unclaim:
        row.append(InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim:{printer_num}"))
    return InlineKeyboardMarkup([row])


def with_ruined_button(printer_num: int, claimer_id: int,
                       keyboard: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    """A main chat keyboard with a row for reporting the claimer's print as ruined.
    The claimer is in the button, so it still works on failure messages after the session ended."""
    rows = list(keyboard.inline_keyboard) if keyboard else []
    rows.append([InlineKeyboardButton("⚠️ Report ruined print", callback_data=f"ruined:{printer_num}:{claimer_id}")])
    return InlineKeyboardMarkup(rows)


def start_dm_keyboard(bot_username: str, printer_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Start DM with bot", url=claim_link(bot_username, printer_num))]])


def queue_dm_keyboard(bot_username: str, printer_num: int) -> InlineKeyboardMarkup:
    """Start-DM button that sets up the /queue reminder once the DM is open."""
    url = f"https://t.me/{bot_username}?start=queue-{printer_num}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("Start DM with bot", url=url)]])


def open_dm_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Open DM", url=f"https://t.me/{bot_username}?start=")]])


def _unclaim_help_row(printer_num: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim:{printer_num}"),
        InlineKeyboardButton("Help", callback_data="help"),
    ]


def dm_preference_keyboard(printer_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dmpref:{printer_num}:chat"),
            InlineKeyboardButton("Send to DM only", callback_data=f"dmpref:{printer_num}:dm"),
        ],
        _unclaim_help_row(printer_num),
    ])


def settings_message(printer_num: int, session: PrintSession,
                     anonymous_toggle: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    """The claimer's settings. ``anonymous_toggle`` is False while /anonymous is disabled."""
    destination = "main chat" if session.dm_preference == "chat" else "here privately"
    layer2_status = "ON" if session.layer2_notify else "OFF"
    anonymous_status = "ON" if session.anonymous else "OFF"
    cooldown_status = "ON" if session.cooldown_notify else "OFF"

    text = (
        f"Settings for Printer {printer_num}:\n"
        f"- Finished image: {destination}\n"
        f"- Layer 2 notification: {layer2_status}\n"
        f"- Bed cooled notification: {cooldown_status}\n"
        f"- Anonymous (name hidden from others): {anonymous_status}\n\n"
        f"You can use /camera {printer_num} to check on your print while it's active."
    )
    rows = [[InlineKeyboardButton(f"Layer 2 Notify: {layer2_status}", callback_data=f"layer2:{printer_num}")],
            [InlineKeyboardButton(f"Bed Cooled Notify: {cooldown_status}", callback_data=f"cooldown:{printer_num}")]]
    if anonymous_toggle:
        rows.append([InlineKeyboardButton(f"Anonymous: {anonymous_status}", callback_data=f"anon:{printer_num}")])
    rows.append(_unclaim_help_row(printer_num))
    return text, InlineKeyboardMarkup(rows)


def staff_list_text(storage: Storage) -> str:
    staff = storage.staff()
    lines = ["Staff:"]
    for role in (Role.OWNER, Role.ADMIN, Role.MOD):
        names = ", ".join(sorted(storage.display_name(uid) for uid in staff[role])) or "none"
        lines.append(f"- {role.label}: {names}")
    return "\n".join(lines)


def config_text(values: dict, disabled: frozenset[str]) -> str:
    lines = ["Config (change with /config set <key> <value>, or /config here <key> in the target chat):"]
    lines += [f"- {key}: {value}" for key, value in values.items()]
    disabled_list = ", ".join(f"/{command}" for command in sorted(disabled)) or "none"
    lines.append(f"Disabled commands: {disabled_list} (change with /config disable|enable <command>)")
    return "\n".join(lines)


def printer_list_text(printers: dict, printer_manager) -> str:
    """Admin view of configured printers. Access codes are never shown."""
    if not printers:
        return "No printers configured. Add one with /printer add."
    lines = ["Printers:"]
    for num, config in sorted(printers.items()):
        online = "online" if printer_manager.get_online_printer(num) else "offline"
        lines.append(f"{num}: {config.ip} | MAC {config.mac or '-'} | SN {config.serial} | {online}")
    return "\n".join(lines)


def watchers_text(printer_num: int, session: PrintSession, storage: Storage) -> str:
    names = ", ".join(storage.display_name(uid) for uid in session.watchers)
    claimer = public_claimer_name(session)
    if not names:
        return (f"Only {claimer} is notified about Printer {printer_num}. "
                f"Add others with /notify_user {printer_num} <user>.")
    return f"Printer {printer_num} notifies {claimer} and: {names}"


# /notify_every types and the unit each one counts in
NOTIFY_EVERY_UNITS = {"layers": "layers", "time": "minutes", "percent": "%"}


def every_text(notify_type: str, value) -> str:
    """How often a /notify_every snapshot is sent, e.g. '10 layers', '5 minutes' or '10%'."""
    unit = NOTIFY_EVERY_UNITS[notify_type]
    return f"{value}{unit}" if unit == "%" else f"{value} {unit}"


def session_extras(session: PrintSession) -> list[str]:
    """Short notes about a print's pending notifications and actions."""
    extras = []
    if session.notify_layer and not session.notify_layer_notified:
        if session.notify_type == "percent":
            extras.append(f"notify at {session.notify_original_value}%")
        else:
            extras.append(f"notify at L{session.notify_layer}")
    if session.notify_every_type:
        extras.append(f"snapshot every {every_text(session.notify_every_type, session.notify_every_value)}")
    if session.stop_at_layer:
        extras.append(f"stop at L{session.stop_at_layer}")
    if session.watchers:
        extras.append(f"+{len(session.watchers)} notified")
    return extras


def code_block(title: str, lines: list[str]) -> str:
    """A MarkdownV2 title and code block, like the status message. Title must not need escaping."""
    body = "\n".join(lines).replace("\\", "\\\\").replace("`", "\\`")
    return f"{title}```c\n{body}\n```"


def temp(value) -> str:
    """Format a temperature reading, which the printer may not report."""
    return f"{value:.0f}°C" if isinstance(value, (int, float)) else "n/a"


def objects_text(printer_num: int, objects, skipped: set[int]) -> str:
    if not objects:
        return f"No objects found for Printer {printer_num}'s print."
    lines = [f"Objects on Printer {printer_num} (skip with /skip {printer_num} <number> ...):"]
    for n, obj in enumerate(objects, start=1):
        lines.append(f"{n}. {obj.name}" + (" (skipped)" if obj.identify_id in skipped else ""))
    return "\n".join(lines)
