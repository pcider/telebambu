#!/bin/sh

cd "$(dirname "$0")"

echo "--- BOT RESTARTED at $(date) ---" >> main.log
python3 -u main.py >main.log 2>main.log