import asyncio

from .manager import PrinterManager, EventType
from bot.messages import MessageService


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

        # Process printer events
        for event in printer_manager.check_states():
            try:
                await handle_event(event, message_service)
            except Exception as e:
                print(f'Error handling event {event.type}: {e}')


async def handle_event(event, message_service: MessageService):
    printer = event.printer
    i = event.printer_index

    if event.type == EventType.STATE_CHANGED:
        if 'prev' in event.data and 'new' in event.data:
            await message_service.log_message(
                f'Printer {i + 1} GCODE state changed from {event.data["prev"]} to {event.data["new"]}'
            )
        elif 'prev_print' in event.data:
            await message_service.log_message(
                f'Printer {i + 1} PRINT state changed from {event.data["prev_print"]} to {event.data["new_print"]}'
            )

    elif event.type == EventType.PRINT_STARTED:
        # Delay to allow printer to update print time estimate
        await asyncio.sleep(2)
        print_time = message_service.format_print_time(printer.get_time())
        await message_service.send_print_started(i, print_time)

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
