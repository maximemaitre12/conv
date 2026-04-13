"""
Eva Bot — Telegram User Account (Telethon)
Délais humains + vocaux via ZvukoGram TTS
"""

import asyncio
import random
import re
import httpx
import os
import sys
import json
import time as _time
import io
import atexit
import msvcrt
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from openai import OpenAI
from dotenv import load_dotenv
from soul import SOUL
from zvukogram_agent import get_voice_agent

# Force UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

# ─── Single-instance lock (empêche deux bots simultanés → crash SQLite) ───────
_LOCK_FILE = "eva_bot.lock"
_lock_fh = None

def _acquire_instance_lock():
    global _lock_fh
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        _lock_fh.close()
        print("[FATAL] Une autre instance du bot tourne déjà. Arrêt.", flush=True)
        sys.exit(1)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()
    atexit.register(_release_instance_lock)

def _release_instance_lock():
    global _lock_fh
    if _lock_fh:
        try:
            _lock_fh.seek(0)
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        _lock_fh.close()
        try:
            os.remove(_LOCK_FILE)
        except OSError:
            pass
        _lock_fh = None

_acquire_instance_lock()

# ─── Config ───────────────────────────────────────────────────
API_ID   = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE    = os.getenv("TELEGRAM_PHONE")
SESSION  = "eva_session"

MY_ID = 8086331281  # ID du compte Eva

# Probabilité qu'Eva envoie un vocal spontanément (après tour 4)
VOICE_SPONTANEOUS_PROB = float(os.getenv("VOICE_PROB", "0.18"))

client_openai = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(verify=False),
)

histories    = {}
HISTORY_FILE = "histories.json"

# Utilisateurs bloqués — Eva ne leur répond pas
BLOCKED_USERS = {"623346108"}  # СОВЕТСКИЙ (@mkaafromspai)

# Regex pour ignorer les appuis de boutons LeoMatchBot (outgoing handler).
# ⚠ Ne doit PAS matcher les messages réels (ex: numéro de téléphone "0612 34 56 78").
# Règle : le message doit contenir au moins un emoji/symbole de navigation pour être ignoré.
_BUTTON_RE = re.compile(
    r"^[\d\s]*[«»←→↑↓✅❌❤️👍👎🚀🔥⭐️🎉][«»←→↑↓✅❌❤️👍👎🚀🔥⭐️🎉\d\s]*$"
    r"|^(Назад|Вперёд|Профиль|Главное меню|Меню|Фильтр|Смотреть анкеты)$",
    re.UNICODE,
)

# Utilisateurs à qui Eva a déjà envoyé un vocal (max 1 par conversation)
voice_sent_users: set[str] = set()

# ─── Logs ─────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(tag, msg, extra=""):
    line = f"[{ts()}] [{tag}] {msg}"
    if extra:
        line += f"  |  {extra}"
    print(line, flush=True)

# ─── Persistance historique ───────────────────────────────────

_VOICE_SENT_KEY = "__voice_sent_users__"

def load_histories():
    global histories, voice_sent_users
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # voice_sent_users est stocké dans une clé spéciale du fichier
        voice_sent_users = set(data.pop(_VOICE_SENT_KEY, []))
        histories = data
        log("MEM", f"Historique charge — {len(histories)} conversation(s), "
                   f"{len(voice_sent_users)} vocal(ux) deja envoye(s)")
    else:
        log("MEM", "Aucun historique — demarrage a zero")

def save_histories():
    """Sauvegarde synchrone — utiliser save_histories_async() depuis un contexte async."""
    tmp = HISTORY_FILE + ".tmp"
    data = dict(histories)
    data[_VOICE_SENT_KEY] = list(voice_sent_users)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_FILE)  # écriture atomique — évite la corruption en cas de crash

async def save_histories_async():
    """Sauvegarde non-bloquante pour l'event loop asyncio."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, save_histories)

# ─── Filtres qualité réponse ──────────────────────────────────

# Mots/phrases bannis — stems inclus pour attraper les formes fléchies russes
_BANNED_WORDS = re.compile(
    # Phrases figées
    r"приятно познакомиться"
    r"|звучит как план"
    r"|никогда не знаешь"
    r"|стоит попробовать"
    r"|лондон идеален"
    r"|столько всего"
    r"|каждый по.своему"
    r"|захватывающ"           # захватывающий вид + formes
    r"|реально впечатля"      # реально впечатляющий + formes
    r"|важно находить время"
    r"|надо находить время"
    r"|иногда так нужно"
    r"|иногда такое бывает"
    r"|город затягивает"
    r"|стоит подумать о"
    r"|если захочется дай знать"
    r"|береги себя"
    r"|хорошего дня"
    r"|не стесняйся писать"
    r"|с удовольствием"
    r"|то чувство когда"
    r"|как интересно"
    r"|это интересно"
    r"|это классно"
    # Mots seuls — stems pour couvrir toutes les formes fléchies
    r"|конечн"       # конечно, конечная...
    r"|разумеется"
    r"|я понимаю"
    r"|замечательн"  # замечательно, замечательный
    r"|здоров[ао]"   # здорово, здорова (mais pas здоровье/здоровый)
    r"|незабываем"   # незабываемо, незабываемый
    r"|восхищ"       # восхищаюсь, восхищает, восхищение
    r"|впечатля"     # впечатляет, впечатляющий
    r"|вдохновля"    # вдохновляет, вдохновляющий
    r"|потряс"       # потрясающе, потрясающий
    r"|удивительн"   # удивительно, удивительный
    r"|заряжа"       # заряжает, заряжается, заряжаться
    ,
    re.IGNORECASE | re.UNICODE,
)

# Emojis explicitement interdits (выдают бота)
_FORBIDDEN_EMOJIS = re.compile(r"[😊🙂😄🤗👍❤️]")

# Mots anglais (hors noms propres acceptés)
_ENGLISH_WORD_RE = re.compile(r"\b[a-zA-Z]{3,}\b")
_ENGLISH_WHITELIST = re.compile(
    r"\b(london|eye|the|ok|wow|hmm|lol|ok)\b"
    r"|https?://\S+",
    re.IGNORECASE,
)

def _has_banned(text: str) -> bool:
    return bool(_BANNED_WORDS.search(text))

def _has_forbidden_emoji(text: str) -> bool:
    return bool(_FORBIDDEN_EMOJIS.search(text))

def _has_english(text: str) -> bool:
    """Détecte des mots anglais non autorisés dans un message."""
    cleaned = _ENGLISH_WHITELIST.sub("", text)
    return bool(_ENGLISH_WORD_RE.search(cleaned))

def _fix_reply(text: str) -> str:
    """Post-processing : force minuscule en début de message."""
    if text and text[0].isupper():
        text = text[0].lower() + text[1:]
    return text

def _reply_is_clean(text: str) -> bool:
    """True si le message passe tous les filtres qualité."""
    return (
        not _has_banned(text)
        and not _has_forbidden_emoji(text)
        and not _has_english(text)
    )

# ─── Génération réponse OpenAI ────────────────────────────────

def get_eva_response(user_id: str, username: str, user_message: str) -> str:
    if user_id not in histories:
        histories[user_id] = []
        log("NEW", f"Nouvelle conversation", f"user={username} id={user_id}")

    turn = len(histories[user_id]) // 2 + 1
    log("GPT", f"Generation reponse (tour {turn})", f"user={username}")

    histories[user_id].append({"role": "user", "content": user_message})

    # Garde uniquement les 40 derniers messages (20 échanges) pour éviter overflow GPT
    MAX_HISTORY = 40
    if len(histories[user_id]) > MAX_HISTORY:
        histories[user_id] = histories[user_id][-MAX_HISTORY:]

    # ── Injection tour 1 : forcer "привет)" sans question ──
    _turn1_injection = {
        "role": "user",
        "content": (
            "[SYSTÈME] C'est ton PREMIER message dans cette conversation. "
            "Réponds UNIQUEMENT avec 'привет)' — rien d'autre, aucune question, "
            "aucun emoji, aucune salutation enthousiaste. Un seul mot."
        ),
    } if turn == 1 else None

    # ── Injection anti-lien double ──
    _link_in_history = any(
        "the-londoneye.com" in m.get("content", "")
        for m in histories[user_id]
        if m["role"] == "assistant"
    )
    _link_injection = {
        "role": "user",
        "content": (
            "[SYSTÈME] Tu as DÉJÀ envoyé le lien https://www.the-londoneye.com "
            "dans cette conversation. NE PAS le renvoyer une seconde fois."
        ),
    } if _link_in_history else None

    reply = ""
    for attempt in range(4):
        messages = [{"role": "system", "content": SOUL}] + histories[user_id]

        if _turn1_injection:
            messages.append(_turn1_injection)
        if _link_injection:
            messages.append(_link_injection)

        if attempt > 0:
            issues = []
            if _has_banned(reply):
                issues.append(
                    "mots interdits détectés (конечн/здоров/восхищ/заряжа/впечатля/"
                    "вдохновля/потряс/удивительн/замечательн/незабываем/"
                    "звучит как план/столько всего/каждый по-своему/"
                    "приятно познакомиться/то чувство когда/если захочется дай знать)"
                )
            if _has_forbidden_emoji(reply):
                issues.append("emoji interdit présent (😊🙂😄🤗👍❤️ — JAMAIS ces emojis)")
            if _has_english(reply):
                issues.append("mot anglais présent — parle UNIQUEMENT en russe")
            messages.append({
                "role": "user",
                "content": (
                    f"[SYSTÈME] Problème(s) dans ta réponse précédente: {'; '.join(issues)}. "
                    "Réécris-la: courte, naturelle, 100% russe minuscule, sans les éléments listés."
                ),
            })

        response = client_openai.chat.completions.create(
            model="gpt-4o",
            max_tokens=400,
            messages=messages,
        )
        reply = response.choices[0].message.content.strip()

        if _reply_is_clean(reply):
            break
        log("WRN",
            f"Reponse non conforme (tentative {attempt + 1}/4)",
            f'banned={_has_banned(reply)} emoji={_has_forbidden_emoji(reply)} '
            f'en={_has_english(reply)} | "{reply[:60]}"')

    # ── Post-processing : force minuscule sur la 1ère lettre ──
    reply = _fix_reply(reply)

    histories[user_id].append({"role": "assistant", "content": reply})
    # La sauvegarde est faite par l'appelant async via save_histories_async()

    tokens = response.usage.total_tokens
    log("OK ", f"Reponse generee ({tokens} tokens, tour {turn})", f"user={username}")
    return reply

# ─── Transcription vocaux entrants ───────────────────────────

async def transcribe_voice(message) -> str | None:
    """
    Télécharge un vocal Telegram et le transcrit via OpenAI Whisper.
    Retourne le texte transcrit, ou None si échec.
    """
    buf = io.BytesIO()
    await message.download_media(file=buf)
    buf.seek(0)
    buf.name = "voice.ogg"  # Whisper a besoin d'une extension reconnue
    try:
        result = client_openai.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
        )
        return result.text.strip() or None
    except Exception as e:
        log("ERR", "Transcription Whisper echouee", str(e))
        return None

# ─── Logique vocaux ───────────────────────────────────────────

# Mots-clés qui déclenchent un vocal en réponse directe
_VOICE_REQUEST_RE = re.compile(
    r"голосов[оаое]|запиши|скинь голос|пришли голос|голосом|войс|voice|запишешь",
    re.IGNORECASE,
)

def user_wants_voice(text: str) -> bool:
    """L'utilisateur demande explicitement un message vocal."""
    return bool(_VOICE_REQUEST_RE.search(text))

_TTS_ENABLED = bool(os.getenv("ZVUKOGRAM_TOKEN") and os.getenv("ZVUKOGRAM_EMAIL"))

def should_send_voice(user_id: str, incoming_text: str, turn: int) -> bool:
    """
    Décide si Eva envoie un vocal plutôt qu'un texte.
    Désactivé si ZVUKOGRAM_TOKEN/EMAIL manquants.
    """
    if not _TTS_ENABLED:
        return False
    if user_id in voice_sent_users:
        return False
    if user_wants_voice(incoming_text):
        return True
    if turn >= 4 and random.random() < VOICE_SPONTANEOUS_PROB:
        return True
    return False

async def send_voice(bot: TelegramClient, chat_id: int, user_id: str,
                     reply_text: str, name: str, username: str) -> tuple[bool, str]:
    """
    Sélectionne (catalogue ou TTS) et envoie un message vocal.
    Simule l'action "enregistrement audio" dans Telegram.
    Retourne (succès, transcript_utilisé).
    """
    agent = get_voice_agent()

    async with bot.action(chat_id, "record_audio"):
        audio_buf, transcript = await agent.generate(reply_text)

        # Durée réaliste d'enregistrement basée sur le transcript
        text_used = transcript or reply_text
        record_dur = max(2.5, len(text_used) * 0.055) + random.uniform(0.3, 1.5)
        log("VOX", f"Simulation enregistrement {record_dur:.1f}s", f"user={username}")
        await asyncio.sleep(record_dur)

    if audio_buf:
        await bot.send_file(chat_id, audio_buf, voice_note=True)
        voice_sent_users.add(user_id)
        log("OUT", f"VOCAL ENVOYE a {name} (@{username})", f'"{(transcript or reply_text)[:50]}"')
        return True, transcript or reply_text
    else:
        log("WRN", "Generation vocale echouee — fallback texte", f"user={username}")
        return False, reply_text

# ─── Délai humain ─────────────────────────────────────────────

async def human_delay(received_text: str, reply_text: str, user_id: str = ""):
    """
    Délai adaptatif :
    - Si l'utilisateur écrit vite (< 30s depuis son dernier message) → 30-55s
    - Sinon → 75-120s
    """
    words_in  = len(received_text.split())
    words_out = len(reply_text.split())

    read_time  = words_in  * 0.6
    think_time = random.uniform(2.0, 8.0)
    type_time  = words_out * 0.9
    total = read_time + think_time + type_time

    # Détection d'un utilisateur actif (écrit rapidement)
    now  = _time.time()
    last = last_message_time.get(user_id, 0)
    active = user_id and (now - last) < 30

    if active:
        total = min(max(total, 30.0), 55.0)
        log("DLY", f"Delai rapide {total:.1f}s (actif)", f"user={user_id}")
    else:
        total = min(max(total, 75.0), 120.0)
        if random.random() < 0.15:
            extra = random.uniform(10.0, 30.0)
            total += extra
            log("DLY", f"Pause longue {total:.0f}s (occupee)", f"user en attente")
        else:
            log("DLY", f"Delai {total:.1f}s",
                f"(lu:{read_time:.1f} reflechi:{think_time:.1f} frappe:{type_time:.1f})")

    await asyncio.sleep(total)

# ─── Bot Telegram ─────────────────────────────────────────────

bot = TelegramClient(SESSION, API_ID, API_HASH, catch_up=True)

# Verrou par utilisateur — une seule réponse en vol à la fois
user_locks: dict[str, bool] = {}
# Timestamp du dernier message entrant par utilisateur
last_message_time: dict[str, float] = {}
# Messages texte envoyés par le bot (pour ne pas les compter comme manuels)
# Clé : (chat_id, text) → timestamp d'envoi. TTL 60s pour éviter les fuites mémoire.
bot_sent_texts: dict[tuple, float] = {}

def _bot_sent_mark(chat_id: int, text: str):
    """Enregistre un message envoyé par le bot."""
    bot_sent_texts[(chat_id, text)] = _time.time()

def _bot_sent_check_and_remove(chat_id: int, text: str) -> bool:
    """Retourne True si ce message a été envoyé par le bot (et le supprime)."""
    now = _time.time()
    # Nettoyage TTL 60s
    stale = [k for k, t in bot_sent_texts.items() if now - t > 60]
    for k in stale:
        del bot_sent_texts[k]
    key = (chat_id, text)
    if key in bot_sent_texts:
        del bot_sent_texts[key]
        return True
    return False
# Heure de démarrage — ignorer les messages outgoing antérieurs
BOT_START_TIME = _time.time()

@bot.on(events.NewMessage(outgoing=True))
async def handle_outgoing(event):
    """Enregistre uniquement les messages écrits MANUELLEMENT par Eva dans l'historique."""
    if not event.is_private:
        return
    msg_time = event.message.date.timestamp() if event.message.date else 0
    if msg_time < BOT_START_TIME:
        return
    text = (event.message.message or "").strip()
    if not text:
        return
    chat    = await event.get_chat()
    user_id = str(chat.id)
    if _bot_sent_check_and_remove(chat.id, text):
        return
    if _BUTTON_RE.match(text):
        return
    # Ignore les messages envoyés à LeoMatchBot (automation)
    if int(user_id) == LEO_BOT_ID:
        return
    if user_id not in histories:
        histories[user_id] = []
    histories[user_id].append({"role": "assistant", "content": text})
    asyncio.create_task(save_histories_async())
    log("MAN", f"Message manuel enregistre ({user_id})", f'"{text}"')

@bot.on(events.NewMessage(incoming=True))
async def handle_message(event):
    if not event.is_private:
        return

    sender   = await event.get_sender()

    if sender.id == MY_ID or sender.id == 777000 or getattr(sender, "bot", False):
        return

    user_id  = str(sender.id)

    if user_id in BLOCKED_USERS:
        return

    username = getattr(sender, "username", None) or f"id:{sender.id}"
    name     = getattr(sender, "first_name", "") or username
    text     = (event.message.message or "").strip()

    # Gestion des messages sans texte
    if not text:
        if event.message.voice:
            log("VOX", f"Vocal recu de {name} (@{username}) — transcription...")
            transcribed = await transcribe_voice(event.message)
            if transcribed:
                text = transcribed
                log("VOX", f"Transcrit", f'"{text}"')
            else:
                text = "[голосовое сообщение]"
                log("WRN", "Transcription impossible — placeholder utilise")
        elif event.message.sticker:
            emoji = getattr(event.message.sticker, "alt", "") or "😶"
            text = f"[стикер {emoji}]"
            log("STK", f"Sticker recu de {name} (@{username})", f"emoji={emoji}")
        elif event.message.photo:
            text = "[фото]"
            log("IMG", f"Photo recue de {name} (@{username})")
        elif event.message.gif or event.message.video:
            text = "[видео/гиф]"
            log("VID", f"Video/GIF recu de {name} (@{username})")
        else:
            return

    log("IN ", f"RECU de {name} (@{username})", f'"{text}"')

    last_message_time[user_id] = _time.time()

    # Si une réponse est déjà en cours, accumule dans l'historique
    if user_locks.get(user_id):
        if user_id not in histories:
            histories[user_id] = []
        histories[user_id].append({"role": "user", "content": text})
        asyncio.create_task(save_histories_async())
        log("SKP", f"Message accumule (reponse en cours)", f"user={username}")
        return

    user_locks[user_id] = True
    try:
        while True:
            # ── Génération réponse texte ──
            reply = get_eva_response(user_id, username, text)
            turn  = len(histories[user_id]) // 2

            # ── Délai humain adaptatif ──
            await human_delay(text, reply, user_id)

            # ── Décision : vocal ou texte ? ──
            send_as_voice = should_send_voice(user_id, text, turn)

            if send_as_voice:
                log("VOX", f"Eva envoie un vocal", f"user={username} tour={turn}")
                sent, actual_transcript = await send_voice(
                    bot, event.chat_id, user_id, reply, name, username
                )
                if sent:
                    histories[user_id][-1] = {"role": "assistant", "content": actual_transcript}
                    await save_histories_async()
                else:
                    async with bot.action(event.chat_id, "typing"):
                        await asyncio.sleep(random.uniform(0.8, 2.0))
                    _bot_sent_mark(event.chat_id, reply)
                    await event.respond(reply)
                    await save_histories_async()
                    log("OUT", f"ENVOYE (fallback texte) a {name} (@{username})", f'"{reply}"')
            else:
                # ── Envoi texte classique ──
                async with bot.action(event.chat_id, "typing"):
                    await asyncio.sleep(random.uniform(0.8, 2.0))
                _bot_sent_mark(event.chat_id, reply)
                await event.respond(reply)
                await save_histories_async()
                log("OUT", f"ENVOYE a {name} (@{username})", f'"{reply}"')

            log("---", "-" * 55)

            # ── Check messages accumulés pendant le délai ──
            # On pop TOUS les messages user en attente pour les batcher en un seul appel GPT.
            # Avant : seul le dernier était traité → les autres restaient dans l'historique
            # sans réponse et causaient des doublons de réponse identique.
            hist = histories.get(user_id, [])
            pending = []
            while hist and hist[-1]["role"] == "user":
                pending.insert(0, hist.pop()["content"])

            if pending:
                # Combine tous les messages en attente en un seul texte
                text = "\n".join(pending)
                await save_histories_async()
                log("ACC",
                    f"{len(pending)} message(s) accumule(s) batch",
                    f"user={username} | \"{text[:60]}\"")
                continue
            break  # plus de messages en attente

    except Exception as e:
        log("ERR", f"Erreur", str(e))
    finally:
        user_locks[user_id] = False

# ─── LeoMatchBot automation ───────────────────────────────────

LEO_BOT_ID = 1234060895
LEO_LOG_FILE = "leo_conversation.jsonl"

_leo_lock = None          # asyncio.Lock() créé dans main()
_leo_start_time: float = 0.0  # défini dans main() après connexion — ignore les catch_up avant

_leo_likes_this_hour: list[float] = []
_LEO_RATE_LIMIT = 120
_leo_last_like_time: float = 0.0
_leo_last_action_time: float = 0.0  # timestamp de la dernière action envoyée à Leo
_leo_pause_until: float = 0.0  # chargé depuis leo_pause.json au démarrage

# Villes acceptées pour le like (Londres + alentours)
_LEO_ALLOWED_CITIES = {
    "лондон", "london", "richmond", "kingston", "wimbledon", "croydon",
    "bromley", "greenwich", "lewisham", "southwark", "lambeth", "wandsworth",
    "hammersmith", "fulham", "chelsea", "kensington", "islington", "hackney",
    "tower hamlets", "newham", "barking", "dagenham", "havering", "redbridge",
    "waltham", "haringey", "enfield", "barnet", "harrow", "brent", "ealing",
    "hounslow", "hillingdon", "hertfordshire", "surrey", "kent", "essex",
}

def _leo_log(direction: str, text: str, btns: list):
    """Log complet de toutes les interactions LeoMatchBot pour analyse."""
    entry = {"time": datetime.now().isoformat(), "direction": direction,
             "text": text[:300], "buttons": btns}
    try:
        with open(LEO_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _leo_is_london(text: str) -> bool:
    if not text:
        return True
    t = text.lower()
    for city in _LEO_ALLOWED_CITIES:
        if city in t:
            return True
    parts = text.split(",")
    if len(parts) >= 3:
        city_field = parts[2].strip().split()[0].lower() if parts[2].strip() else ""
        if city_field and city_field not in _LEO_ALLOWED_CITIES:
            return False
    return True

def _leo_can_like() -> bool:
    global _leo_last_like_time
    now = _time.time()
    _leo_likes_this_hour[:] = [t for t in _leo_likes_this_hour if now - t < 3600]
    if now - _leo_last_like_time < 28:
        return False
    return len(_leo_likes_this_hour) < _LEO_RATE_LIMIT

def _get_buttons(msg) -> list[str]:
    buttons = []
    kb = getattr(msg, "reply_markup", None)
    if not kb:
        return buttons
    rows = getattr(kb, "rows", [])
    for row in rows:
        for btn in row.buttons:
            txt = getattr(btn, "text", None)
            if txt:
                buttons.append(txt)
    return buttons

async def _gpt_leo_decide(text: str, btns: list[str]) -> str | None:
    """
    Demande à GPT-4o-mini quelle action prendre selon l'état de LeoMatchBot.
    Retourne le texte exact d'un bouton, ou None si aucune action.
    """
    if not btns:
        return None
    system = (
        "Tu automatises LeoMatchBot (appli rencontres russe). "
        "Objectif : parcourir les profils et liker ceux qui sont à Londres.\n"
        "Règles:\n"
        "- Boutons ❤️/👎/💤 → profil affiché → réponds '❤️'\n"
        "- Bouton '1 🚀' présent → menu principal → réponds '1 🚀'\n"
        "- Bouton 'Главное меню' présent → écran Premium ou erreur → réponds 'Главное меню'\n"
        "- 'Начинай общаться' ou match confirmé → réponds 'rien'\n"
        "- Bouton '1 👍' et quelqu'un a liké ton profil → réponds '1 👍'\n"
        "- Sinon → analyse et choisis le bouton le plus logique, ou 'rien'\n"
        "Réponds UNIQUEMENT avec le texte EXACT d'un bouton disponible, ou le mot 'rien'."
    )
    user_msg = f"Texte: {text[:200] or '(vide)'}\nBoutons disponibles: {btns}"
    try:
        resp = client_openai.chat.completions.create(
            model="gpt-4o-mini", max_tokens=20, temperature=0,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user_msg}]
        )
        action = resp.choices[0].message.content.strip()
        if action.lower() == "rien":
            return None
        for btn in btns:
            if action.strip() == btn.strip():
                return btn
        return None
    except Exception as e:
        log("LEO", f"GPT decide erreur: {e}")
        return None

@bot.on(events.NewMessage(chats=LEO_BOT_ID, incoming=True))
async def handle_leobot(event):
    """
    Handler LeoMatchBot — une seule action à la fois via _leo_lock.
    Ignore les messages antérieurs au démarrage (catch_up).
    """
    global _leo_last_like_time, _leo_last_action_time

    # ── Ignorer les messages catch_up (avant le démarrage du bot) ──
    msg_ts = event.message.date.timestamp() if event.message.date else 0
    if msg_ts < _leo_start_time:
        return

    text = (event.message.text or "").strip()
    btns = _get_buttons(event.message)
    _leo_log("in", text, btns)

    if not btns and not text:
        return

    # ── Pause temporaire ──
    if _leo_pause_until and _time.time() < _leo_pause_until:
        return

    # ── Une seule action à la fois ──
    if _leo_lock is None or _leo_lock.locked():
        log("LEO", "Lock actif — message Leo ignoré")
        return

    async with _leo_lock:
        await asyncio.sleep(random.uniform(2.0, 5.0))

        # ── Profil → like ou skip (logique hardcodée, plus fiable que GPT) ──
        if "❤️" in btns and ("👎" in btns or "💤" in btns):
            name = text.split(",")[0].strip()[:25] if text else "photo"
            if name.lower() in ("ева", "eva", "ieva"):
                await bot.send_message(LEO_BOT_ID, "1")
                _leo_log("out", "1", [])
                log("LEO", "Mon propre profil — skip")
            elif not _leo_is_london(text) and text:
                await bot.send_message(LEO_BOT_ID, "👎")
                _leo_log("out", "👎", [])
                log("LEO", f"Skip hors London", f"profil={name}")
            elif _leo_can_like():
                _leo_last_like_time = _time.time()
                _leo_likes_this_hour.append(_leo_last_like_time)
                await bot.send_message(LEO_BOT_ID, "❤️")
                _leo_log("out", "❤️", [])
                log("LEO", f"Like envoyé", f"profil={name}")
            else:
                log("LEO", "Rate limit — skip")
            _leo_last_action_time = _time.time()
            return

        # ── Tout le reste → GPT décide ──
        action = await _gpt_leo_decide(text, btns)
        if action:
            await bot.send_message(LEO_BOT_ID, action)
            _leo_log("out", action, [])
            _leo_last_action_time = _time.time()
            log("LEO", f"GPT → '{action}'", f"btns={btns[:2]}")
        else:
            log("LEO", f"Aucune action", f"text={text[:40]} btns={btns[:2]}")


async def recover_unanswered():
    """
    Au démarrage, répond aux messages restés sans réponse.
    Ne se fie pas à unread_count (Telegram peut auto-lire).
    Scanne toutes les conversations privées où le dernier message est entrant
    et où aucune réponse n'a été envoyée depuis ce message.
    """
    await asyncio.sleep(5.0)
    log("RCV", "Scan des conversations non-repondues...")
    recovered = 0
    cutoff = _time.time() - 48 * 3600  # fenêtre de 48h
    try:
        async for dialog in bot.iter_dialogs():
            if not dialog.is_user:
                continue
            peer = dialog.entity
            user_id = str(peer.id)
            if peer.id == MY_ID or user_id == str(LEO_BOT_ID):
                continue
            if peer.id == 777000:  # Telegram système (codes SMS, notifications)
                continue
            if getattr(peer, "bot", False):
                continue
            if user_id in BLOCKED_USERS:
                continue

            username = getattr(peer, "username", None) or f"id:{peer.id}"
            name     = getattr(peer, "first_name", "") or username

            last_msgs = await bot.get_messages(peer, limit=5)
            if not last_msgs:
                continue

            # Le message le plus récent doit être entrant
            if last_msgs[0].out:
                continue

            # Doit être dans la fenêtre de 6h
            msg_age = last_msgs[0].date.timestamp()
            if msg_age < cutoff:
                continue

            # Aucun message sortant ne doit être postérieur au dernier entrant
            last_in_ts = last_msgs[0].date.timestamp()
            already_replied = any(
                m.out and m.date.timestamp() > last_in_ts
                for m in last_msgs[1:]
            )
            if already_replied:
                continue

            # Éviter les doublons si une réponse sortante est très récente (< 120s)
            recent_out = any(
                m.out and (_time.time() - m.date.timestamp()) < 120
                for m in last_msgs[1:]
            )
            if recent_out:
                log("RCV", f"Skip doublon recent — {name} (@{username})")
                continue

            # Extraire le texte ou un placeholder pour les médias
            msg0 = last_msgs[0]
            if msg0.text:
                text = msg0.text.strip()
            elif getattr(msg0, "voice", None):
                transcribed = await transcribe_voice(msg0)
                text = transcribed if transcribed else "[голосовое сообщение]"
                log("RCV", f"Vocal transcrit pour {name}", f"{text[:60]}")
            elif getattr(msg0, "sticker", None):
                text = f"[стикер {getattr(msg0.sticker, 'alt', '') or ''}]".strip()
            elif getattr(msg0, "photo", None):
                text = "[фото]"
            elif getattr(msg0, "gif", None) or getattr(msg0, "video", None):
                text = "[видео/гиф]"
            else:
                text = ""
            if not text:
                continue

            log("RCV", f"Non-repondu : {name} (@{username})", f'"{text[:50]}"')
            await asyncio.sleep(random.uniform(5.0, 15.0) * (recovered + 1))
            if user_locks.get(user_id):
                continue
            user_locks[user_id] = True
            try:
                reply = get_eva_response(user_id, username, text)
                async with bot.action(peer.id, "typing"):
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                _bot_sent_mark(peer.id, reply)
                await bot.send_message(peer.id, reply)
                await save_histories_async()
                log("RCV", f"Reponse rattrapee -> {name} (@{username})", f'"{reply[:50]}"')
                recovered += 1
            except Exception as e:
                log("ERR", f"Erreur recover {username}", str(e))
                # Rollback : si get_eva_response a ajouté le message user avant de planter,
                # on le retire pour éviter une duplication au prochain redémarrage
                hist = histories.get(user_id, [])
                if hist and hist[-1]["role"] == "user" and hist[-1]["content"] == text:
                    histories[user_id].pop()
                    asyncio.create_task(save_histories_async())
            finally:
                user_locks[user_id] = False
    except Exception as e:
        log("ERR", f"Erreur recover_unanswered", str(e))
    log("RCV", f"Recuperation terminee — {recovered} message(s) rattrape(s)")

async def periodic_recover():
    """Scan toutes les 30 min pour rattraper les conversations sans réponse."""
    while True:
        await asyncio.sleep(1800)  # 30 min
        log("RCV", "Scan periodique des conversations non-repondues...")
        await recover_unanswered()

async def leo_start_browsing():
    """
    Watchdog : envoie "1 🚀" uniquement si aucune action n'a eu lieu depuis 60s
    et que le lock n'est pas actif. LeoMatchBot enchaîne les profils automatiquement
    après chaque ❤️/👎 — la boucle ne sert qu'à relancer si idle.
    """
    await asyncio.sleep(random.uniform(15.0, 25.0))  # délai initial
    while True:
        try:
            if _leo_lock is None:
                await asyncio.sleep(30.0)
                continue
            if _leo_pause_until and _time.time() < _leo_pause_until:
                remaining = (_leo_pause_until - _time.time()) / 3600
                log("LEO", f"Watchdog en pause — reprise dans {remaining:.1f}h")
                await asyncio.sleep(300.0)
                continue
            idle = _time.time() - _leo_last_action_time > 60
            if not _leo_lock.locked() and idle:
                await bot.send_message(LEO_BOT_ID, "1 🚀")
                log("LEO", "Watchdog — relance profil (idle 60s)")
            # sinon : action récente ou lock actif → LeoMatchBot va envoyer le suivant
        except Exception as e:
            log("LEO", f"Erreur watchdog: {e}")
        await asyncio.sleep(30.0)

# ─── Entrée ───────────────────────────────────────────────────

async def main():
    load_histories()

    log("BOT", "Demarrage Eva Bot...")
    log("TEL", f"Numero : {PHONE}")

    # Vérifie si l'agent vocal est configuré
    zg_token = os.getenv("ZVUKOGRAM_TOKEN", "")
    if not zg_token:
        log("WRN", "ZVUKOGRAM_TOKEN absent — vocaux desactives")
        log("WRN", "Crée un compte sur zvukogram.com et ajoute dans .env")
    else:
        log("VOX", f"Agent vocal actif — voix={os.getenv('ZVUKOGRAM_VOICE', 'Alena')} "
                   f"prob_spontanee={VOICE_SPONTANEOUS_PROB:.0%}")

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
        password = input("Mot de passe 2FA : ")
        await bot.sign_in(password=password)

    # Initialise le lock Leo et le timestamp de démarrage (après connexion — filtre catch_up)
    global _leo_lock, _leo_start_time, _leo_pause_until
    _leo_lock = asyncio.Lock()
    _leo_start_time = _time.time()
    # Charge la pause Leo depuis fichier (évite de modifier le code source)
    try:
        if os.path.exists("leo_pause.json"):
            with open("leo_pause.json", "r") as _pf:
                _leo_pause_until = float(json.load(_pf).get("until", 0))
            if _leo_pause_until > _time.time():
                from datetime import datetime as _dt
                log("LEO", f"Pause chargée — reprise à {_dt.fromtimestamp(_leo_pause_until).strftime('%H:%M')}")
            else:
                _leo_pause_until = 0.0
    except Exception as _e:
        log("WRN", f"leo_pause.json illisible: {_e}")

    me = await bot.get_me()
    log("OK ", f"Connectee : {me.first_name} (@{me.username})", f"id={me.id}")
    log("LEO", f"LeoMatchBot automation active — rate limit {_LEO_RATE_LIMIT} likes/h")
    log("RDY", "En attente de messages...\n")

    asyncio.create_task(leo_start_browsing())
    asyncio.create_task(recover_unanswered())
    asyncio.create_task(periodic_recover())

    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
