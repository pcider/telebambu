import asyncio
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from bambulabs_api import GcodeState

from .manager import PrinterManager, EventType
from bot.messages import MessageService
import config as cfg

# Track which printers have been reported as stale to avoid spam
_stale_camera_reported: set[int] = set()


async def monitor_loop(printer_manager: PrinterManager, message_service: MessageService):
    while True:
        await asyncio.sleep(5)

        # Reconnect printers if needed
        await printer_manager.reconnect_if_needed(message_service.log_message)

        # Update status message
        try:
            status_text = printer_manager.get_status_text()
            await message_service.update_status_message(status_text)
        except Exception as e:
            print(f'Failed to update status message: {e}')

        # Check for stale cameras on idle printers
        await check_stale_cameras(printer_manager, message_service)

        # Send recurring camera notifications when their configured trigger is due.
        await check_periodic_camera_notifications(printer_manager, message_service)

        # Process printer events
        for event in printer_manager.check_states():
            try:
                await handle_event(event, printer_manager, message_service)
            except Exception as e:
                print(f'Error handling event {event.type}: {e}')


async def check_stale_cameras(printer_manager: PrinterManager, message_service: MessageService):
    """Check if any idle printers have stale cameras and notify owner."""
    for i, printer in enumerate(printer_manager.printers):
        if not printer or not printer.mqtt_client_ready():
            continue

        gcode_state = printer.get_state()
        has_frame = printer_manager.has_camera_frame(i)

        # If printer is IDLE and has no camera frame, it might need a restart
        if gcode_state == GcodeState.IDLE and not has_frame:
            if i not in _stale_camera_reported:
                _stale_camera_reported.add(i)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Restart Printer", callback_data=f"restart_printer_{i}")]
                ])
                await message_service.bot.send_message(
                    chat_id=cfg.OWNER_ID,
                    text=f"Printer {i + 1} is IDLE but camera is not updating. Consider restarting.",
                    reply_markup=keyboard
                )
        elif has_frame and i in _stale_camera_reported:
            # Camera recovered, clear the flag
            _stale_camera_reported.discard(i)


async def check_periodic_camera_notifications(printer_manager: PrinterManager, message_service: MessageService):
    for i, printer in enumerate(printer_manager.printers):
        if not printer or not printer.mqtt_client_ready():
            continue

        session = message_service.storage.get_print(i)
        if not session or not session.claimed_by or not session.notify_every_type:
            continue
        if printer.get_state() != GcodeState.RUNNING:
            continue

        trigger_value = None
        due = False
        if session.notify_every_type == "layers":
            interval = session.notify_every_value
            current_layer = printer.current_layer_num()
            trigger_value = current_layer // interval if interval else 0
            due = trigger_value > 0 and trigger_value > (session.notify_every_last_value or 0)
        elif session.notify_every_type == "percent":
            interval = session.notify_every_value
            progress = printer.get_percentage()
            trigger_value = progress // interval if interval else 0
            due = trigger_value > 0 and trigger_value > (session.notify_every_last_value or 0)
        elif session.notify_every_type == "time":
            interval_seconds = session.notify_every_value * 60
            due = time.time() - (session.notify_every_last_sent_at or 0) >= interval_seconds

        if due:
            frame = await printer_manager.get_camera_frame(i)
            await message_service.send_periodic_camera_notification(i, printer, trigger_value, frame)


async def handle_event(event, printer_manager: PrinterManager, message_service: MessageService):
    printer = event.printer
    i = event.printer_index

    if event.type == EventType.STATE_CHANGED:
        # Log state changes to stdout only (not to Telegram)
        if 'prev' in event.data and 'new' in event.data:
            await message_service.log_message(
                f'Printer {i + 1} GCODE state: {event.data["prev"]} -> {event.data["new"]}',
                stdout_only=True
            )
        elif 'prev_print' in event.data:
            await message_service.log_message(
                f'Printer {i + 1} PRINT state: {event.data["prev_print"]} -> {event.data["new_print"]}',
                stdout_only=True
            )

    elif event.type == EventType.PRINT_STARTED:
        # Delay to allow printer to update print time estimate
        await asyncio.sleep(2)
        print_time = message_service.format_print_time(printer.get_time())
        total_layers = printer.total_layer_num()
        await message_service.send_print_started(i, print_time, total_layers)

    elif event.type == EventType.PRINT_FINISHED:
        frame = await printer_manager.get_camera_frame(i)
        await message_service.send_print_finished(i, frame)

    elif event.type == EventType.PRINT_FAILED:
        err_code = event.data.get('error_code')
        frame = await printer_manager.get_camera_frame(i)
        await message_service.send_print_failed(i, err_code, frame)

    elif event.type == EventType.PRINT_PAUSED:
        err_code = event.data.get('error_code')
        frame = await printer_manager.get_camera_frame(i)
        await message_service.log_message(
            f'Printer {i + 1} has paused printing. (code: {err_code})',
            frame
        )

    elif event.type == EventType.LAYER_CHANGED:
        layer = event.data['layer']
        if layer == 2:
            frame = await printer_manager.get_camera_frame(i)
            await message_service.send_layer2_notification(i, frame)

        # Check for custom layer notification (handles both layer and percent notifications)
        frame = await printer_manager.get_camera_frame(i)
        await message_service.send_custom_layer_notification(i, layer, frame)
