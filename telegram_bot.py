"""
Eva Bot — Telegram User Account (Telethon)
Logs ultra-précis pour monitoring complet
"""

import asyncio
import re
import httpx
import os
import sys
import json
from datetime import datetime

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from openai import OpenAI
from dotenv import load_dotenv
from soul import SOUL

load_dotenv()

# ─── Config ───────────────────────────────────────────────────
API_ID   = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE    = os.getenv("TELEGRAM_PHONE")
SESSION  = "eva_session"

client_openai = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(verify=False),
)

histories   = {}
HISTORY_FILE = "histories.json"

# ─── Logs ─────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(tag, msg, extra=""):
    line = f"[{ts()}] [{tag}] {msg}"
    if extra:
        line += f"  |  {extra}"
    print(line, flush=True)

# ─── Persistance historique ───────────────────────────────────

def load_histories():
    global histories
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            histories = json.load(f)
        log("MEM", f"Historique chargé — {len(histories)} conversation(s) en mémoire")
    else:
        log("MEM", "Aucun historique existant — démarrage à zéro")

def save_histories():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(histories, f, ensure_ascii=False, indent=2)

# ─── Génération réponse OpenAI ────────────────────────────────

def get_eva_response(user_id: str, username: str, user_message: str) -> str:
    if user_id not in histories:
        histories[user_id] = []
        log("NEW", f"Nouvelle conversation", f"user={username} id={user_id}")

    turn = len(histories[user_id]) // 2 + 1
    log("GPT", f"Génération réponse (tour {turn})", f"user={username}")

    histories[user_id].append({"role": "user", "content": user_message})
    messages = [{"role": "system", "content": SOUL}] + histories[user_id]

    response = client_openai.chat.completions.create(
        model="gpt-4o",
        max_tokens=400,
        messages=messages,
    )

    reply = response.choices[0].message.content
    histories[user_id].append({"role": "assistant", "content": reply})
    save_histories()

    tokens_used = response.usage.total_tokens
    log("OK ", f"Réponse générée ({tokens_used} tokens)", f"user={username}")

    return reply

# ─── Délai réaliste ───────────────────────────────────────────

async def typing_delay(text: str):
    words = len(text.split())
    delay = min(max(words * 0.4 + 1.0, 1.5), 6.0)
    log("DLY", f"Délai frappe {delay:.1f}s", f"({words} mots reçus)")
    await asyncio.sleep(delay)

# ─── Bot Telegram ─────────────────────────────────────────────

bot = TelegramClient(SESSION, API_ID, API_HASH)

MY_ID = 8086331281  # ID du compte Eva — jamais répondre à soi-même

@bot.on(events.NewMessage(incoming=True))
async def handle_message(event):
    if not event.is_private:
        return

    sender   = await event.get_sender()

    # Ignorer ses propres messages et les bots Telegram officiels
    if sender.id == MY_ID or getattr(sender, "bot", False):
        return
    user_id  = str(sender.id)
    username = getattr(sender, "username", None) or f"id:{sender.id}"
    name     = getattr(sender, "first_name", "") or username
    text     = (event.message.message or "").strip()

    if not text:
        log("WRN", f"Message vide ignoré", f"user={username}")
        return

    log("IN ", f"REÇU de {name} (@{username})", f'"{text}"')

    await typing_delay(text)

    async with bot.action(event.chat_id, "typing"):
        try:
            reply = get_eva_response(user_id, username, text)
        except Exception as e:
            log("ERR", f"Erreur OpenAI", str(e))
            return
        await asyncio.sleep(0.5)

    await event.respond(reply)
    log("OUT", f"ENVOYÉ à {name} (@{username})", f'"{reply}"')
    log("-" * 60, "")

# ─── Entrée ───────────────────────────────────────────────────

async def main():
    load_histories()

    log("BOT", "Démarrage Eva Bot...")
    log("TEL", f"Numéro : {PHONE}")
    log("KEY", f"API ID : {API_ID}")

    def code_callback():
        code_file = "telegram_code.txt"
        if os.path.exists(code_file):
            with open(code_file, "r") as f:
                code = f.read().strip()
            os.remove(code_file)
            log("OK ", f"Code lu depuis fichier : {code}")
            return code
        return input("Code Telegram : ")

    try:
        await bot.start(phone=PHONE, code_callback=code_callback)
    except SessionPasswordNeededError:
        password = input("Mot de passe 2FA Telegram : ")
        await bot.sign_in(password=password)

    me = await bot.get_me()
    log("OK ", f"Connectée en tant que : {me.first_name} (@{me.username})", f"id={me.id}")
    log("RDY", "En attente de messages...\n")

    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
