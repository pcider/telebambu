from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from data import Storage
import config as cfg


def _get_claimed_printers(storage: Storage, user_id: int) -> list[int]:
    """Get list of printer indices claimed by a user."""
    return [idx for idx, session in storage.active_prints.items() if session.claimed_by == user_id]


def _resolve_printer(storage: Storage, user_id: int, args: list, require_claim: bool = True) -> tuple[int | None, str | None]:
    """
    Resolve which printer to use based on user's claimed printers and optional argument.
    Returns (printer_index, error_message). If error_message is set, printer_index is None.
    """
    claimed = _get_claimed_printers(storage, user_id)

    if require_claim and not claimed:
        return None, "You don't have an active print claimed."

    # If printer number provided as argument
    if args:
        try:
            printer_num = int(args[0])
            printer_index = printer_num - 1
            if printer_index < 0 or printer_index >= len(cfg.PRINTERS):
                return None, f"Invalid printer number. Use 1-{len(cfg.PRINTERS)}"
            if require_claim and printer_index not in claimed:
                return None, f"You haven't claimed Printer {printer_num}."
            return printer_index, None
        except ValueError:
            return None, None  # Not a number, might be another argument

    # No argument provided
    if len(claimed) == 1:
        return claimed[0], None
    elif len(claimed) > 1:
        printer_list = ", ".join(str(idx + 1) for idx in claimed)
        return None, f"You have multiple prints claimed ({printer_list}). Please specify the printer number."

    return None, "You don't have an active print claimed."


def setup_handlers(app: Application, storage: Storage, message_service, printer_manager=None):
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        user = query.from_user

        if data.startswith("claim_"):
            await handle_claim(query, user, storage, message_service, context, printer_manager)
        elif data.startswith("dm_pref_"):
            await handle_dm_preference(query, user, storage, message_service)
        elif data.startswith("layer2_toggle_"):
            await handle_layer2_toggle(query, user, storage)
        elif data.startswith("unclaim_"):
            await handle_unclaim_callback(query, user, storage, context)
        elif data.startswith("restart_printer_"):
            await handle_restart_printer(query, user, printer_manager)
        elif data == "help":
            await handle_help_callback(query)

    app.add_handler(CallbackQueryHandler(handle_callback))

    # /camera command - owner has full access, claimers can access their printers
    async def handle_camera(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        is_owner = user_id == cfg.OWNER_ID

        if not printer_manager:
            await update.message.reply_text("Printer manager not available.")
            return

        claimed = _get_claimed_printers(storage, user_id)

        if not is_owner and not claimed:
            await update.message.reply_text("You don't have access to any printer camera.")
            return

        if not context.args:
            if len(claimed) == 1:
                printer_index = claimed[0]
            elif len(claimed) > 1:
                printer_list = ", ".join(str(idx + 1) for idx in claimed)
                await update.message.reply_text(f"You have multiple prints claimed ({printer_list}). Usage: /camera <printer>")
                return
            elif is_owner:
                await update.message.reply_text(
                    f"Usage: /camera <printer>\n"
                    f"Available printers: 1-{len(cfg.PRINTERS)}"
                )
                return
            else:
                await update.message.reply_text("You don't have an active print claimed.")
                return
        else:
            try:
                printer_num = int(context.args[0])
                printer_index = printer_num - 1
            except ValueError:
                await update.message.reply_text("Please provide a valid printer number.")
                return

            if printer_index < 0 or printer_index >= len(cfg.PRINTERS):
                await update.message.reply_text(f"Invalid printer number. Use 1-{len(cfg.PRINTERS)}")
                return

            # Check permission: owner can access all, claimers only their printers
            if not is_owner and printer_index not in claimed:
                claimed_list = ", ".join(str(idx + 1) for idx in claimed)
                await update.message.reply_text(f"You only have access to Printer(s) {claimed_list}.")
                return

        frame = printer_manager.get_camera_frame(printer_index)
        if not frame:
            await update.message.reply_text(f"Printer {printer_index + 1} is not connected or has no camera frame.")
            return

        await update.message.reply_photo(
            photo=InputFile(frame, filename=f"printer_{printer_index + 1}.jpg"),
            caption=f"Camera image from Printer {printer_index + 1}"
        )

    app.add_handler(CommandHandler("camera", handle_camera))

    # /livestream command - start a livestream for a claimed printer (owner can access all)
    async def handle_livestream(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        is_owner = user_id == cfg.OWNER_ID
        claimed = _get_claimed_printers(storage, user_id)

        if not is_owner and not claimed:
            await update.message.reply_text("You don't have an active print claimed.")
            return

        if not printer_manager:
            await update.message.reply_text("Printer manager not available.")
            return

        # Resolve printer - owner can access any, claimers only their own
        if not context.args:
            if len(claimed) == 1:
                printer_index = claimed[0]
            elif len(claimed) > 1:
                printer_list = ", ".join(str(idx + 1) for idx in claimed)
                await update.message.reply_text(f"You have multiple prints claimed ({printer_list}). Usage: /livestream <printer>")
                return
            elif is_owner:
                await update.message.reply_text(
                    f"Usage: /livestream <printer>\n"
                    f"Available printers: 1-{len(cfg.PRINTERS)}"
                )
                return
            else:
                await update.message.reply_text("You don't have an active print claimed.")
                return
        else:
            try:
                printer_num = int(context.args[0])
                printer_index = printer_num - 1
            except ValueError:
                await update.message.reply_text("Please provide a valid printer number.")
                return

            if printer_index < 0 or printer_index >= len(cfg.PRINTERS):
                await update.message.reply_text(f"Invalid printer number. Use 1-{len(cfg.PRINTERS)}")
                return

            # Check permission: owner can access all, claimers only their printers
            if not is_owner and printer_index not in claimed:
                claimed_list = ", ".join(str(idx + 1) for idx in claimed)
                await update.message.reply_text(f"You only have access to Printer(s) {claimed_list}.")
                return

        frame = printer_manager.get_camera_frame(printer_index)
        if not frame:
            await update.message.reply_text(f"Printer {printer_index + 1} is not connected or has no camera frame.")
            return

        # Start the livestream (this will stop any existing one for this printer)
        await message_service.start_livestream(printer_index, update.effective_chat.id, frame)

    app.add_handler(CommandHandler("livestream", handle_livestream))

    # /notify command - set a layer or percentage to be notified at
    async def handle_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        claimed = _get_claimed_printers(storage, user_id)

        if not claimed:
            await update.message.reply_text("You don't have an active print claimed.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /notify [printer] <layer> or /notify [printer] <percent>%\nExamples: /notify 50 or /notify 2 75%")
            return

        # Determine printer index and notification value
        args = list(context.args)
        printer_index = None

        # Check if first arg is a printer number
        if len(args) >= 2:
            try:
                maybe_printer = int(args[0])
                if 1 <= maybe_printer <= len(cfg.PRINTERS) and (maybe_printer - 1) in claimed:
                    printer_index = maybe_printer - 1
                    args = args[1:]  # Remove printer arg
            except ValueError:
                pass

        # If no printer specified, resolve from claimed
        if printer_index is None:
            if len(claimed) == 1:
                printer_index = claimed[0]
            else:
                printer_list = ", ".join(str(idx + 1) for idx in claimed)
                await update.message.reply_text(f"You have multiple prints claimed ({printer_list}). Usage: /notify <printer> <layer|percent%>")
                return

        if not args:
            await update.message.reply_text("Usage: /notify [printer] <layer> or /notify [printer] <percent>%")
            return

        arg = args[0]

        # Check if it's a percentage
        if arg.endswith('%'):
            try:
                percent = int(arg[:-1])
                if percent < 1 or percent > 100:
                    await update.message.reply_text("Percentage must be between 1 and 100.")
                    return

                # Get total layers to convert percent to layer
                if not printer_manager:
                    await update.message.reply_text("Printer manager not available.")
                    return

                printer = printer_manager.get_printer(printer_index)
                if not printer or not printer.mqtt_client_ready():
                    await update.message.reply_text(f"Printer {printer_index + 1} is not connected.")
                    return

                total_layers = printer.total_layer_num()
                if total_layers <= 0:
                    await update.message.reply_text("Cannot determine total layers for this print.")
                    return

                # Convert percent to target layer
                target_layer = max(1, (percent * total_layers) // 100)
                storage.set_notify_layer(printer_index, target_layer, notify_type="percent", original_value=percent)
                await update.message.reply_text(f"You will be notified when {percent}% is reached (layer {target_layer}/{total_layers}) on Printer {printer_index + 1}.")

            except ValueError:
                await update.message.reply_text("Please provide a valid percentage.")
        else:
            try:
                layer = int(arg)
                if layer < 1:
                    await update.message.reply_text("Layer must be a positive number.")
                    return

                storage.set_notify_layer(printer_index, layer, notify_type="layer", original_value=layer)
                await update.message.reply_text(f"You will be notified when layer {layer} is reached on Printer {printer_index + 1}.")

            except ValueError:
                await update.message.reply_text("Please provide a valid layer number or percentage.")

    app.add_handler(CommandHandler("notify", handle_notify))

    # /info command - show info about user's current print
    async def handle_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        claimed = _get_claimed_printers(storage, user_id)

        if not claimed:
            await update.message.reply_text("You don't have an active print claimed.")
            return

        # Resolve printer from args or single claim
        printer_index, error = _resolve_printer(storage, user_id, context.args)
        if error:
            await update.message.reply_text(error)
            return

        session = storage.get_print(printer_index)

        if not printer_manager:
            await update.message.reply_text("Printer manager not available.")
            return

        printer = printer_manager.get_printer(printer_index)
        if not printer or not printer.mqtt_client_ready():
            await update.message.reply_text(f"Printer {printer_index + 1} is not connected.")
            return

        # Gather print info
        progress = printer.get_percentage()
        time_left = printer_manager._format_print_time(printer.get_time())
        current_layer = printer.current_layer_num()
        total_layers = printer.total_layer_num()
        gcode_state = printer.get_state()

        info_text = (
            f"Printer {printer_index + 1} Info:\n"
            f"- Status: {gcode_state}\n"
            f"- Progress: {progress}%\n"
            f"- Time remaining: {time_left}\n"
            f"- Layer: {current_layer}/{total_layers}\n"
        )

        if session and session.notify_layer and not session.notify_layer_notified:
            if session.notify_type == "percent":
                info_text += f"- Notification: {session.notify_original_value}% (layer {session.notify_layer})\n"
            else:
                info_text += f"- Notification: layer {session.notify_layer}\n"

        await update.message.reply_text(info_text)

    app.add_handler(CommandHandler("info", handle_info))

    # /unclaim command - unclaim the print and revert main chat message
    async def handle_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id

        # Resolve printer from args or single claim
        printer_index, error = _resolve_printer(storage, user_id, context.args)
        if error:
            await update.message.reply_text(error)
            return

        session = storage.get_print(printer_index)
        if not session:
            await update.message.reply_text("This print session has ended.")
            return

        # Store message info before unclaiming
        message_id = session.message_id
        chat_id = session.chat_id.split("/")[0] if "/" in session.chat_id else session.chat_id
        print_time = session.print_time

        # Unclaim the print
        storage.unclaim_print(printer_index)

        # Restore the main chat message with the Claim Print button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Claim Print", callback_data=f"claim_{printer_index}")]
        ])

        print_time_str = f" (print time: {print_time})" if print_time else ""
        message = f"Printer {printer_index + 1} has started printing.{print_time_str}"

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=message,
                reply_markup=keyboard
            )
            await update.message.reply_text(f"You have unclaimed Printer {printer_index + 1}.")
        except Exception:
            await update.message.reply_text(f"Unclaimed Printer {printer_index + 1}, but could not update the main chat message.")

    app.add_handler(CommandHandler("unclaim", handle_unclaim))

    # /help command
    async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "Available commands:\n"
            "/help - Show this help message\n"
            "/info [printer] - Show info about your print\n"
            "/notify [printer] <layer> - Get notified at a specific layer\n"
            "/notify [printer] <percent>% - Get notified at a percentage\n"
            "/camera [printer] - View camera image from your printer\n"
            "/livestream [printer] - Start a live updating camera feed\n"
            "/unclaim [printer] - Unclaim your print\n\n"
            "Note: [printer] is required when you have multiple prints claimed."
        )
        await update.message.reply_text(help_text)

    app.add_handler(CommandHandler("help", handle_help))

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

            # Get current print info
            print_info = _get_print_info(printer_manager, printer_index)

            # Show preference selection
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dm_pref_{printer_index}_chat"),
                    InlineKeyboardButton("Send to DM only", callback_data=f"dm_pref_{printer_index}_dm")
                ],
                [
                    InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim_{printer_index}"),
                    InlineKeyboardButton("Help", callback_data="help")
                ]
            ])

            await update.message.reply_text(
                f"You claimed Printer {printer_index + 1}!{print_info}\n\nWhere would you like to receive the finished print image?",
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


def _get_print_info(printer_manager, printer_index: int) -> str:
    """Get current print info for a printer."""
    if not printer_manager:
        return ""

    printer = printer_manager.get_printer(printer_index)
    if not printer or not printer.mqtt_client_ready():
        return ""

    progress = printer.get_percentage()
    time_left = printer_manager._format_print_time(printer.get_time())
    layer = printer.current_layer_num()
    total_layers = printer.total_layer_num()

    return f"\n\nCurrent status:\n- Progress: {progress}%\n- Time remaining: {time_left}\n- Layer: {layer}/{total_layers}"


async def handle_claim(query, user, storage: Storage, message_service, context, printer_manager=None):
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

    # Get current print info
    print_info = _get_print_info(printer_manager, printer_index)

    # Try to DM the user asking for their preference
    dm_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dm_pref_{printer_index}_chat"),
            InlineKeyboardButton("Send to DM only", callback_data=f"dm_pref_{printer_index}_dm")
        ],
        [
            InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim_{printer_index}"),
            InlineKeyboardButton("Help", callback_data="help")
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"You claimed Printer {printer_index + 1}!{print_info}\n\nWhere would you like to receive the finished print image?",
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
        [InlineKeyboardButton(layer2_btn_text, callback_data=f"layer2_toggle_{printer_index}")],
        [
            InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim_{printer_index}"),
            InlineKeyboardButton("Help", callback_data="help")
        ]
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


async def handle_unclaim_callback(query, user, storage: Storage, context):
    data = query.data
    printer_index = int(data.split("_")[1])

    session = storage.get_print(printer_index)
    if not session:
        await query.edit_message_text("This print session has ended.")
        return

    # Verify this user is the one who claimed it
    if session.claimed_by != user.id:
        await query.answer("You are not the claimer of this print.", show_alert=True)
        return

    # Store message info before unclaiming
    message_id = session.message_id
    chat_id = session.chat_id.split("/")[0] if "/" in session.chat_id else session.chat_id
    print_time = session.print_time

    # Unclaim the print
    storage.unclaim_print(printer_index)

    # Restore the main chat message with the Claim Print button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Claim Print", callback_data=f"claim_{printer_index}")]
    ])

    print_time_str = f" (print time: {print_time})" if print_time else ""
    message = f"Printer {printer_index + 1} has started printing.{print_time_str}"

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=message,
            reply_markup=keyboard
        )
        await query.edit_message_text(f"You have unclaimed Printer {printer_index + 1}.")
    except Exception:
        await query.edit_message_text(f"Unclaimed Printer {printer_index + 1}, but could not update the main chat message.")


async def handle_help_callback(query):
    help_text = (
        "Available commands:\n"
        "/help - Show this help message\n"
        "/info [printer] - Show info about your print\n"
        "/notify [printer] <layer> - Get notified at a specific layer\n"
        "/notify [printer] <percent>% - Get notified at a percentage\n"
        "/camera [printer] - View camera image from your printer\n"
        "/livestream [printer] - Start a live updating camera feed\n"
        "/unclaim [printer] - Unclaim your print\n\n"
        "Note: [printer] is required when you have multiple prints claimed."
    )
    await query.answer()
    await query.message.reply_text(help_text)


async def handle_restart_printer(query, user, printer_manager):
    """Handle restart printer button callback (owner only)."""
    if user.id != cfg.OWNER_ID:
        await query.answer("Only the owner can restart printers.", show_alert=True)
        return

    printer_index = int(query.data.split("_")[2])

    if not printer_manager:
        await query.edit_message_text("Printer manager not available.")
        return

    printer = printer_manager.get_printer(printer_index)
    if not printer:
        await query.edit_message_text(f"Printer {printer_index + 1} not found.")
        return

    try:
        printer.disconnect()
        printer.connect()
        await query.edit_message_text(f"Printer {printer_index + 1} reconnection initiated.")
    except Exception as e:
        await query.edit_message_text(f"Failed to restart Printer {printer_index + 1}: {e}")
