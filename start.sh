#!/bin/sh

cd "$(dirname "$0")"

echo "--- BOT RESTARTED at $(date) ---" >> telebambu.log
# exec replaces this shell with python, so $$ is the bot's PID for stop.sh
echo $$ > telebambu.pid
exec python3 -u main.py >>telebambu.log 2>&1
