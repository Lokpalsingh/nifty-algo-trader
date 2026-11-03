#!/bin/bash
# Double-click to stop the automatic daily run.
PLIST="$HOME/Library/LaunchAgents/com.algotrader.autopilot.plist"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Autopilot removed. Nothing will start by itself any more."
echo "(To also cancel the automatic wake: sudo pmset repeat cancel)"
read -p "Press return to close."
