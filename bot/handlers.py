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
        elif data.startswith("layer2_toggle_"):
            await handle_layer2_toggle(query, user, storage)

    app.add_handler(CallbackQueryHandler(handle_callback))

    # /camera command - owner has full access, claimers can access their printer
    async def handle_camera(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        is_owner = user_id == cfg.OWNER_ID

        if not printer_manager:
            await update.message.reply_text("Printer manager not available.")
            return

        # Find which printer this user has claimed (if any)
        claimed_printer_index = None
        for idx, session in storage.active_prints.items():
            if session.claimed_by == user_id:
                claimed_printer_index = idx
                break

        if not is_owner and claimed_printer_index is None:
            await update.message.reply_text("You don't have access to any printer camera.")
            return

        if not context.args:
            if is_owner:
                await update.message.reply_text(
                    f"Usage: /camera <printer_number>\n"
                    f"Available printers: 1-{len(cfg.PRINTERS)}"
                )
            else:
                await update.message.reply_text(
                    f"Usage: /camera {claimed_printer_index + 1}\n"
                    f"You have access to Printer {claimed_printer_index + 1} while your print is active."
                )
            return

        try:
            printer_num = int(context.args[0])
            printer_index = printer_num - 1

            if printer_index < 0 or printer_index >= len(cfg.PRINTERS):
                await update.message.reply_text(f"Invalid printer number. Use 1-{len(cfg.PRINTERS)}")
                return

            # Check permission: owner can access all, claimers only their printer
            if not is_owner and printer_index != claimed_printer_index:
                await update.message.reply_text(
                    f"You only have access to Printer {claimed_printer_index + 1} while your print is active."
                )
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

    # /start command - handles deep links from "Start DM with bot" button
    async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user

        # Check if this is a deep link with claim parameter
        if context.args and context.args[0].startswith("claim_"):
            printer_index = int(context.args[0].split("_")[1])

            session = storage.get_print(printer_index)
            if not session:
                await update.message.reply_text("This print session has ended.")
                return

            # Verify this user is the one who claimed it
            if session.claimed_by != user.id:
                await update.message.reply_text("You are not the claimer of this print.")
                return

            # Show preference selection
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dm_pref_{printer_index}_chat"),
                    InlineKeyboardButton("Send to DM only", callback_data=f"dm_pref_{printer_index}_dm")
                ]
            ])

            await update.message.reply_text(
                f"You claimed Printer {printer_index + 1}!\n\nWhere would you like to receive the finished print image?",
                reply_markup=keyboard
            )

            # Update the main chat message to remove the "Start DM" button
            print_time_str = f" (print time: {session.print_time})" if session.print_time else ""
            new_text = f"Printer {printer_index + 1} started by {session.claimed_username}{print_time_str}"
            try:
                # Parse chat_id (may be in format "chat_id/thread_id")
                chat_id = session.chat_id.split("/")[0] if "/" in session.chat_id else session.chat_id
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=session.message_id,
                    text=new_text
                )
            except Exception as e:
                print(f'Exception: could not edit main chat message {session.message_id} on /start deep link: {e}')
        else:
            # Generic start message
            await update.message.reply_text(
                "Welcome! Use the buttons in the main chat to claim prints."
            )

    app.add_handler(CommandHandler("start", handle_start))


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
    print_time_str = f" (print time: {session.print_time})" if session.print_time else ""
    new_text = f"Printer {printer_index + 1} started by {username}{print_time_str}"

    # Try to DM the user asking for their preference
    dm_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dm_pref_{printer_index}_chat"),
            InlineKeyboardButton("Send to DM only", callback_data=f"dm_pref_{printer_index}_dm")
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"You claimed Printer {printer_index + 1}!\n\nWhere would you like to receive the finished print image?",
            reply_markup=dm_keyboard
        )
        await query.edit_message_text(new_text)
    except Exception:
        # User hasn't started a conversation with the bot yet
        bot_username = context.bot.username
        start_dm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start DM with bot", url=f"https://t.me/{bot_username}?start=claim_{printer_index}")]
        ])
        await query.edit_message_text(
            f"{new_text}\n\n{username}, please start a conversation with the bot to configure your print settings:",
            reply_markup=start_dm_keyboard
        )


def _build_settings_message(printer_index: int, dm_preference: str, layer2_notify: bool) -> tuple[str, InlineKeyboardMarkup]:
    printer_num = printer_index + 1

    if dm_preference == "chat":
        destination = "main chat"
    else:
        destination = "here privately"

    layer2_status = "ON" if layer2_notify else "OFF"
    layer2_btn_text = "Layer 2 Notify: ON" if layer2_notify else "Layer 2 Notify: OFF"

    text = (
        f"Settings for Printer {printer_num}:\n"
        f"- Finished image: {destination}\n"
        f"- Layer 2 notification: {layer2_status}\n\n"
        f"You can use /camera {printer_num} to check on your print while it's active."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(layer2_btn_text, callback_data=f"layer2_toggle_{printer_index}")]
    ])

    return text, keyboard


async def handle_dm_preference(query, user, storage: Storage, message_service):
    data = query.data
    parts = data.split("_")
    printer_index = int(parts[2])
    preference = parts[3]  # "chat" or "dm"

    storage.set_dm_preference(printer_index, preference)

    session = storage.get_print(printer_index)
    layer2_notify = session.layer2_notify if session else True

    text, keyboard = _build_settings_message(printer_index, preference, layer2_notify)
    await query.edit_message_text(text, reply_markup=keyboard)


async def handle_layer2_toggle(query, user, storage: Storage):
    data = query.data
    printer_index = int(data.split("_")[2])

    session = storage.get_print(printer_index)
    if not session:
        await query.edit_message_text("This print session has ended.")
        return

    # Toggle the current value
    new_value = not session.layer2_notify
    storage.set_layer2_notify(printer_index, new_value)

    text, keyboard = _build_settings_message(printer_index, session.dm_preference, new_value)
    await query.edit_message_text(text, reply_markup=keyboard)
