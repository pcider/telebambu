
# format: 'chat_id/thread_id' if using threads
LOG_CHAT_ID = '1234567890' # Set to None to disable logging
CHAT_ID = '1234567890/1234' # Main chat ID for print updates
STATUS_CHAT_ID = '1234567890/1234' # Chat ID for status messages

OWNER_ID = 1234567890  # Bot owner user ID (can use /camera command)

TELEGRAM_BOT_TOKEN = '1234567890:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'

UPDATE_INTERVAL = 3  # seconds
UPDATE_START_PRINTING = False  # Send message when printing starts

PRINTERS = [
    ('1', 'AA:BB:CC:DD:EE:FF', '192.168.1.123', '12345678', '0123456789ABCDE'),
    ('2', '00:11:22:33:44:55', '192.168.1.124', '12345679', '123456789ABCDEF'),
]