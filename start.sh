#!/bin/sh

cd "$(dirname "$0")"

echo "--- BOT RESTARTED at $(date) ---" >> telebambu.log
python3 -u main.py >>telebambu.log 2>>telebambu.log
echo $$ > telebambu.pid