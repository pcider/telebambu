#!/bin/sh

cd "$(dirname "$0")"

# Only kill the bot itself, not every python process on the machine
kill "$(cat telebambu.pid)" && rm -f telebambu.pid
