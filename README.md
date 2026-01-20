# Telebambu

A Telegram bot for monitoring and managing a fleet of Bambu Labs 3D printers. The bot connects to printers via MQTT and provides real-time status updates, print notifications, and camera access through Telegram.

## Features

- Real-time monitoring of multiple Bambu Labs printers
- Telegram notifications when prints start, finish, or encounter errors
- Users can "claim" print jobs and receive personalized updates
- Live camera access to view printers
- Layer 2 progress notifications
- Persistent status message showing all printer states
- Configurable notification preferences (chat or DM)

## Requirements

- Python 3.8+
- Bambu Labs printer(s) on the same network
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/pcider/telebambu.git
   cd telebambu
   ```

2. Create a virtual environment (recommended):
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

1. Copy the example configuration file:
   ```bash
   cp config_example.py config.py
   ```

2. Edit `config.py` with your settings:

   ```python
   # Telegram Chat IDs
   # Format: 'chat_id' or 'chat_id/thread_id' if using forum threads
   LOG_CHAT_ID = '1234567890'        # Chat for logging (set to None to disable)
   CHAT_ID = '1234567890/1234'       # Main chat for print updates
   STATUS_CHAT_ID = '1234567890/1234' # Chat for status messages

   # Bot owner user ID (can use /camera command for any printer)
   OWNER_ID = 1234567890

   # Your Telegram bot token from @BotFather
   TELEGRAM_BOT_TOKEN = '1234567890:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'

   # Update interval in seconds
   UPDATE_INTERVAL = 3

   # Whether to send a message when printing starts
   UPDATE_START_PRINTING = False

   # Printer configuration
   # Format: (display_name, mac_address, ip_address, access_code, serial_number)
   PRINTERS = [
       ('1', 'AA:BB:CC:DD:EE:FF', '192.168.1.123', '12345678', '0123456789ABCDE'),
       ('2', '00:11:22:33:44:55', '192.168.1.124', '12345679', '123456789ABCDEF'),
   ]
   ```

   **Finding your printer information:**
   - **IP Address**: Check your router's DHCP client list or the printer's network settings
   - **Access Code**: Found in the printer's LAN settings
   - **Serial Number**: Found in the printer's device info
   - **MAC Address**: Found in the printer's network settings

## Running the Bot

### Direct execution

```bash
python3 main.py
```

### Using the start script (recommended for production)

The `start.sh` script runs the bot in the background with logging:

```bash
chmod +x start.sh  # Make executable (first time only)
./start.sh
```

This will:
- Change to the script's directory
- Log a restart timestamp to `telebambu.log`
- Run the bot with output redirected to `telebambu.log`

To view logs in real-time:
```bash
tail -f telebambu.log
```

### Running as a service (Linux)

For persistent operation, create a systemd service:

```bash
sudo nano /etc/systemd/system/telebambu.service
```

```ini
[Unit]
Description=Telebambu Telegram Bot
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/path/to/telebambu
ExecStart=/path/to/telebambu/venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable telebambu
sudo systemctl start telebambu
```

## Usage

### Telegram Commands

- `/camera <number>` - Get a live camera snapshot from a printer (owner can access all printers, users can access their claimed printer)

### Interactive Buttons

When a print starts, the bot sends a message with a **Claim Print** button. Claiming a print allows you to:
- Receive the finish notification directly
- Choose between chat or DM delivery
- Enable/disable Layer 2 progress notifications
- Access the printer's camera

## Project Structure

```
telebambu/
├── main.py              # Application entry point
├── config.py            # Configuration (create from config_example.py)
├── config_example.py    # Configuration template
├── requirements.txt     # Python dependencies
├── start.sh             # Shell script to run the bot
├── bot/                 # Telegram bot module
│   ├── telegram_bot.py  # Bot initialization
│   ├── handlers.py      # Command and callback handlers
│   └── messages.py      # Message sending service
├── printers/            # Printer management module
│   ├── manager.py       # Printer connection manager
│   └── monitor.py       # State monitoring loop
└── data/                # Data persistence module
    └── storage.py       # Session and preference storage
```

## License

MIT License
