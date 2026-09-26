#!/bin/sh

cd "$(dirname "$0")"

kill "$(cat telebambu.pid)" && rm -f telebambu.pid
