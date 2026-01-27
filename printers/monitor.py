import asyncio
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from bambulabs_api import GcodeState

from .manager import PrinterManager, EventType
from bot.messages import MessageService
import config as cfg

# Track which printers have been reported as stale to avoid spam
_stale_camera_reported: set[int] = set()
_last_livestream_update: float = 0


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

        # Update livestreams
        await update_livestreams(printer_manager, message_service)

        # Process printer events
        for event in printer_manager.check_states():
            try:
                await handle_event(event, message_service)
            except Exception as e:
                print(f'Error handling event {event.type}: {e}')


async def check_stale_cameras(printer_manager: PrinterManager, message_service: MessageService):
    """Check if any idle printers have stale cameras and notify owner."""
    for i, printer in enumerate(printer_manager.printers):
        if not printer or not printer.mqtt_client_ready():
            continue

        gcode_state = printer.get_state()
        has_frame = printer.camera_client.last_frame is not None

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


async def update_livestreams(printer_manager: PrinterManager, message_service: MessageService):
    """Update all active livestreams at the configured interval."""
    global _last_livestream_update

    now = time.time()
    if now - _last_livestream_update < cfg.LIVESTREAM_INTERVAL:
        return

    _last_livestream_update = now
    await message_service.update_livestreams(printer_manager.get_camera_frame)


async def handle_event(event, message_service: MessageService):
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
        printer.turn_light_on()
        printer.camera_client.last_frame = None

        # Wait for a fresh frame
        for _ in range(10):
            await asyncio.sleep(1)
            if printer.camera_client.last_frame:
                break

        await message_service.send_print_finished(i, printer.camera_client.last_frame)
        printer.turn_light_off()

    elif event.type == EventType.PRINT_FAILED:
        err_code = event.data.get('error_code')
        await message_service.log_message(
            f'Printer {i + 1} failed! (code: {err_code})',
            printer.camera_client.last_frame
        )

    elif event.type == EventType.PRINT_PAUSED:
        err_code = event.data.get('error_code')
        await message_service.log_message(
            f'Printer {i + 1} has paused printing. (code: {err_code})',
            printer.camera_client.last_frame
        )

    elif event.type == EventType.LAYER_CHANGED:
        layer = event.data['layer']
        if layer == 2:
            await message_service.send_layer2_notification(i, printer.camera_client.last_frame)

        # Check for custom layer notification (handles both layer and percent notifications)
        await message_service.send_custom_layer_notification(i, layer, printer.camera_client.last_frame)
