#!/usr/bin/env bash
set -euo pipefail

FILENAME=servo-x86_64-linux-gnu.tar.gz
URL="https://download.servo.org/nightly/linux/$FILENAME"

rm -rf "$FILENAME" servo
wget "$URL"
tar -xzf "$FILENAME"
