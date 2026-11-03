#!/bin/bash
# Double-click this ONCE. After that the trading day runs by itself, every
# weekday morning, with no Terminal and no commands.
set -e
cd "$(dirname "$0")"
PROJECT="$(pwd)"
PYTHON="$PROJECT/venv/bin/python3"
PLIST="$HOME/Library/LaunchAgents/com.algotrader.autopilot.plist"
LABEL="com.algotrader.autopilot"

echo "Project : $PROJECT"
if [ ! -x "$PYTHON" ]; then
  echo "ERROR: $PYTHON not found. Open Terminal in this folder and run:"
  echo "   python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  read -p "Press return to close."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/data"

{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
  echo '<plist version="1.0"><dict>'
  echo "  <key>Label</key><string>$LABEL</string>"
  echo '  <key>ProgramArguments</key><array>'
  echo '    <string>/usr/bin/caffeinate</string><string>-i</string>'
  echo "    <string>$PYTHON</string><string>$PROJECT/autopilot.py</string><string>--once</string>"
  echo '  </array>'
  echo "  <key>WorkingDirectory</key><string>$PROJECT</string>"
  echo '  <key>StartCalendarInterval</key><array>'
  for day in 1 2 3 4 5; do
    echo "    <dict><key>Weekday</key><integer>$day</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>45</integer></dict>"
  done
  echo '  </array>'
  echo '  <key>RunAtLoad</key><false/>'
  echo "  <key>StandardOutPath</key><string>$PROJECT/data/autopilot.log</string>"
  echo "  <key>StandardErrorPath</key><string>$PROJECT/data/autopilot.log</string>"
  echo '  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>'
  echo '</dict></plist>'
} > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo
echo "Installed. From now on, every weekday at 08:45 your Mac starts the autopilot:"
echo "   logs in to Kotak, fetches data, trades the day in PAPER mode, reports at 15:20."
echo "Nothing to type, nothing to click. The log is at data/autopilot.log"
echo
echo "Two things to know:"
echo " * The Mac must be ON (lid open or connected to power with sleep allowed)."
echo "   To wake it automatically on weekdays, run this once in Terminal:"
echo "     sudo pmset repeat wakeorpoweron MTWRF 08:40:00"
echo " * To stop all of it, double-click uninstall_autopilot.command"
echo
read -p "Press return to close."
