"""
generate_session.py
--------------------
Helper script to generate an optional Telegram SESSION_STRING.

Using SESSION_STRING is optional; without it the bot runs with
BOT_TOKEN over MTProto, which is enough for most use cases.
If you want to use a user account (ideally Telegram Premium for a
4GB upload cap) to upload larger files, run this script and put the
resulting value into the SESSION_STRING environment variable.

Usage:
    python generate_session.py

Security note: the generated SESSION_STRING is as sensitive as your
account password. Never store it anywhere but a secure env var store (e.g. Railway Variables).
"""

from __future__ import annotations

import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    print("=" * 60)
    print("Generate a Telegram SESSION_STRING (optional)")
    print("=" * 60)

    api_id = int(os.getenv("API_ID") or input("Enter API_ID: ").strip())
    api_hash = os.getenv("API_HASH") or input("Enter API_HASH: ").strip()

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()
        print("\n✅ Login successful!\n")
        print("Put the value below into the SESSION_STRING environment variable:\n")
        print(session_string)
        print("\n⚠️ Keep this value secret and never share it.")


if __name__ == "__main__":
    asyncio.run(main())
