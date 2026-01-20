from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from data import Storage
import config as cfg


def setup_handlers(app: Application, storage: Storage, message_service, printer_manager=None):
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        user = query.from_user

        if data.startswith("claim_"):
            await handle_claim(query, user, storage, message_service, context)
        elif data.startswith("dm_pref_"):
            await handle_dm_preference(query, user, storage, message_service)
        elif data.startswith("layer2_off_"):
            await handle_layer2_off(query, user, storage)

    app.add_handler(CallbackQueryHandler(handle_callback))

    # Owner-only /camera command
    async def handle_camera(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != cfg.OWNER_ID:
            await update.message.reply_text("You are not authorized to use this command.")
            return

        if not printer_manager:
            await update.message.reply_text("Printer manager not available.")
            return

        if not context.args:
            await update.message.reply_text(
                f"Usage: /camera <printer_number>\n"
                f"Available printers: 1-{len(cfg.PRINTERS)}"
            )
            return

        try:
            printer_num = int(context.args[0])
            printer_index = printer_num - 1

            if printer_index < 0 or printer_index >= len(cfg.PRINTERS):
                await update.message.reply_text(f"Invalid printer number. Use 1-{len(cfg.PRINTERS)}")
                return

            frame = printer_manager.get_camera_frame(printer_index)
            if not frame:
                await update.message.reply_text(f"Printer {printer_num} is not connected or has no camera frame.")
                return

            await update.message.reply_photo(
                photo=InputFile(frame, filename=f"printer_{printer_num}.jpg"),
                caption=f"Camera image from Printer {printer_num}"
            )

        except ValueError:
            await update.message.reply_text("Please provide a valid printer number.")

    app.add_handler(CommandHandler("camera", handle_camera))


async def handle_claim(query, user, storage: Storage, message_service, context):
    data = query.data
    printer_index = int(data.split("_")[1])

    session = storage.get_print(printer_index)
    if not session:
        await query.edit_message_text("This print session has ended.")
        return

    if session.claimed_by:
        await query.answer(f"Already claimed by {session.claimed_username}", show_alert=True)
        return

    username = f"@{user.username}" if user.username else user.full_name
    session = storage.claim_print(printer_index, user.id, username)

    # Edit the original message to show who claimed it
    new_text = f"Printer {printer_index + 1} started by {username}"
    await query.edit_message_text(new_text)

    # DM the user asking for their preference
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dm_pref_{printer_index}_chat"),
            InlineKeyboardButton("Send to DM only", callback_data=f"dm_pref_{printer_index}_dm")
        ]
    ])

    await context.bot.send_message(
        chat_id=user.id,
        text=f"You claimed Printer {printer_index + 1}!\n\nWhere would you like to receive the finished print image?",
        reply_markup=keyboard
    )


async def handle_dm_preference(query, user, storage: Storage, message_service):
    data = query.data
    parts = data.split("_")
    printer_index = int(parts[2])
    preference = parts[3]  # "chat" or "dm"

    storage.set_dm_preference(printer_index, preference)

    if preference == "chat":
        await query.edit_message_text(
            f"Got it! The finished print image will be shown in the main chat.\n"
            f"You'll also get a layer 2 notification here when your print progresses."
        )
    else:
        await query.edit_message_text(
            f"Got it! The finished print image will be sent to you here privately.\n"
            f"You'll also get a layer 2 notification here when your print progresses."
        )


async def handle_layer2_off(query, user, storage: Storage):
    data = query.data
    printer_index = int(data.split("_")[2])

    storage.set_layer2_notify(printer_index, False)

    await query.edit_message_text(
        "Layer notifications turned off for this print.\n"
        "This will be remembered for your future prints."
    )
