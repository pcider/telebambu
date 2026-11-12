#!/bin/sh

cd "$(dirname "$0")"
#sleep 60
echo "--- BOT RESTARTED at $(date) ---" >> telebambu.log
# Run in the background and record the bot's own PID ($!) for stop.sh
python3 -u main.py >>telebambu.log 2>&1 &
echo $! > telebambu.pid
