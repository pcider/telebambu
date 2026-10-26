# Telebambu

A Telegram bot for monitoring and managing a fleet of Bambu Labs 3D printers. The bot connects to printers via MQTT and provides real-time status updates, print notifications, and camera access through Telegram.

## Features

- Real-time monitoring of multiple Bambu Labs printers
- Telegram notifications when prints start, finish, or encounter errors
- Users can "claim" print jobs and receive personalized updates
- Live camera access to view printers
- Layer 2 progress notifications
- Low filament (10% or less on the spool in use) and filament run-out alerts to a print's claimer and anyone added with `/notify_user`
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
   cp config_example.yml config.yml
   ```

2. Edit `config.yml` with your bot token, chat IDs, owner ID and printers. See the comments in
   `config_example.yml` for what each setting does. Printers are listed by number:

   ```yaml
   printers:
     1:
       mac: 'AA:BB:CC:DD:EE:FF'
       ip: '192.168.1.123'
       access_code: '12345678'
       serial: '0123456789ABCDE'
   ```

   Upgrading from `config.py`? Start the bot once without a `config.yml` and it will create one
   from your `config.py`, which can then be deleted.

   The owner can change every setting except the bot token, `owner_id` (use `/staff edit owner`)
   and the printers (use `/printer`) at runtime with `/config`, and changes take effect without a
   restart. The bot rewrites `config.yml` when settings change, so hand-written comments are not kept.
   - `/config set <key> <value>` for `chat_id`, `status_chat_id`, `log_chat_id` (`none` turns the
     log chat off), `update_interval`, `stats_log_interval` (`none` turns the report off) and
     `auto_restart_printers` (`true`/`false`).
   - `thread_id`, `status_thread_id` and `log_thread_id` set just the topic of those chats
     (`none` for no topic).
   - `/config here <chat_id|status_chat_id|log_chat_id>`, sent in the target chat or forum topic,
     sets that chat and topic without having to look up their ids.
   - `/config disable <command>` and `/config enable <command>` change `disabled_commands`.

   Every change is posted to the log chat.

   `update_interval` must be at least 1 second, but has no upper limit. `/config set update_interval inf`
   is accepted and stops printer polling, notifications and the status message. Setting it back with
   `/config` doesn't help, because the monitor is already waiting out the old interval, and the value is
   saved to `config.yml`, so it also survives restarts. To undo it, stop the bot, change
   `update_interval` in `config.yml` by hand, and start it again.

   `disabled_commands` turns individual commands off: set `print`, `speed`, `skip`, `staff`,
   `forcereclaim`, `restart`, `restartall` or `home` to `true`. Disabled commands are hidden from
   `/help` and the command menu and reply that they are disabled. `staff: true` also leaves the
   owner as the only role: admins and mods are treated as ordinary users, but their roles stay in
   `data.json` and come back when `staff` is set to `false` again.

   Changes made with `/config disable|enable` take effect at once; edits to `config.yml` by hand
   take effect after a restart.

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
- Write the bot's PID to `telebambu.pid` so `./stop.sh` can shut it down cleanly

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

`[printer]` is optional when you have exactly one print claimed. Users can be given as `@username`
(once the bot has seen them), a numeric user id, or by replying to one of their messages.
`/help` only lists the commands your role can use.

**Everyone**
- `/help` - List the commands you can use
- `/info` - Summary of all your claimed and watched prints, in the status message style
- `/info <printer>` - Progress, time remaining, layer, file name, temperatures, speed, loaded filament (AMS slots and external spool, with % left) and pending notifications for a print
- `/claim <printer>` - Claim an unclaimed print without its button
- `/unclaim [printer]` - Release your claim on a print
- `/notify [printer] <layer>` or `/notify [printer] <percent>%` - One-time notification with a camera snapshot at a milestone
- `/notify_every [printer] layers|time|percent <number>` - Recurring camera snapshots during a print (`off` to disable)
- `/notify_user <printer> <user> [user ...]` - Also notify other users about a claimed print (`remove` to stop, no users to list); the claimer and anyone already notified can use it
- `/stop_at [printer] <layer>` - Stop the print automatically at a layer (`off` to cancel)
- `/pause [printer]` / `/resume [printer]` - Pause or resume your print
- `/speed [printer] <silent|standard|sport|ludicrous>` - Change your print's speed profile (no value shows the current one)
- `/skip [printer]` - List the objects on the plate being printed; `/skip <printer> <object> [object ...]` stops printing those objects (read from the print's 3MF on the printer over FTP)
- `/camera [printer]` - Fresh camera snapshot; in your DM it has a Refresh button
- `/light [printer]` - Toggle the printer light
- `/staff [list [user]]` - See the owner, admins and mods, or one user's role

**Admins** (the owner can do all of these too)
- `/camera`, `/light`, `/stop_at`, `/pause`, `/resume`, `/speed` and `/skip` work on any printer
- `/forcereclaim <printer> <user>` - Give a print to another user (logged)
- `/forceunclaim <printer>` - Unclaim someone else's print so anyone can claim it (logged)
- `/restart <printer> [printer ...]` / `/restartall` - Reboot and reconnect printers
- `/home <printer> [printer ...]` - Home the axes of idle printers (refused while printing)
- `/print <printer> [plate] [ams|ext]` - Remote printing, for testing: send a sliced `.3mf` (up to 20 MB) with this as its caption, or reply to one with it. The bot uploads it to the printer's SD card and starts the plate. Filament N in the project is printed from AMS tray N; `ext` uses the external spool instead
- `/printer list|add|edit|del` - Manage printers in `config.yml`, e.g. `/printer add 25 AA:BB:CC:DD:EE:FF 192.168.1.125 12345678 SERIAL`, `/printer edit 25 ip 192.168.1.126`
- `/staff edit <mod|user> <user>` - Add or remove mods

**Owner**
- `/staff edit <admin|mod|user> <user>` - Add or remove admins and mods
- `/staff edit owner <user>` - Transfer ownership (asks for confirmation; you become an admin)
- `/config` / `/config set <key> <value>` - Show or change settings (chats, topics, intervals, `auto_restart_printers`)
- `/config here <chat_id|status_chat_id|log_chat_id>` - Point that chat target at the chat and topic the command is sent in
- `/config disable <command>` / `/config enable <command>` - Turn a command off or on

Role changes, force reclaims, restarts and printer/config changes are posted to the log chat.
Mods have no extra permissions yet.

### Interactive Buttons

When a print starts, the bot sends a message with a **Claim Print** button. Claiming a print allows you to:
- Receive the finish notification directly
- Choose between chat or DM delivery
- Enable/disable Layer 2 progress notifications
- Access the printer's camera, with a Refresh button on images in your DM

## Project Structure

```
telebambu/
├── main.py              # Application entry point
├── settings.py          # Loads and saves config.yml
├── config.yml           # Configuration (create from config_example.yml)
├── config_example.yml   # Configuration template
├── requirements.txt     # Python dependencies
├── start.sh             # Shell script to run the bot
├── bot/                 # Telegram bot module
│   ├── telegram_bot.py  # Bot initialization
│   ├── handlers.py      # Command and callback handlers
│   ├── messages.py      # Message sending service
│   └── ui.py            # Shared message texts and inline keyboards
├── printers/            # Printer management module
│   ├── manager.py       # Printer connection manager
│   └── monitor.py       # State monitoring loop
└── data/                # Data persistence module
    └── storage.py       # Session and preference storage
```

## License

This project is licensed under the GNU Affero General Public License v3.0. See [LICENSE](LICENSE) for details.
