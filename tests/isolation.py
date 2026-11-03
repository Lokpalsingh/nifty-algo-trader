"""
Keeping tests away from the files the running bot depends on.

Two bugs in this project came from the same mistake, eleven days apart:

  16 Oct 2026  a test read the real data/history/NIFTY_D.csv, so it passed on a
               machine with no history and failed on the one that had it
  26 Oct 2026  a test wrote the real settings.json, resetting orb_narrow_pct from
               50 back to 30 on every test run for eleven days

A test that touches a live file is not a test, it is a coin flip on which machine
you run it. Use these helpers in setUp.
"""
import tempfile
from pathlib import Path

import trader.config as config


def use_temp_settings(test_case) -> Path:
    """Point Settings.load() and Settings.save() at a throwaway file.

    Call it in setUp and ignore the return value unless you want to inspect the
    file. Cleanup registers itself, so there is nothing to undo in tearDown:

        def setUp(self):
            use_temp_settings(self)
    """
    original = config.SETTINGS_FILE
    temp = Path(tempfile.mkdtemp()) / "settings.json"
    config.SETTINGS_FILE = temp
    test_case.addCleanup(_restore_settings, original)
    return temp


def _restore_settings(original: Path) -> None:
    config.SETTINGS_FILE = original
