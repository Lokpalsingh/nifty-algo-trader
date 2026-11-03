"""
The live settings.json must survive the test suite.

Between 15 and 26 Oct 2026 it did not. tests/test_engine_switch.py switches strategy,
Engine.update_settings calls Settings.save(), and the save went to the project's own
settings.json - putting orb_narrow_pct back to its default of 30 on every test run.
Eleven days of trading a configuration nobody had chosen, and the only reason it was
found was that the narrow-day line in the log did not match the daily ranges.

Every test here asks one of two questions: can a test redirect the settings file, and
is the real one still untouched afterwards?
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trader.config as config
from tests.isolation import use_temp_settings
from trader.config import LIVE_SETTINGS_FILE, Settings


class SettingsFileTestCase(unittest.TestCase):
    def setUp(self):
        self.live_before = (LIVE_SETTINGS_FILE.read_bytes()
                            if LIVE_SETTINGS_FILE.exists() else None)
        config.LAST_TEST_DIVERSION = None

    def assert_live_file_untouched(self):
        after = LIVE_SETTINGS_FILE.read_bytes() if LIVE_SETTINGS_FILE.exists() else None
        self.assertEqual(self.live_before, after,
                         "the project's own settings.json was modified by a test")


class TestRedirection(SettingsFileTestCase):
    def test_a_redirected_save_goes_to_the_temp_file(self):
        temp = use_temp_settings(self)
        Settings(orb_narrow_pct=50.0).save()
        self.assertTrue(temp.exists())
        self.assertIn('"orb_narrow_pct": 50.0', temp.read_text())
        self.assert_live_file_untouched()

    def test_a_redirected_load_reads_the_temp_file(self):
        temp = use_temp_settings(self)
        Settings(orb_narrow_pct=50.0, strategy="narrow_orb").save()
        self.assertEqual(Settings.load().orb_narrow_pct, 50.0)
        self.assertEqual(Settings.load().strategy, "narrow_orb")
        self.assert_live_file_untouched()

    def test_a_redirected_load_creates_the_file_when_missing(self):
        temp = use_temp_settings(self)
        self.assertFalse(temp.exists())
        Settings.load()
        self.assertTrue(temp.exists(), "load() should create the file it was pointed at")
        self.assert_live_file_untouched()

    def test_the_redirect_is_undone_afterwards(self):
        """addCleanup puts it back, so one test cannot leak into the next."""
        original = config.SETTINGS_FILE

        class Inner(unittest.TestCase):
            def test_it(inner):
                use_temp_settings(inner)
                self.assertNotEqual(config.SETTINGS_FILE, original)

        import io
        unittest.TextTestRunner(stream=io.StringIO()).run(
            unittest.TestLoader().loadTestsFromTestCase(Inner))
        self.assertEqual(config.SETTINGS_FILE, original)


class TestTheSafetyNet(SettingsFileTestCase):
    """Belt and braces: even a test that forgets to redirect cannot write the real
    file, because save() notices a test runner is loaded and diverts."""

    def test_an_unredirected_save_is_diverted_not_written(self):
        Settings(orb_narrow_pct=30.0).save()
        self.assert_live_file_untouched()
        self.assertEqual(config.LAST_TEST_DIVERSION, LIVE_SETTINGS_FILE,
                         "the diversion should have been recorded")

    def test_the_diverted_content_is_still_written_somewhere(self):
        """Diverting must not silently drop the write - a test asserting on the saved
        file would then fail for a confusing reason."""
        Settings(orb_narrow_pct=42.0).save()
        scratch = Path(tempfile.gettempdir()) / "algo_trader_settings_under_test.json"
        self.assertTrue(scratch.exists())
        self.assertIn('"orb_narrow_pct": 42.0', scratch.read_text())

    def test_an_explicit_path_is_always_honoured(self):
        """A test that passes its own path knows what it is doing; never divert that."""
        mine = Path(tempfile.mkdtemp()) / "mine.json"
        Settings(orb_narrow_pct=50.0).save(mine)
        self.assertIn('"orb_narrow_pct": 50.0', mine.read_text())
        self.assertIsNone(config.LAST_TEST_DIVERSION)
        self.assert_live_file_untouched()

    def test_a_redirected_save_is_not_diverted(self):
        temp = use_temp_settings(self)
        Settings().save()
        self.assertIsNone(config.LAST_TEST_DIVERSION,
                          "a redirected save is already safe; no diversion needed")
        self.assertTrue(temp.exists())


class TestThePathIsResolvedAtCallTime(SettingsFileTestCase):
    """The original bug under the bug. `path: Path = SETTINGS_FILE` is evaluated once
    when the module is imported, so the live path was welded into the signature and
    redirecting it did nothing at all - quietly."""

    def test_save_has_no_baked_in_default(self):
        import inspect
        for method in (Settings.save, Settings.load):
            default = inspect.signature(method).parameters["path"].default
            self.assertIsNone(default, f"{method.__name__} still binds a path at import")

    def test_changing_settings_file_actually_changes_where_save_goes(self):
        first = Path(tempfile.mkdtemp()) / "a.json"
        second = Path(tempfile.mkdtemp()) / "b.json"
        original = config.SETTINGS_FILE
        self.addCleanup(setattr, config, "SETTINGS_FILE", original)

        config.SETTINGS_FILE = first
        Settings(orb_narrow_pct=30.0).save()
        config.SETTINGS_FILE = second
        Settings(orb_narrow_pct=50.0).save()

        self.assertIn('"orb_narrow_pct": 30.0', first.read_text())
        self.assertIn('"orb_narrow_pct": 50.0', second.read_text())
        self.assert_live_file_untouched()


if __name__ == "__main__":
    unittest.main(verbosity=2)
