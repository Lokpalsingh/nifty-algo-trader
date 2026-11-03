"""
Tests for setup_telegram.py.

The dangerous part of this script is not Telegram - it is that it writes to .env, the
file holding the Kotak consumer key, MPIN and TOTP secret. A careless rewrite there
would lock the bot out of the broker. So every test below is really asking the same
question: did anything other than the two Telegram lines change?
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from setup_telegram import chat_ids_from, write_env

REAL_ENV = """# Kotak Neo
KOTAK_CONSUMER_KEY=abc123
KOTAK_CONSUMER_SECRET=def456
KOTAK_MOBILE_NUMBER=+919999999999
KOTAK_UCC=U1
KOTAK_MPIN=123456
KOTAK_TOTP_SECRET=JBSWY3DPEHPK3PXP

# Safety
LIVE_TRADING=false
"""


class TestWritingEnv(unittest.TestCase):
    def test_the_two_keys_are_added(self):
        out = write_env(REAL_ENV, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": 42})
        self.assertIn("TELEGRAM_BOT_TOKEN=tok", out)
        self.assertIn("TELEGRAM_CHAT_ID=42", out)

    def test_nothing_else_is_touched(self):
        """The whole point. Every original line must survive, byte for byte."""
        out = write_env(REAL_ENV, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": 42})
        for line in REAL_ENV.splitlines():
            self.assertIn(line, out.splitlines(), f"lost: {line!r}")

    def test_comments_and_blank_lines_survive(self):
        out = write_env(REAL_ENV, {"TELEGRAM_CHAT_ID": 7})
        self.assertIn("# Kotak Neo", out)
        self.assertIn("# Safety", out)
        self.assertIn("", out.splitlines())

    def test_order_is_preserved(self):
        out = write_env(REAL_ENV, {"TELEGRAM_CHAT_ID": 7}).splitlines()
        self.assertLess(out.index("KOTAK_CONSUMER_KEY=abc123"), out.index("KOTAK_MPIN=123456"))

    def test_an_existing_value_is_replaced_in_place(self):
        before = "A=1\nTELEGRAM_CHAT_ID=old\nB=2\n"
        out = write_env(before, {"TELEGRAM_CHAT_ID": "new"}).splitlines()
        self.assertEqual(out, ["A=1", "TELEGRAM_CHAT_ID=new", "B=2"])
        self.assertNotIn("TELEGRAM_CHAT_ID=old", out)

    def test_no_duplicate_key_is_created(self):
        once = write_env(REAL_ENV, {"TELEGRAM_CHAT_ID": 1})
        twice = write_env(once, {"TELEGRAM_CHAT_ID": 2})
        self.assertEqual(sum(1 for l in twice.splitlines()
                             if l.startswith("TELEGRAM_CHAT_ID=")), 1)
        self.assertIn("TELEGRAM_CHAT_ID=2", twice)

    def test_spaces_around_equals_are_handled(self):
        out = write_env("TELEGRAM_CHAT_ID = 5\n", {"TELEGRAM_CHAT_ID": 9})
        self.assertEqual(out.strip(), "TELEGRAM_CHAT_ID=9")

    def test_a_similarly_named_key_is_not_clobbered(self):
        before = "TELEGRAM_CHAT_ID_OLD=keepme\n"
        out = write_env(before, {"TELEGRAM_CHAT_ID": 1})
        self.assertIn("TELEGRAM_CHAT_ID_OLD=keepme", out)
        self.assertIn("TELEGRAM_CHAT_ID=1", out)

    def test_a_commented_out_key_is_left_as_a_comment(self):
        before = "# TELEGRAM_CHAT_ID=disabled\n"
        out = write_env(before, {"TELEGRAM_CHAT_ID": 1})
        self.assertIn("# TELEGRAM_CHAT_ID=disabled", out)
        self.assertIn("TELEGRAM_CHAT_ID=1", out)

    def test_a_file_with_no_trailing_newline(self):
        out = write_env("A=1", {"TELEGRAM_CHAT_ID": 1})
        self.assertEqual(out, "A=1\nTELEGRAM_CHAT_ID=1\n")

    def test_the_result_always_ends_with_exactly_one_newline(self):
        for text in ("A=1", "A=1\n", "A=1\n\n\n"):
            out = write_env(text, {"TELEGRAM_CHAT_ID": 1})
            self.assertTrue(out.endswith("\n"))
            self.assertFalse(out.endswith("\n\n"))

    def test_an_empty_file_still_works(self):
        out = write_env("", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": 1})
        self.assertIn("TELEGRAM_BOT_TOKEN=t", out)
        self.assertIn("TELEGRAM_CHAT_ID=1", out)


class TestFindingTheChatId(unittest.TestCase):
    def test_a_normal_message(self):
        updates = [{"message": {"chat": {"id": 12345, "first_name": "Trader"}}}]
        self.assertEqual(chat_ids_from(updates), {12345: "Trader"})

    def test_nothing_sent_yet(self):
        self.assertEqual(chat_ids_from([]), {})
        self.assertEqual(chat_ids_from(None), {})

    def test_repeat_messages_are_one_chat(self):
        updates = [{"message": {"chat": {"id": 7, "first_name": "Trader"}}},
                   {"message": {"chat": {"id": 7, "first_name": "Trader"}}}]
        self.assertEqual(len(chat_ids_from(updates)), 1)

    def test_two_people_are_two_chats(self):
        updates = [{"message": {"chat": {"id": 7, "first_name": "Trader"}}},
                   {"message": {"chat": {"id": 8, "first_name": "Someone"}}}]
        self.assertEqual(len(chat_ids_from(updates)), 2)

    def test_edited_messages_and_channel_posts_count(self):
        self.assertEqual(chat_ids_from([{"edited_message": {"chat": {"id": 1}}}]), {1: ""})
        self.assertEqual(chat_ids_from([{"channel_post": {"chat": {"id": 2, "title": "Alerts"}}}]),
                         {2: "Alerts"})

    def test_junk_is_skipped_without_crashing(self):
        updates = [{}, {"message": {}}, {"message": {"chat": {}}},
                   {"message": {"chat": {"id": 5}}}, {"other": 1}]
        self.assertEqual(chat_ids_from(updates), {5: ""})

    def test_a_chat_id_can_be_negative(self):
        """Group chats have negative ids, and `if chat.get('id')` would drop a zero."""
        self.assertEqual(chat_ids_from([{"message": {"chat": {"id": -100123}}}]), {-100123: ""})


if __name__ == "__main__":
    unittest.main(verbosity=2)
