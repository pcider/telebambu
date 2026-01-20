from telegram.ext import Application, ContextTypes
from dataclasses import dataclass

import config as cfg


@dataclass
class BotContext:
    chat_id: str
    thread_id: str | None
    status_chat_id: str
    status_thread_id: str | None
    log_chat_id: str | None


def parse_chat_id(chat_id_str: str) -> tuple[str, str | None]:
    if '/' in chat_id_str:
        parts = chat_id_str.split('/')
        return parts[0], parts[1]
    return chat_id_str, None


def create_application() -> Application:
    return Application.builder().token(cfg.TELEGRAM_BOT_TOKEN).build()


def get_bot_context() -> BotContext:
    chat_id, thread_id = parse_chat_id(cfg.CHAT_ID)
    status_chat_id, status_thread_id = parse_chat_id(cfg.STATUS_CHAT_ID)

    return BotContext(
        chat_id=chat_id,
        thread_id=thread_id,
        status_chat_id=status_chat_id,
        status_thread_id=status_thread_id,
        log_chat_id=cfg.LOG_CHAT_ID
    )
