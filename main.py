import time
import bambulabs_api as bl
from bambulabs_api import PrintStatus, GcodeState
import asyncio
import telegram

import config as cfg

LOG_CHAT_ID = cfg.LOG_CHAT_ID
CHAT_ID = cfg.CHAT_ID.split('/')[0]
THREAD_ID = cfg.CHAT_ID.split('/')[1] if '/' in cfg.CHAT_ID else None
STATUS_CHAT_ID = cfg.STATUS_CHAT_ID.split('/')[0]
STATUS_THREAD_ID = cfg.STATUS_CHAT_ID.split('/')[1] if '/' in cfg.STATUS_CHAT_ID else None

TELEGRAM_BOT_TOKEN = cfg.TELEGRAM_BOT_TOKEN
PRINTERS = cfg.PRINTERS

bot = telegram.Bot(TELEGRAM_BOT_TOKEN)

async def send_telegram_message(message: str, chat_id: str = CHAT_ID, thread_id: str = THREAD_ID):
    async with bot:
        await bot.send_message(chat_id=chat_id, text=message, message_thread_id=thread_id)

async def log_message(message: str):
    if LOG_CHAT_ID:
        await send_telegram_message(message, LOG_CHAT_ID)

cur_status_message = None

async def update_status_message(message):
    global cur_status_message
    async with bot:
        if cur_status_message is None:
            msg = await bot.send_message(chat_id=STATUS_CHAT_ID, text=message, message_thread_id=STATUS_THREAD_ID, parse_mode=telegram.constants.ParseMode.MARKDOWN_V2)
            cur_status_message = msg.message_id
        else:
            await bot.edit_message_text(chat_id=STATUS_CHAT_ID, message_id=cur_status_message, text=message, parse_mode=telegram.constants.ParseMode.MARKDOWN_V2)
prev_status_message = ''

async def update_printer_states(printers):
    status_message = 'Printer Statuses:```c\n'
    for i, printer in enumerate(printers):
        gcode_state = printer.get_state()
        print_state = printer.get_current_state()
        bed_temp = round(printer.get_bed_temperature())
        nozzle_temp = round(printer.get_nozzle_temperature())
        
        status_message += f'{i+1}: {gcode_state} ({print_state}'

        if gcode_state not in (GcodeState.IDLE, GcodeState.FINISH, GcodeState.UNKNOWN):
            progress = printer.get_percentage()
            mins = printer.get_time() or 0
            hrs = mins // 60
            mins = mins % 60
            status_message += f', {progress}% done, {hrs == 0 and "" or f"{hrs}h"}{mins}m left'

        # status_message += f', B: {bed_temp}°C, N: {nozzle_temp}°C'
        status_message += ')\n'
    status_message += 'Note: "FINISH (PRINTING)" means not in use\n'
    status_message += '```\n'
    global prev_status_message
    if status_message != prev_status_message:
        await update_status_message(status_message)
    prev_status_message = status_message

async def main():
    await log_message('Bot started!')
    
    try:
        printers = [None] * len(PRINTERS)
        prevState = [(GcodeState.UNKNOWN, PrintStatus.UNKNOWN)] * len(PRINTERS)
        for i, printer in enumerate(PRINTERS):
            name, mac, ip, access_code, serial = printer
            print(f'Connecting to printer {name} at IP {ip} with serial {serial} and access code {access_code}')
            try:
                p = bl.Printer(ip, access_code, serial)
                p.connect()
                printers[i] = p
            except Exception as e:
                print(f'Failed to connect to printer {name}: {e}')

        while True:
            await asyncio.sleep(1)
            for i, printer in enumerate(printers):
                try:
                    prev_gcode_state, prev_print_state = prevState[i]
                    gcode_state = printer.get_state()
                    print_state = printer.get_current_state()
                    prevState[i] = (gcode_state, print_state)

                    await update_printer_states(printers)

                    if prev_gcode_state != GcodeState.UNKNOWN and prev_gcode_state != gcode_state:
                        print(f'Printer {i+1} GCODE state changed from {prev_gcode_state} to {gcode_state}')
                        await log_message(f'Printer {i+1} GCODE state changed from {prev_gcode_state} to {gcode_state}')

                        if gcode_state == GcodeState.FINISH:
                            print(f'Printer {i+1} finished printing.')
                            await send_telegram_message(f'Printer {i+1} has finished printing.')
                        if gcode_state == GcodeState.FAILED:
                            err_code = printer.get_error_code()
                            print(f'Printer {i+1} print failed!')
                            await send_telegram_message(f'Printer {i+1} failed! (code: {err_code})')
                        if prev_gcode_state == GcodeState.RUNNING and gcode_state == GcodeState.PAUSED:
                            print(f'Printer {i+1} paused printing.')
                            await send_telegram_message(f'Printer {i+1} has paused printing.')
                        if prev_gcode_state == GcodeState.IDLE and gcode_state == GcodeState.PREPARE:
                            print(f'Printer {i+1} started printing.')
                            await send_telegram_message(f'Printer {i+1} has started printing.')

                    if prev_print_state != PrintStatus.UNKNOWN and prev_print_state != print_state:
                        print(f'Printer {i+1} PRINT state changed from {prev_print_state} to {print_state}')
                        await log_message(f'Printer {i+1} PRINT state changed from {prev_print_state} to {print_state}')

                except Exception as e:
                    print(f'Failed to get print_state or disconnect from printer {i+1}: {e}')
            print('.', end='')
    except KeyboardInterrupt:
        for printer in printers:
            try:
                printer.disconnect()
            except Exception as e:
                print(f'Failed to disconnect from printer {printer.serial}: {e}')
        print('Exiting...')
        exit(0)

if __name__ == '__main__':
    asyncio.run(main())

