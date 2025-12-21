import time
import bambulabs_api as bl
from bambulabs_api import PrintStatus, GcodeState, Printer
import asyncio
import telegram

import config as cfg

"""
TODO:
- add notify me button when print starts
- /status command to get current status
- add /print command for uploading and starting prints
- add /stop command to stop prints
- handle printer disconnections and reconnections
- improve status message formatting
- change access code via command
"""

LOG_CHAT_ID = cfg.LOG_CHAT_ID
CHAT_ID = cfg.CHAT_ID.split('/')[0]
THREAD_ID = cfg.CHAT_ID.split('/')[1] if '/' in cfg.CHAT_ID else None
STATUS_CHAT_ID = cfg.STATUS_CHAT_ID.split('/')[0]
STATUS_THREAD_ID = cfg.STATUS_CHAT_ID.split('/')[1] if '/' in cfg.STATUS_CHAT_ID else None

TELEGRAM_BOT_TOKEN = cfg.TELEGRAM_BOT_TOKEN
PRINTERS = cfg.PRINTERS
UPDATE_INTERVAL = cfg.UPDATE_INTERVAL
UPDATE_START_PRINTING = cfg.UPDATE_START_PRINTING

bot = telegram.Bot(TELEGRAM_BOT_TOKEN)

async def send_telegram_message(message: str, chat_id: str, thread_id: str = None, image: bytes | bytearray = None):
    if isinstance(image, bytearray):
        image = bytes(image)
    async with bot:
        if image is not None:
            await bot.send_photo(chat_id=chat_id, photo=telegram.InputFile(image), caption=message, message_thread_id=thread_id)
        else:
            await bot.send_message(chat_id=chat_id, text=message, message_thread_id=thread_id)

last_log_time = 0
message_buffer = ''

async def send_update_message(message: str, image: bytearray = None):
    await send_telegram_message(message, CHAT_ID, THREAD_ID, image)

async def log_message(message: str):
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {message}')
    if LOG_CHAT_ID:
        global last_log_time, message_buffer
        cur_time = time.time()
        message_buffer += f'\n{message}'
        if cur_time - last_log_time < 5:
            return
        last_log_time = cur_time
        await send_telegram_message(message_buffer, LOG_CHAT_ID)
        message_buffer = ''


cur_status_msg_id = None

def load_current_status_msg_id():
    global cur_status_msg_id
    try:
        with open('cur_status_id.txt', 'r') as f:
            cur_status_msg_id = int(f.read().strip())
    except:
        print('No previous status message ID found.')

def save_current_status_msg_id():
    global cur_status_msg_id
    with open('cur_status_id.txt', 'w') as f:
        f.write(str(cur_status_msg_id))

prev_status_message = ''
async def update_status_message(message):
    global prev_status_message, cur_status_msg_id
    if message == prev_status_message:
        return
    prev_status_message = message
    # print(message)
    async with bot:
        if cur_status_msg_id is None:
            msg = await bot.send_message(chat_id=STATUS_CHAT_ID, text=message, message_thread_id=STATUS_THREAD_ID, parse_mode=telegram.constants.ParseMode.MARKDOWN_V2)
            cur_status_msg_id = msg.message_id
            save_current_status_msg_id()
        else:
            await bot.edit_message_text(chat_id=STATUS_CHAT_ID, message_id=cur_status_msg_id, text=message, parse_mode=telegram.constants.ParseMode.MARKDOWN_V2)

def format_print_time(printer):
    total_mins = printer.get_time()
    hrs = total_mins // 60
    mins = total_mins % 60
    return f'{hrs}h{mins}m' if hrs > 0 else f'{mins}m'

last_update_time = 0

async def update_printer_states(printers: list[Printer]):
    # global last_update_time
    # cur_time = time.time()
    # if cur_time - last_update_time < 10:
    #     return
    # last_update_time = cur_time

    status_message = 'Printer Statuses:```c\n'
    for i, printer in enumerate(printers):
        if not printer.mqtt_client_ready():
            continue
        gcode_state = printer.get_state()
        print_state = printer.get_current_state()
        bed_temp = round(printer.get_bed_temperature())
        nozzle_temp = round(printer.get_nozzle_temperature())
        layer = printer.current_layer_num()
        total_layers = printer.total_layer_num()

        status_message += f'{i+1}: {gcode_state} ({print_state}'

        if gcode_state not in (GcodeState.IDLE, GcodeState.FINISH, GcodeState.UNKNOWN):
            progress = printer.get_percentage()
            status_message += f', {progress}% done, {format_print_time(printer)} left'

        # status_message += f', B: {bed_temp}°C, N: {nozzle_temp}°C'
        status_message += 'L {layer}/{total_layers})\n'
    status_message += 'Note: "FINISH/IDLE" means not in use\n'
    status_message += f'Updated on: {time.strftime("%Y-%m-%d %H:%M")}, ID: {cur_status_msg_id}\n'
    status_message += '```\n'
    await update_status_message(status_message)

async def main():
    load_current_status_msg_id()
    await log_message('Bot started!')
    
    try:
        printers: list[Printer] = [None] * len(PRINTERS)
        prevState = [(GcodeState.UNKNOWN, PrintStatus.UNKNOWN)] * len(PRINTERS)
        lastPausedTime = [time.time()] * len(PRINTERS)

        for i, printer in enumerate(PRINTERS):
            name, mac, ip, access_code, serial = printer
            print(f'Connecting to printer {i+1} at IP {ip} with serial {serial} and access code {access_code}')
            try:
                p = bl.Printer(ip, access_code, serial)
                p.mqtt_start()
                p.camera_start()
                printers[i] = p
            except Exception as e:
                print(f'Failed to connect to printer {i+1}: {e}')

        while True:
            await asyncio.sleep(5)
            try:
                await update_printer_states(printers)
            except Exception as e:
                print(f'Failed to update printer states: {e}')

            for i, printer in enumerate(printers):
                try:
                    if not printer.mqtt_client_ready():
                        continue
                    
                    prev_gcode_state, prev_print_state = prevState[i]
                    gcode_state = printer.get_state()
                    print_state = printer.get_current_state()
                    prevState[i] = (gcode_state, print_state)

                    if prev_gcode_state != GcodeState.UNKNOWN and prev_gcode_state != gcode_state:
                        await log_message(f'Printer {i+1} GCODE state changed from {prev_gcode_state} to {gcode_state}')

                        if gcode_state == GcodeState.FINISH:
                            await send_update_message(f'Printer {i+1} has finished printing.', printer.camera_client.last_frame)

                        elif gcode_state == GcodeState.FAILED:
                            err_code = printer.get_error_code()
                            await send_update_message(f'Printer {i+1} failed! (code: {err_code})')

                        elif prev_gcode_state == GcodeState.RUNNING and gcode_state == GcodeState.PAUSE:
                            now = time.time()
                            if now - lastPausedTime[i] > 60: # arbitrary 60s to avoid duplicate messages
                                err_code = printer.get_error_code()
                                await send_update_message(f'Printer {i+1} has paused printing. (code: {err_code})')
                                lastPausedTime[i] = now

                        elif UPDATE_START_PRINTING and prev_gcode_state in (GcodeState.FINISH, GcodeState.IDLE, GcodeState.PREPARE) and gcode_state == GcodeState.RUNNING:
                            await send_update_message(f'Printer {i+1} has started printing. (print time: {format_print_time(printer)})')

                    if prev_print_state != PrintStatus.UNKNOWN and prev_print_state != print_state:
                        await log_message(f'Printer {i+1} PRINT state changed from {prev_print_state} to {print_state}')

                except Exception as e:
                    print(f'Failed to get print_state or disconnect from printer {i+1}: {e}')
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

