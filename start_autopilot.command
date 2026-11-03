#!/bin/bash
# Double-click this file to run the trading day by itself.
# It keeps the Mac awake while it runs (caffeinate) and stops when you close the window.
cd "$(dirname "$0")"
source venv/bin/activate
exec caffeinate -i python3 autopilot.py "$@"
