import asyncio

import config as cfg
from data import Storage
from bot import create_application, setup_handlers, MessageService
from bot.telegram_bot import get_bot_context
from printers import PrinterManager, monitor_loop


async def main():
    storage = Storage()
    printer_manager = PrinterManager(cfg.PRINTERS)
    app = create_application()
    bot_context = get_bot_context()

    # Create message service
    message_service = MessageService(app.bot, bot_context, storage)

    # Setup callback and command handlers
    setup_handlers(app, storage, message_service, printer_manager)

    # Connect to printers
    await printer_manager.connect_all(message_service.log_message)

    await message_service.log_message('Bot started!')

    # Run the application with polling and monitoring
    async with app:
        await app.start()
        await app.updater.start_polling()

        try:
            await monitor_loop(printer_manager, message_service)
        except KeyboardInterrupt:
            print('Shutting down...')
        finally:
            await app.updater.stop()
            await app.stop()
            printer_manager.disconnect_all()


if __name__ == '__main__':
    asyncio.run(main())
