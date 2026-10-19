import asyncio
import faulthandler
import signal
import socket

from settings import load as load_settings
from data import Storage
from bot import create_application, get_bot_context, setup_handlers, MessageService, set_command_menu
from printers import PrinterManager, PrinterMonitor

# bambulabs_api's camera thread opens its socket and does the TLS handshake with no
# timeout, so a printer whose camera service wedges (or that reboots mid-handshake)
# can block that thread forever, and anything joining it hangs too. A default timeout
# makes those calls fail and retry instead. paho-mqtt and httpx set their own timeouts,
# so this only affects sockets created without one.
SOCKET_DEFAULT_TIMEOUT = 15  # seconds
socket.setdefaulttimeout(SOCKET_DEFAULT_TIMEOUT)

# `kill -USR1 <pid>` dumps every thread's stack to stderr (telebambu.log).
# Registered at the C level, so it still works while the event loop is blocked.
faulthandler.register(signal.SIGUSR1, all_threads=True)


async def main():
    settings = load_settings()
    storage = Storage()
    storage.ensure_owner(settings.owner_id)
    storage.roles_enabled = not settings.is_disabled('staff')
    printer_manager = PrinterManager(settings.printers)
    app = create_application(settings)
    await set_command_menu(app.bot, storage, settings)
    message_service = MessageService(app.bot, get_bot_context(settings), storage)
    setup_handlers(app, storage, message_service, printer_manager, settings)
    monitor = PrinterMonitor(printer_manager, message_service, settings)

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
