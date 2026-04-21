"""Exporte eva_session.session → string pour Railway."""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
import os
from dotenv import load_dotenv

load_dotenv()

API_ID   = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")

async def main():
    client = TelegramClient("eva_session", API_ID, API_HASH)
    await client.connect()
    string = StringSession.save(client.session)
    print("\n=== TELEGRAM_SESSION_STRING ===")
    print(string)
    print("=== Copie cette valeur dans Railway → Variables ===\n")
    await client.disconnect()

asyncio.run(main())
