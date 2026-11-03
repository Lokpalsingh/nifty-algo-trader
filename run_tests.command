#!/bin/bash
# Run the test suite without letting it eat your settings.
#
# tests/test_engine_switch.py calls Engine.update_settings(), which calls
# Settings.save(), which writes the REAL settings.json. On 26 Oct 2026 that had been
# quietly resetting orb_narrow_pct from 50 back to 30 on every test run for eleven
# days. This script saves your settings, runs the tests, and puts them back.
#
# It is a seatbelt, not a repair: the tests should not be touching that file at all.
# Until they are fixed, use this instead of running unittest directly.
#
#     bash run_tests.command
cd "$(dirname "$0")" || exit 1

PYTHON="./venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="python3"

if [ -f settings.json ]; then
  cp settings.json .settings.json.before_tests
  echo "settings.json saved. Current narrow %:"
  grep orb_narrow_pct settings.json || echo "  (not set)"
  echo
fi

"$PYTHON" -m unittest discover -s tests
result=$?

if [ -f .settings.json.before_tests ]; then
  if cmp -s settings.json .settings.json.before_tests; then
    echo
    echo "settings.json untouched by the tests."
    rm -f .settings.json.before_tests
  else
    mv .settings.json.before_tests settings.json
    echo
    echo "!! The tests changed settings.json. Put back to:"
    grep orb_narrow_pct settings.json
  fi
fi

exit $result
