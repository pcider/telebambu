from telegram import BotCommand, BotCommandScopeChatMember
from telegram.ext import Application, ContextTypes
from dataclasses import dataclass

from bot.commands import get_commands
from data import Role, Storage
from settings import Settings


@dataclass
class BotContext:
    chat_id: str
    thread_id: int | None
    status_chat_id: str
    status_thread_id: int | None
    log_chat_id: str | None
    log_thread_id: int | None


def parse_chat_id(chat_id_str: str | None) -> tuple[str | None, int | None]:
    """Parse ``chat_id`` or ``chat_id/message_thread_id`` configuration.

    Telegram expects ``message_thread_id`` to be an integer. Keep the chat ID
    as a string for compatibility with numeric IDs and @usernames.
    """
    if chat_id_str is None:
        return None, None

    value = str(chat_id_str).strip()
    parts = value.split('/')

    if len(parts) > 2 or not parts[0]:
        raise ValueError(
            f"Invalid Telegram chat target {chat_id_str!r}; expected "
            "'chat_id' or 'chat_id/message_thread_id'"
        )

    if len(parts) == 1:
        return parts[0], None

    if not parts[1].isdigit():
        raise ValueError(f"Invalid Telegram message thread ID: {parts[1]!r}")

    return parts[0], int(parts[1])


def create_application(settings: Settings) -> Application:
    # Concurrent updates so a slow command (e.g. /camera waiting for a frame) doesn't block others
    return Application.builder().token(settings.telegram_bot_token).concurrent_updates(True).build()


async def set_command_menu(bot, storage: Storage, settings: Settings) -> None:
    """Configure Telegram slash-command menus: user commands for everyone, plus each
    admin's and the owner's extra commands for them in the main chat."""
    disabled = settings.disabled()
    await bot.set_my_commands(_bot_commands(Role.USER, disabled))

    chat_id, _ = parse_chat_id(settings.chat_id)
    if chat_id is None:
        return
    for user_id in storage.roles:
        role = storage.get_role(user_id)
        scope = BotCommandScopeChatMember(chat_id=chat_id, user_id=user_id)
        try:
            if role >= Role.ADMIN:
                await bot.set_my_commands(_bot_commands(role, disabled), scope=scope)
            else:
                # Mods have no extra commands; also clears a menu left from before /staff was disabled
                await bot.delete_my_commands(scope=scope)
        except Exception as e:
            # e.g. the user isn't in the main chat; don't block startup over a menu
            print(f'Failed to set command menu for user {user_id}: {e}')


def _bot_commands(role: Role, disabled: frozenset[str] = frozenset()) -> list[BotCommand]:
    return [BotCommand(command.name, command.description) for command in get_commands(role, disabled)]


def get_bot_context(settings: Settings) -> BotContext:
    chat_id, thread_id = parse_chat_id(settings.chat_id)
    status_chat_id, status_thread_id = parse_chat_id(settings.status_chat_id)
    log_chat_id, log_thread_id = parse_chat_id(settings.log_chat_id)

    return BotContext(
        chat_id=chat_id,
        thread_id=thread_id,
        status_chat_id=status_chat_id,
        status_thread_id=status_thread_id,
        log_chat_id=log_chat_id,
        log_thread_id=log_thread_id
    )
