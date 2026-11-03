"""
Turn on Telegram alerts, without editing .env by hand.

    python3 setup_telegram.py

It asks for your bot token, finds your chat id for you, writes both into .env, and
sends a test message. Your existing Kotak settings in .env are kept exactly as they
are, and a copy is saved to .env.backup before anything is written.

The token is typed invisibly and is never printed, never logged and never shown in
your terminal history - so the output of this script is safe to paste to anyone,
including into a chat with Claude.

Why bother: alerts are the only thing that reaches you when the bot takes a trade,
loses its connection or hits the daily loss limit. Without them a bot with real money
can fail silently, which is the one failure mode worth real effort to avoid.
"""
import getpass
import re
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
ENV = PROJECT / ".env"
BACKUP = PROJECT / ".env.backup"
API = "https://api.telegram.org"


def ask_yes(question: str) -> bool:
    return input(f"{question} [y/n] ").strip().lower().startswith("y")


# --------------------------------------------------------------------------- #
# Talking to Telegram
# --------------------------------------------------------------------------- #
def call(token: str, method: str, **params):
    """Returns (ok, payload_or_error_text). Never raises, never prints the token."""
    try:
        import requests
    except ImportError:
        return False, "the requests library is missing - run: pip install -r requirements.txt"
    try:
        r = requests.get(f"{API}/bot{token}/{method}", params=params or None, timeout=20)
    except Exception as e:
        return False, f"could not reach Telegram ({type(e).__name__}). Check your internet."
    try:
        body = r.json()
    except Exception:
        return False, f"Telegram replied {r.status_code} with something that was not JSON"
    if not body.get("ok"):
        return False, str(body.get("description") or f"Telegram replied {r.status_code}")
    return True, body.get("result")


def chat_ids_from(updates) -> dict:
    """{chat_id: display name} out of a getUpdates result."""
    found = {}
    for update in updates or []:
        for key in ("message", "edited_message", "channel_post"):
            chat = (update.get(key) or {}).get("chat")
            if chat and chat.get("id") is not None:
                found[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    return found


# --------------------------------------------------------------------------- #
# Writing .env without breaking what is already there
# --------------------------------------------------------------------------- #
def write_env(text: str, values: dict) -> str:
    """Return `text` with each key in `values` set, every other line untouched.

    A key already present is replaced in place, keeping its position and any
    comments elsewhere in the file. A key that is absent is appended."""
    lines = text.splitlines()
    for key, value in values.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
def main():
    print(__doc__.strip().split("Why bother")[0].strip())
    print("-" * 70)

    if not ENV.exists():
        print(f"No .env file at {ENV}. Set up Kotak first; this script only adds Telegram.")
        return 1

    print("\nStep 1 of 3 - the bot token")
    print("  In Telegram, message @BotFather, send /newbot, and copy the long token")
    print("  it replies with. Paste it below. It will not appear on screen.")
    token = getpass.getpass("  token: ").strip()
    if not token:
        print("\nNothing entered. Stopped, nothing changed.")
        return 1

    ok, result = call(token, "getMe")
    if not ok:
        print(f"\nThat token did not work: {result}")
        print("Nothing was changed. Copy the whole token from BotFather and try again.")
        return 1
    bot_name = (result or {}).get("username") or "your bot"
    print(f"  token works - this is @{bot_name}")

    print("\nStep 2 of 3 - your chat id")
    print(f"  Open @{bot_name} in Telegram, press Start, and send it any message.")
    print("  It will not reply. That is normal - it only sends, it never answers.")
    chats = {}
    while not chats:
        input("  Press return here once you have sent it a message... ")
        ok, updates = call(token, "getUpdates")
        if not ok:
            print(f"  Could not read messages: {updates}")
            if not ask_yes("  Try again?"):
                print("\nStopped, nothing changed.")
                return 1
            continue
        chats = chat_ids_from(updates)
        if not chats:
            print(f"  Telegram has not seen your message yet. Make sure you messaged")
            print(f"  @{bot_name} and not @BotFather.")
            if not ask_yes("  Try again?"):
                print("\nStopped, nothing changed.")
                return 1

    if len(chats) == 1:
        chat_id, who = next(iter(chats.items()))
    else:
        print("  More than one chat has messaged this bot:")
        options = list(chats.items())
        for n, (cid, who) in enumerate(options, 1):
            print(f"    {n}. {who or cid}")
        choice = input(f"  Which one is you? [1-{len(options)}] ").strip()
        try:
            chat_id, who = options[int(choice) - 1]
        except Exception:
            print("\nNot a valid choice. Stopped, nothing changed.")
            return 1
    print(f"  found: {who or chat_id}")

    print("\nStep 3 of 3 - saving and testing")
    original = ENV.read_text(encoding="utf-8")
    shutil.copy2(ENV, BACKUP)
    print(f"  your old .env is saved as {BACKUP.name}")
    ENV.write_text(write_env(original, {"TELEGRAM_BOT_TOKEN": token,
                                        "TELEGRAM_CHAT_ID": chat_id}), encoding="utf-8")
    print("  .env updated (Kotak settings untouched)")

    ok, _ = call(token, "sendMessage", chat_id=chat_id,
                 text="Your algo trading bot is connected. "
                      "You will get a message here for every trade, stop-loss and problem.")
    if not ok:
        print("\n  The settings were saved, but the test message did not send.")
        print("  Run python3 status.py - it will tell you if Telegram is on.")
        return 1

    print("  test message sent")
    print("\nDone. Check your phone - you should have a message from "
          f"@{bot_name}.")
    print("From now on you get a message for every entry, exit, stop-loss, the daily")
    print("loss limit, connection problems, and the 15:20 report.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Nothing was changed.")
        sys.exit(1)
