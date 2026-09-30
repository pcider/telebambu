import asyncio
import signal

import config as cfg
from data import Storage
from bot import create_application, get_bot_context, setup_handlers, MessageService
from printers import PrinterManager, PrinterMonitor
from printers.monitor import DEFAULT_UPDATE_INTERVAL, DEFAULT_STATS_LOG_INTERVAL


async def main():
    storage = Storage()
    printer_manager = PrinterManager(cfg.PRINTERS)
    app = create_application()
    message_service = MessageService(app.bot, get_bot_context(), storage)
    setup_handlers(app, storage, message_service, printer_manager)
    monitor = PrinterMonitor(printer_manager, message_service,
                             getattr(cfg, 'UPDATE_INTERVAL', DEFAULT_UPDATE_INTERVAL),
                             getattr(cfg, 'AUTO_RESTART_PRINTERS', True),
                             getattr(cfg, 'STATS_LOG_INTERVAL', DEFAULT_STATS_LOG_INTERVAL))

    # Stop cleanly on Ctrl+C or `kill` (stop.sh)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with app:
        await printer_manager.connect_all(message_service.log_message)
        await app.start()
        await app.updater.start_polling()
        await message_service.log_message('Bot started!')

        monitor_task = asyncio.create_task(monitor.run())
        try:
            await stop.wait()
        finally:
            print('Shutting down...')
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            await message_service.flush_logs(force=True)
            await app.updater.stop()
            await app.stop()
            # Disabled: printer.disconnect() can hang forever joining the camera thread
            # if it's stuck in a blocking socket connect to an unreachable printer,
            # which blocks the whole event loop and prevents shutdown.
            # printer_manager.disconnect_all()


if __name__ == '__main__':
    asyncio.run(main())
