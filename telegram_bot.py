"""
Alina Bot — Telegram User Account (Telethon)
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
import base64
from datetime import datetime
from pathlib import Path
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import SendMessageRecordAudioAction
from telethon.tl.functions.account import UpdateStatusRequest
import anthropic
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

# Probabilité qu'Eva envoie un vocal spontanément (après tour 2)
VOICE_SPONTANEOUS_PROB = float(os.getenv("VOICE_PROB", "0.0"))
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "0") == "1"

# Vidéo d'Eva — envoyée sur demande, 1 fois max par conversation
_VIDEO_PATH = Path("ScreenRecording_04-14-2026 11-31-05_1.mp4")
_video_sent_users: set[str] = set()  # user_ids ayant déjà reçu la vidéo

client_anthropic = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
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

# Compteur de vocaux envoyés par conversation
voice_sent_users: dict[str, int] = {}

# Timestamp du dernier envoi par utilisateur — cooldown anti-burst
_last_sent_to: dict[str, float] = {}
SEND_COOLDOWN_MIN = 45.0  # secondes minimum entre deux envois au même user

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
        raw_vsu = data.pop(_VOICE_SENT_KEY, {})
        # Compat ancienne version (list) → dict compteur
        if isinstance(raw_vsu, list):
            voice_sent_users = {uid: 1 for uid in raw_vsu}
        else:
            voice_sent_users = raw_vsu
        histories = data
        # Inférer la langue de chaque conversation depuis les derniers messages user
        for uid, msgs in histories.items():
            if uid == _VOICE_SENT_KEY:
                continue
            user_msgs = [m["content"] for m in msgs if m.get("role") == "user"]
            for msg in reversed(user_msgs[-5:]):
                if _detect_lang(msg) == "fr":
                    _user_lang[uid] = "fr"
                    break
        total_vox = sum(voice_sent_users.values())
        log("MEM", f"Historique charge — {len(histories)} conversation(s), "
                   f"{total_vox} vocal(ux) deja envoye(s)")
        # Reconstruire les compteurs poniatno et smirk depuis l'historique
        for uid, msgs in histories.items():
            if uid == _VOICE_SENT_KEY:
                continue
            assistant_msgs = [m["content"] for m in msgs if m.get("role") == "assistant"]
            # poniatno : compter les occurrences dans toute la conv
            cnt = sum(1 for m in assistant_msgs if re.search(r"понятно", m, re.IGNORECASE))
            if cnt > 0:
                _poniatno_count[uid] = cnt
            # smirk : trouver le dernier tour où 😏 a été envoyé
            for turn_idx, msg in enumerate(assistant_msgs):
                if "\U0001f60f" in msg:
                    _smirk_last[uid] = turn_idx + 1  # tour 1-based approximatif
    else:
        log("MEM", "Aucun historique — demarrage a zero")

def save_histories():
    """Sauvegarde synchrone — utiliser save_histories_async() depuis un contexte async."""
    tmp = HISTORY_FILE + ".tmp"
    data = dict(histories)
    data[_VOICE_SENT_KEY] = dict(voice_sent_users)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.replace(tmp, HISTORY_FILE)  # écriture atomique — évite la corruption en cas de crash
    except OSError:
        # Windows peut refuser os.replace si le fichier est ouvert — fallback
        import shutil
        shutil.copy2(tmp, HISTORY_FILE)
        os.remove(tmp)

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
    r"|попробовать более"        # conseils sommeil/vie
    r"|чтобы расслабиться"       # conseil relaxation
    r"|попробуй "                # conseil direct
    r"|советую"
    r"|помогает при"
    r"|хорошо помогает"
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
    r"|не стесняйся спрашивать"
    r"|если есть вопросы"
    r"|если будут вопросы"
    r"|можешь спрашивать"
    r"|задавай вопросы"
    r"|обращайся"
    r"|стресс сказывается"       # diagnostic de bot
    r"|режим сна"                # conseil médical
    r"|перед сном"               # conseil relaxation
    r"|говорю только по-русски"  # réponse d'erreur de bot
    r"|стоит найти"              # conseil de vie
    r"|место для отдыха"         # conseil détente
    r"|с удовольствием"
    r"|то чувство когда"
    r"|как интересно"
    r"|это интересно"
    r"|это классно"
    r"|о, интересно"          # validation template AI
    r"|иногда такое"          # variante бывает
    r"|просто болтаем"        # justification de bot
    r"|ничё заготовленного"   # justification de bot
    r"|никакого заготовленного"
    r"|я тут чтобы"           # description de chatbot
    r"|я здесь чтобы"
    r"|давай лучше"           # redirect bot systématique
    r"|расскажи лучше"        # redirect bot systématique
    r"|попробуем ещё раз"     # service client
    r"|я предпочитаю"         # trop formel/corporate
    r"|вполне реальн"         # nier être un bot = tell de bot
    r"|я в порядке, спасибо"  # réponse service client
    r"|силой и глубиной"      # critique musicale encyclopédique
    r"|совсем другое настроение, но тоже"  # critique musicale encyclopédique
    r"|цепляет"               # critique style encyclopédique
    r"|это важно, чтобы"      # version banned phrase
    r"|буду ждать"            # trop patient/formel
    r"|наверняка ещё"         # réassurance de bot
    r"|наверняка что-то"
    r"|договорились пообщаться"  # phrase bizarre scriptée
    r"|для чата"                 # invention de contexte inexistant
    r"|просто для разговора"     # trop explicatif/justificatif
    r"|интересно у тебя всё складывается"  # approbation de bot
    r"|есть ещё вопросы или темы"  # service client chatbot
    r"|не переборщи"             # conseil de vie
    r"|главное.*не переб"        # "главное, не переборщить"
    r"|не играй с так"           # "не играй с таким"
    r"|будь осторожн"            # conseil sécurité
    r"|осторожн"                 # toutes formes d'осторожно
    # Formes masculines — Eva est une femme, jamais de genre masculin
    r"|\bя понял\b"   # doit être "поняла"
    r"|\bя устал\b"   # doit être "устала"
    r"|\bя был\b"     # doit être "была"
    r"|\bя пошёл\b"   # doit être "пошла"
    r"|\bя сказал\b"  # doit être "сказала"
    r"|\bя думал\b"   # doit être "думала"
    r"|\bя хотел\b"   # doit être "хотела"
    r"|\bя рад\b"     # doit être "рада"
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
    # Tournures trop « consultant IA » / service client
    r"|спасибо за"   # спасибо за понимание / спасибо за вопрос
    r"|надеюсь.*понравится"
    r"|рада что"     # рада что спросил = service client
    r"|это не просто"  # rhétorique IA
    r"|именно поэтому"
    r"|на самом деле" # sur-explication
    r"|в целом"       # bilan de bot
    r"|в общем-то"
    r"|это ощущение"  # description émotionnelle générique
    r"|я стараюсь"    # effort de bot
    r"|мне важно"     # valeur de bot
    r"|звучит заманчиво"   # enthousiasme de bot
    r"|звучит интересно"
    r"|звучит неплохо"
    r"|спасибо что рассказал"  # service client
    r"|спасибо, что"           # service client
    r"|это меняет"             # rhétorique IA
    # Aveux d'être une IA — absolu
    r"|спрошу у ии"
    r"|спрошу у искусственного"
    r"|у меня нет доступа"
    r"|как языковая модель"
    r"|как ии"
    # Questions type questionnaire de rencontres — jamais posées par une vraie personne
    r"|у тебя есть хобби"
    r"|что.*делаешь.*свободное время"
    r"|чем.*увлекаешься"
    r"|что тебе нравится делать"
    r"|есть ли у тебя"   # questionnaire formel
    r"|расскажи о себе"  # interview bot
    r"|что-нибудь интересное"
    ,
    re.IGNORECASE | re.UNICODE,
)

# Compte d'occurrences de "понятно" par conversation — limité à 1
_poniatno_count: dict[str, int] = {}

# Compte d'occurrences de 😏 par conversation — max 1 toutes les 5 réponses
_smirk_last: dict[str, int] = {}   # user_id → numéro de tour du dernier 😏

def _check_poniatno(user_id: str, text: str) -> bool:
    """Retourne True si "понятно" apparaît et le quota est dépassé (max 1 par conversation)."""
    if re.search(r"понятно", text, re.IGNORECASE):
        count = _poniatno_count.get(user_id, 0)
        if count >= 1:
            return True  # déjà utilisé une fois → refaire générer
        _poniatno_count[user_id] = count + 1
    return False

# Emojis explicitement interdits (выдают бота)
# Seuls 😏 et 😐 sont autorisés — tout le reste est interdit
_FORBIDDEN_EMOJIS = re.compile(r"[😊🙂😄🤗👍❤️😀😁😂🥰😍🤩😘😗😙😚😛😝😜🤪🤨🧐🤓😎🥳😒😞😔😟😕🙁☹️😣😖😫😩🥺😢😭😤😠😡🤬🤯😳🥵🥶😱😨😰😥😓🤔🤭🤫🤥😶😑😬🙄😯😦😧😮😲🥱😴🤤😪😵🤐🥴🤢🤮🤧😷🤒🤕🤑🤠😈👿👹👺💀☠️👻👽👾🤖💩😺😸😹😻😼😽🙀😿😾😉🤗]")

# Détection langue française
_FRENCH_RE = re.compile(
    r"\b(je|tu|il|elle|nous|vous|ils|elles|le|la|les|un|une|des|et|est|pas|mais|avec|pour|dans|sur|qui|que|quoi|comment|bonjour|salut|merci|oui|non|vas|veux|peux|vais|suis|fait|dit|rien|bien|très|aussi|déjà|encore|toujours|jamais|parfois|c'est|j'ai|t'as|qu'est|pourquoi)\b",
    re.IGNORECASE
)
_user_lang: dict[str, str] = {}

def _detect_lang(text: str) -> str:
    """Détecte si le message est en français. Sinon retourne 'ru' (défaut)."""
    if _FRENCH_RE.search(text):
        return "fr"
    return "ru"

# Mots anglais (hors noms propres acceptés)
_ENGLISH_WORD_RE = re.compile(r"\b[a-zA-Z]{3,}\b")
_ENGLISH_WHITELIST = re.compile(
    r"\b(london|eye|the|ok|wow|hmm|lol|ok)\b"
    r"|https?://\S+",
    re.IGNORECASE,
)

def _typing_delay(text: str) -> float:
    """Délai de saisie humain — variable et naturel.
    - Vitesse de frappe : 1.0–4.0 chars/sec (grande variation)
    - Pause de réflexion : 1–8s (on hésite toujours un peu avant d'envoyer)
    - Min 5s, max 25s
    """
    chars_per_sec = random.uniform(1.0, 4.0)
    base = len(text) / chars_per_sec
    think_pause = random.uniform(1.0, 8.0)
    return max(5.0, min(25.0, base + think_pause))

def _has_banned(text: str) -> bool:
    return bool(_BANNED_WORDS.search(text))

def _has_forbidden_emoji(text: str) -> bool:
    return bool(_FORBIDDEN_EMOJIS.search(text))

def _has_english(text: str) -> bool:
    """Détecte des mots anglais non autorisés dans un message."""
    cleaned = _ENGLISH_WHITELIST.sub("", text)
    return bool(_ENGLISH_WORD_RE.search(cleaned))

# Détecte une lettre latine collée à du cyrillique (ex: "vестфилд", "Wестмолл")
_MIXED_SCRIPT_RE = re.compile(r'[a-zA-Z][а-яёА-ЯЁ]|[а-яёА-ЯЁ][a-zA-Z]')

def _has_mixed_script(text: str) -> bool:
    """Détecte un mélange latin/cyrillique dans un même mot (bug GPT encodage)."""
    cleaned = _ENGLISH_WHITELIST.sub("", text)
    return bool(_MIXED_SCRIPT_RE.search(cleaned))

def _has_exclamation(text: str) -> bool:
    """'!' = enthousiasme de bot — jamais dans les messages d'Eva."""
    return "!" in text

def _fix_reply(text: str) -> str:
    """Post-processing non contournable : tirets, point final, minuscule, '!'."""
    # Règle absolue : jamais de tiret
    text = text.replace(" — ", ", ")
    text = text.replace("—", ", ")
    text = re.sub(r" - ", ", ", text)
    # Règle absolue : jamais de '!' — signal bot enthousiaste
    text = text.replace("!", "")
    # Règle absolue : jamais de point final (grille le bot)
    stripped = text.rstrip()
    if stripped.endswith(".") and not stripped.endswith("..."):
        text = stripped[:-1]
    # Minuscule sur la 1ère lettre
    if text and text[0].isupper():
        text = text[0].lower() + text[1:]
    return text.strip()

_DASH_RE = re.compile(r" — |—| - ")

def _split_message(text: str) -> list[str]:
    """
    Découpe un message long en 2 parties naturelles pour simuler plusieurs SMS.
    Ne découpe que si > 40 chars ET un point naturel existe dans le tiers central.
    """
    if len(text) <= 40:
        return [text]
    # Cherche un ". " ou une virgule dans le tiers central du message
    lo = len(text) // 4
    hi = (3 * len(text)) // 4
    # Priorité au ". "
    idx = text.find(". ", lo, hi)
    if idx != -1:
        a = text[:idx].strip()
        b = text[idx + 2:].strip()
        if a and b:
            return [_fix_reply(a), _fix_reply(b)]
    # Sinon ", " dans la moitié droite
    idx = text.find(", ", lo, hi)
    if idx != -1:
        a = text[:idx].strip()
        b = text[idx + 2:].strip()
        if a and b and len(b) > 8:
            return [_fix_reply(a), _fix_reply(b)]
    return [text]

def _has_dash(text: str) -> bool:
    """Règle absolue : jamais de tiret dans les messages."""
    return bool(_DASH_RE.search(text))

def _ends_with_dot(text: str) -> bool:
    """Un point final grille le bot — les vrais gens ne finissent pas leurs SMS par un point."""
    return bool(text) and text.rstrip().endswith(".")

def _is_duplicate_reply(user_id: str, text: str) -> bool:
    """True si Eva a déjà envoyé exactement ou quasi ce message dans cette convo."""
    if not user_id or not text:
        return False
    norm = text.strip().lower()
    prev = histories.get(user_id, [])
    for m in prev:
        if m["role"] == "assistant":
            if m["content"].strip().lower() == norm:
                return True
    return False

def _count_recent_questions(user_id: str, window: int = 4) -> int:
    """Compte combien de messages d'Eva dans les <window> derniers échanges se terminent par '?'."""
    msgs = [m["content"] for m in histories.get(user_id, []) if m["role"] == "assistant"]
    return sum(1 for m in msgs[-window:] if "?" in m)

def _max_tokens_for_input(user_msg: str) -> int:
    """Plafonne max_tokens selon la longueur du message entrant — miroir naturel."""
    n = len(user_msg.strip())
    if n <= 10:
        return 60    # "ок" / "норм" → réponse ultra-courte
    if n <= 40:
        return 120   # phrase courte → réponse courte
    if n <= 120:
        return 220   # message moyen → réponse modérée
    return 350       # long message → peut répondre plus longtemps

def _build_reminders(user_id: str, turn: int, user_msg: str) -> list[dict]:
    """Construit les injections système dynamiques pour ce tour."""
    injections = []

    # ── Rappel périodique des règles critiques (tours 5, 10, 15 …) ──
    if turn >= 5 and turn % 5 == 0:
        injections.append({
            "role": "user",
            "content": (
                "[SYSTÈME RAPPEL — règles absolues]\n"
                "1. Minuscules SEULEMENT — jamais de majuscule sauf noms propres\n"
                "2. Jamais '!' dans aucun message\n"
                "3. Jamais le schéma [réaction] + [info sur toi] + [question]\n"
                "4. Jamais de liste (virgule, virgule, virgule)\n"
                "5. Réponse proportionnelle à son message — s'il est court, toi aussi\n"
                "6. Tu es Алина, 21 ans, de Moscou, habites Camden, pas étudiante"
            ),
        })

    # ── Alerte taux de questions élevé ──
    recent_q = _count_recent_questions(user_id, window=4)
    if recent_q >= 1:
        injections.append({
            "role": "user",
            "content": (
                f"[SYSTÈME] Tu as posé {recent_q} question(s) dans tes 4 derniers messages. "
                "Pour CE message : NE TERMINE PAS par '?'. "
                "Réagis seulement, ne pose aucune question."
            ),
        })

    return injections

def _reply_is_clean(text: str, user_id: str = "", turn: int = 0, lang: str = "ru") -> bool:
    """True si le message passe tous les filtres qualité."""
    is_fr = (lang == "fr")
    return (
        not _has_banned(text)
        and not _has_forbidden_emoji(text)
        and (is_fr or not _has_english(text))
        and (is_fr or not _has_mixed_script(text))
        and not _has_dash(text)
        and not _has_exclamation(text)
        and not (user_id and _check_poniatno_peek(user_id, text))
        and not (user_id and turn and _check_smirk_peek(user_id, text, turn))
        and not (user_id and _is_duplicate_reply(user_id, text))
    )

def _check_poniatno_peek(user_id: str, text: str) -> bool:
    """Vérifie sans incrémenter si понятно dépasserait le quota (peek only)."""
    if re.search(r"понятно", text, re.IGNORECASE):
        return _poniatno_count.get(user_id, 0) >= 1
    return False

def _check_smirk_peek(user_id: str, text: str, turn: int) -> bool:
    """Bloque 😏 si utilise il y a moins de 5 tours."""
    if "😏" in text:
        last = _smirk_last.get(user_id, -99)
        return (turn - last) < 5
    return False

# ─── Génération réponse OpenAI ────────────────────────────────

def get_eva_response(user_id: str, username: str, user_message: str) -> str:
    if user_id not in histories:
        histories[user_id] = []
        log("NEW", f"Nouvelle conversation", f"user={username} id={user_id}")

    turn = len(histories[user_id]) // 2 + 1

    # Détection et mémorisation de la langue
    detected = _detect_lang(user_message)
    if detected == "fr":
        _user_lang[user_id] = "fr"
    elif user_id not in _user_lang:
        _user_lang[user_id] = "ru"
    lang = _user_lang[user_id]
    log("GPT", f"Generation reponse (tour {turn}) [{lang}]", f"user={username}")

    histories[user_id].append({"role": "user", "content": user_message})

    # Garde uniquement les 40 derniers messages (20 échanges) pour éviter overflow GPT
    MAX_HISTORY = 40
    if len(histories[user_id]) > MAX_HISTORY:
        histories[user_id] = histories[user_id][-MAX_HISTORY:]

    # ── Injection tour 1 : forcer "привет)" SEULEMENT si l'user a envoyé un salut ──
    _GREETING_RE = re.compile(
        r"^[\W]*(привет|хай|hi|hey|hello|yo|здравствуй|ку|ку-ку|хэй|прив|хеllo|sup)[\W]*$",
        re.IGNORECASE
    )
    _is_first_greeting = (turn == 1 and _GREETING_RE.match(user_message.strip()))
    _turn1_injection = {
        "role": "user",
        "content": (
            "[SYSTÈME] C'est ton PREMIER message dans cette conversation. "
            "Réponds UNIQUEMENT avec 'привет)' — rien d'autre, aucune question, "
            "aucun emoji, aucune salutation enthousiaste. Un seul mot."
        ),
    } if _is_first_greeting else None

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

    # ── Injection langue française ──
    _lang_injection = {
        "role": "user",
        "content": (
            "[SYSTÈME] L'utilisateur écrit en FRANÇAIS. "
            "Réponds UNIQUEMENT en français, même style court et naturel, "
            "même caractère qu'en russe. Minuscules, pas de '!', pas de listes."
        ),
    } if lang == "fr" else None

    # ── Calcul max_tokens selon longueur du message entrant ──
    _max_tok = _max_tokens_for_input(user_message)

    # ── Injections dynamiques (rappels périodiques + alerte questions) ──
    _dynamic_injections = _build_reminders(user_id, turn, user_message)

    reply = ""
    for attempt in range(4):
        messages = [{"role": "system", "content": SOUL}] + histories[user_id]

        # Injections fixes
        if _turn1_injection:
            messages.append(_turn1_injection)
        if _link_injection:
            messages.append(_link_injection)
        if _lang_injection:
            messages.append(_lang_injection)

        # Injections dynamiques (rappels règles + alerte questions)
        for inj in _dynamic_injections:
            messages.append(inj)

        if attempt > 0:
            issues = []
            if _has_banned(reply):
                issues.append(
                    "mots interdits: конечн/здоров/восхищ/заряжа/впечатля/вдохновля/"
                    "потряс/удивительн/замечательн/незабываем/звучит как план/"
                    "столько всего/каждый по-своему/на самом деле/в целом/это ощущение/"
                    "я стараюсь/мне важно/расскажи о себе/у тебя есть хобби"
                )
            if _has_forbidden_emoji(reply):
                issues.append("emoji STRICTEMENT interdit — seuls 😏 ou 😐 autorisés. Ne copie JAMAIS les emojis de l'utilisateur (surtout 😂 🙂 ❤️). Réponds SANS aucun emoji.")
            if lang == "ru" and _has_english(reply):
                issues.append("mot anglais (parle UNIQUEMENT en russe)")
            if _has_dash(reply):
                issues.append("tiret présent (— ou -): JAMAIS de tiret")
            # point final géré par _fix_reply — pas de retry nécessaire
            if _has_exclamation(reply):
                issues.append("'!' interdit: Eva n'est jamais enthousiaste de façon exagérée")
            if _check_poniatno_peek(user_id, reply):
                issues.append("'понятно' déjà utilisé: autre formulation")
            messages.append({
                "role": "user",
                "content": (
                    f"[SYSTÈME] Problème(s) dans ta réponse précédente: {'; '.join(issues)}. "
                    "Réécris-la: courte, naturelle, 100% russe minuscule, sans les éléments listés."
                ),
            })

        # Sépare system des messages user/assistant pour Anthropic
        anthropic_messages = [m for m in messages if m["role"] != "system"]
        response = client_anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=_max_tok,
            system=SOUL,
            messages=anthropic_messages,
        )
        reply = _fix_reply(response.content[0].text.strip())

        if _reply_is_clean(reply, user_id, turn, lang):
            break
        reasons = [
            k for k, v in [
                ("banned",   _has_banned(reply)),
                ("emoji",    _has_forbidden_emoji(reply)),
                ("en",       lang == "ru" and _has_english(reply)),
                ("mixed",    lang == "ru" and _has_mixed_script(reply)),
                ("dash",     _has_dash(reply)),

                ("excl",     _has_exclamation(reply)),
                ("poniatno", _check_poniatno_peek(user_id, reply)),
                ("smirk",   _check_smirk_peek(user_id, reply, turn)),
                ("dup",      _is_duplicate_reply(user_id, reply)),
            ] if v
        ]
        log("WRN",
            f"Reponse non conforme (tentative {attempt + 1}/4)",
            f'raisons={reasons} | "{reply[:60]}"')

    # ── Post-processing : force minuscule sur la 1ère lettre ──
    reply = _fix_reply(reply)

    # ── Filtre dur ")" — interdit sauf "привет)" au tour 1 exact ──
    is_first_msg = (turn == 1)
    if not (is_first_msg and reply.strip() == "привет)"):
        reply = reply.replace(")", "")

    # Comptabilise понятно après validation finale
    if re.search(r"понятно", reply, re.IGNORECASE):
        _poniatno_count[user_id] = _poniatno_count.get(user_id, 0) + 1
    if "😏" in reply:
        _smirk_last[user_id] = turn

    histories[user_id].append({"role": "assistant", "content": reply})
    # La sauvegarde est faite par l'appelant async via save_histories_async()

    tokens = response.usage.input_tokens + response.usage.output_tokens
    log("OK ", f"Reponse generee ({tokens} tokens, tour {turn})", f"user={username}")
    return reply

# ─── Transcription vocaux entrants ───────────────────────────

async def transcribe_voice(message) -> str | None:
    """
    Télécharge un vocal Telegram et le transcrit via OpenAI Whisper.
    Retourne le texte transcrit, ou None si échec.
    """
    # Anthropic n'a pas de STT — transcription vocale désactivée
    log("WRN", "Transcription vocale non disponible (Anthropic)", "")
    return None

# ─── Vision : analyse photos reçues ─────────────────────────

async def describe_photo(message) -> str:
    """
    Télécharge une photo Telegram et demande à GPT-4o de la décrire
    brièvement en russe, du point de vue d'Eva qui la reçoit.
    Retourne une description courte, ou '[фото]' en cas d'échec.
    """
    try:
        buf = io.BytesIO()
        await message.download_media(file=buf)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        resp = client_anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            "Décris cette photo en 1 courte phrase en russe minuscule, "
                            "comme si tu la recevais d'un inconnu sur une app de rencontres. "
                            "Juste ce que tu vois factuellement. Pas de jugement, pas d'analyse. "
                            "Exemple: 'парень на фоне машины' / 'закат на берегу' / 'селфи в зеркале'"
                        )
                    }
                ]
            }]
        )
        desc = resp.content[0].text.strip()
        log("IMG", f"Vision GPT: {desc[:60]}")
        return f"[фото: {desc}]"
    except Exception as e:
        log("ERR", f"Vision GPT echec: {e}")
        return "[фото]"


# ─── Logique vocaux ───────────────────────────────────────────

# Mots-clés qui déclenchent un vocal en réponse directe
_VOICE_REQUEST_RE = re.compile(
    r"голосов[оаое]|запиши|скинь голос|пришли голос|голосом|войс|voice|запишешь|кружок|кружочек",
    re.IGNORECASE,
)

def user_wants_voice(text: str) -> bool:
    """L'utilisateur demande explicitement un message vocal."""
    return bool(_VOICE_REQUEST_RE.search(text))

# Mots-clés vidéo/photo de soi (russe + français)
_VIDEO_REQUEST_RE = re.compile(
    # Russe — vidéo
    r"скинь\s*вид[её]|пришли\s*вид[её]|покажи\s*(себ[яе]|вид[её])|вид[её](о|ос|осик|ео)?\s*(от\s*тебя|свое|пришли|скинь)"
    r"|запишись|покажись|хочу\s*(тебя\s*)?(увидеть|видеть)|снимись"
    # Russe — photo
    r"|скинь\s*фот|пришли\s*фот|покажи\s*(себ[яе]|фот)|фотк|фоточк"
    # Français — photo/vidéo
    r"|montre.*(toi|photo|vid[eé]o)|envoie.*(photo|vid[eé]o|toi)"
    r"|photo\s*de\s*toi|vid[eé]o\s*de\s*toi|te\s*voir|voir\s*(toi|une\s*photo)"
    r"|t'as\s*(une\s*)?photo|montres?\s*toi"
    r"|renvoi[es]?\s*(la\s*)?(vid[eé]o|photo)?|envoie\s*(encore|de\s*nouveau)",
    re.IGNORECASE,
)

def user_wants_video(text: str) -> bool:
    """L'utilisateur demande une vidéo d'Eva."""
    return bool(_VIDEO_REQUEST_RE.search(text))

async def send_eva_video(bot: TelegramClient, chat_id: int, user_id: str) -> bool:
    """
    Envoie la vidéo d'Eva. Maximum 1 fois par conversation.
    Retourne True si envoyée, False sinon.
    """
    if not _VIDEO_PATH.exists():
        log("WRN", "Vidéo introuvable", str(_VIDEO_PATH))
        return False
    if user_id in _video_sent_users:
        return False
    try:
        await bot.send_file(chat_id, str(_VIDEO_PATH))
        _video_sent_users.add(user_id)
        # Inject dans l'historique pour que Claude sache qu'une vidéo vient d'être envoyée
        if user_id in histories:
            histories[user_id].append({"role": "assistant", "content": "[отправила видео]"})
        log("VID", f"Vidéo envoyée", f"user={user_id}")
        return True
    except Exception as e:
        log("ERR", f"Envoi vidéo échoué: {e}")
        return False

_TTS_ENABLED = bool(os.getenv("ZVUKOGRAM_TOKEN") and os.getenv("ZVUKOGRAM_EMAIL"))
# Catalogue local (73 MP3s) — disponible même sans token TTS
_CATALOG_ENABLED = Path("voice_catalog.json").exists()

# Vocaux déjà envoyés par user : user_id → set de filenames
_user_voice_history: dict[str, set[str]] = {}

def pick_voice_for_user(reply_text: str, user_text: str, user_id: str) -> "dict | None":
    """
    Cherche un vocal pertinent non encore envoyé à ce user.
    Retourne l'entrée catalogue ou None.
    """
    if not VOICE_ENABLED or not _CATALOG_ENABLED:
        return None
    from zvukogram_agent import pick_voice_for
    already_sent = _user_voice_history.get(user_id, set())
    entry = pick_voice_for(reply_text, user_text=user_text, fallback=False,
                           exclude=already_sent)
    return entry

async def send_voice(bot: TelegramClient, chat_id: int, user_id: str,
                     name: str, username: str,
                     entry: dict) -> tuple[bool, str]:
    """
    Envoie un vocal depuis une entrée catalogue.
    Simule l'action "enregistrement audio" dans Telegram.
    Retourne (succès, transcript_utilisé).
    """
    path = Path(entry["file"])
    if not path.exists():
        log("WRN", f"Fichier vocal introuvable: {entry['file']}")
        return False, ""

    transcript = entry.get("transcript", "")
    buf = __import__("io").BytesIO(path.read_bytes())
    buf.name = "voice.mp3"

    # Durée réaliste d'enregistrement basée sur le transcript
    record_dur = max(2.5, len(transcript) * 0.055) + random.uniform(0.3, 1.5)
    log("VOX", f"Simulation enregistrement {record_dur:.1f}s", f"user={username}")

    async with bot.action(chat_id, SendMessageRecordAudioAction()):
        await asyncio.sleep(record_dur)

    await bot.send_file(chat_id, buf, voice_note=True)

    # Marque comme envoyé pour ce user
    _user_voice_history.setdefault(user_id, set()).add(entry["filename"])

    log("OUT", f"VOCAL ENVOYE a {name} (@{username})", f'"{transcript[:60]}"')
    return True, transcript

# ─── Délai humain ─────────────────────────────────────────────

async def human_think_delay(received_text: str, user_id: str = "") -> None:
    """
    Simule le comportement humain complet avant de répondre :
    1. Reste OFFLINE pendant toute la réflexion (analyse du message)
    2. Passe ONLINE à un moment naturel (elle ouvre l'appli)
    3. Reste online le temps de "lire" le message (proportionnel à sa longueur)
    4. Retour → le handler lance le typing immédiatement après
    """
    chars    = len(received_text.strip())
    words_in = len(received_text.split())

    now  = _time.time()
    last = last_message_time.get(user_id, 0)
    gap  = (now - last) if last > 0 else 999.0
    rapid_exchange = gap < 20  # échange en cours (dernier message < 20s)

    # ── Délai de réflexion offline (proportionnel à longueur + contexte) ──
    if chars <= 20 and rapid_exchange:
        think = random.uniform(2.0, 8.0)
        label = "express"
    elif chars <= 20:
        think = random.uniform(10.0, 25.0)
        label = "courte"
    elif chars <= 80 and rapid_exchange:
        think = random.uniform(8.0, 22.0)
        label = "moyenne (actif)"
    elif chars <= 80:
        think = random.uniform(25.0, 50.0)
        label = "moyenne"
    else:
        think = random.uniform(40.0, 85.0)
        if random.random() < 0.15:
            think += random.uniform(15.0, 30.0)
        label = "longue"

    log("DLY", f"Reflexion {label} {think:.1f}s (offline)", f"user={user_id}")

    # Reste offline pendant toute la réflexion
    await asyncio.sleep(think)

    # ── Passe online (elle ouvre l'appli) ──
    await go_online()

    # ── Temps de lecture online — proportionnel à la longueur du message ──
    # Elle lit le message sans encore taper
    read_online = max(1.2, words_in * 0.45) + random.uniform(0.5, 2.5)
    log("DLY", f"Lecture en ligne {read_online:.1f}s", f"user={user_id}")
    await asyncio.sleep(read_online)

    # → Retour au handler qui lance le typing

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
            log("IMG", f"Photo recue de {name} (@{username})")
            text = await describe_photo(event.message)
        elif event.message.gif or event.message.video:
            text = "[видео/гиф]"
            log("VID", f"Video/GIF recu de {name} (@{username})")
        else:
            return

    # ── Ignorer les messages catch_up (avant le démarrage) ──
    msg_ts = event.message.date.timestamp() if event.message.date else 0
    if msg_ts < BOT_START_TIME - 5:
        return  # message antérieur au démarrage — traité par recover_unanswered si besoin

    log("IN ", f"RECU de {name} (@{username})", f'"{text}"')
    # Repasse immédiatement offline — Telethon se met online auto à chaque message reçu
    asyncio.create_task(go_offline())

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
            # ── Phase 1 : attente de réflexion (AVANT génération) ──
            # Pendant ce temps, les messages accumulés sont collectés
            await human_think_delay(text, user_id)

            # ── Collecte des messages arrivés pendant l'attente ──
            hist_now = histories.get(user_id, [])
            pending_during_wait = []
            while hist_now and hist_now[-1]["role"] == "user":
                pending_during_wait.insert(0, hist_now.pop()["content"])
            if pending_during_wait:
                log("ACC", f"{len(pending_during_wait)} msg(s) integres avant generation",
                    f"user={username}")
                text = text + "\n" + "\n".join(pending_during_wait)

            # ── Phase 2 : génération réponse (avec tout le contexte à jour) ──
            reply = get_eva_response(user_id, username, text)
            turn  = len(histories[user_id]) // 2

            # ── Cooldown anti-burst : délai minimum entre deux envois au même user ──
            since_last = _time.time() - _last_sent_to.get(user_id, 0)
            if since_last < SEND_COOLDOWN_MIN:
                extra_wait = SEND_COOLDOWN_MIN - since_last
                log("DLY", f"Cooldown anti-burst {extra_wait:.0f}s", f"user={username}")
                await asyncio.sleep(extra_wait)

            # ── Décision : vocal ou texte ? ──
            # Cherche un vocal pertinent non encore envoyé à ce user
            voice_entry = pick_voice_for_user(reply, text, user_id)

            if voice_entry:
                log("VOX", f"Eva envoie un vocal", f"user={username} tour={turn} | {voice_entry.get('transcript','')[:40]}")
                sent, actual_transcript = await send_voice(
                    bot, event.chat_id, user_id, name, username,
                    entry=voice_entry
                )
                if sent:
                    histories[user_id][-1] = {"role": "assistant", "content": actual_transcript}
                    await save_histories_async()
                else:
                    parts = _split_message(reply)
                    for i, part in enumerate(parts):
                        part_dur = _typing_delay(part)
                        async with bot.action(event.chat_id, "typing"):
                            await asyncio.sleep(part_dur)
                        _bot_sent_mark(event.chat_id, part)
                        await event.respond(part)
                        log("OUT", f"ENVOYE (fallback texte) a {name} (@{username})", f'"{part}"')
                        if i < len(parts) - 1:
                            hist_now = histories.get(user_id, [])
                            if hist_now and hist_now[-1]["role"] == "user":
                                log("SKP", f"Split interrompu — nouveaux msgs en attente", f"user={username}")
                                break
                            await asyncio.sleep(random.uniform(1.0, 2.5))
                    await save_histories_async()
            else:
                # ── Vidéo d'Eva si demandée ──
                if user_wants_video(text) and user_id not in _video_sent_users:
                    await send_eva_video(bot, event.chat_id, user_id)
                    await asyncio.sleep(random.uniform(1.5, 3.0))

                # ── Envoi texte classique — avec découpage multi-messages si long ──
                parts = _split_message(reply)
                for i, part in enumerate(parts):
                    part_dur = _typing_delay(part)
                    async with bot.action(event.chat_id, "typing"):
                        await asyncio.sleep(part_dur)
                    _bot_sent_mark(event.chat_id, part)
                    await event.respond(part)
                    log("OUT", f"ENVOYE a {name} (@{username})", f'"{part}"')
                    if i < len(parts) - 1:
                        hist_now = histories.get(user_id, [])
                        if hist_now and hist_now[-1]["role"] == "user":
                            log("SKP", f"Split interrompu — nouveaux msgs en attente", f"user={username}")
                            break
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                await save_histories_async()

            _last_sent_to[user_id] = _time.time()
            log("---", "-" * 55)

            # Repasse offline après un délai naturel (30s–3min)
            asyncio.create_task(_delayed_offline(random.uniform(30.0, 180.0)))

            # ── Check messages accumulés pendant l'ENVOI ──
            # Si d'autres messages sont arrivés pendant la génération/envoi, on les batche.
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

# Détection de boucle : liste des (texte_vu, timestamp) pour les checks watchdog
_leo_loop_history: list[tuple[str, float]] = []
_LEO_LOOP_WINDOW = 180.0   # 3 minutes
_LEO_LOOP_MAX    = 4       # 4 fois le même état = boucle

# Villes acceptées pour le like (Londres + alentours)
_LEO_ALLOWED_CITIES = {
    "лондон", "london", "richmond", "kingston", "wimbledon", "croydon",
    "bromley", "greenwich", "lewisham", "southwark", "lambeth", "wandsworth",
    "hammersmith", "fulham", "chelsea", "kensington", "islington", "hackney",
    "tower hamlets", "newham", "barking", "dagenham", "havering", "redbridge",
    "waltham", "haringey", "enfield", "barnet", "harrow", "brent", "ealing",
    "hounslow", "hillingdon", "hertfordshire", "surrey", "kent", "essex",
}

def _leo_detect_loop(text: str) -> bool:
    """
    Détecte une boucle infinie : même texte vu _LEO_LOOP_MAX fois en _LEO_LOOP_WINDOW s.
    Retourne True si boucle détectée (→ pause immédiate).
    """
    global _leo_loop_history, _leo_pause_until
    now = _time.time()
    # Purge anciens
    _leo_loop_history = [(t, ts) for t, ts in _leo_loop_history if now - ts < _LEO_LOOP_WINDOW]
    # Ajoute entrée courante
    key = text[:60].strip().lower()
    _leo_loop_history.append((key, now))
    # Compte occurrences du même texte
    count = sum(1 for t, _ in _leo_loop_history if t == key)
    if count >= _LEO_LOOP_MAX:
        from datetime import datetime, timedelta
        pause_end = now + 3600  # pause 1h
        _leo_pause_until = pause_end
        _leo_loop_history.clear()
        log("LEO", f"BOUCLE DETECTEE ({count}x) — pause 1h", f"text={text[:40]}")
        return True
    return False


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
    global _leo_last_like_time, _leo_last_action_time, _leo_pause_until

    # ── Ignorer les messages catch_up (avant le démarrage du bot) ──
    msg_ts = event.message.date.timestamp() if event.message.date else 0
    if msg_ts < _leo_start_time:
        return

    text = (event.message.text or "").strip()
    btns = _get_buttons(event.message)
    _leo_log("in", text, btns)

    if not btns and not text:
        return

    # ── Pause temporaire (relit le fichier à chaque appel) ──
    try:
        if os.path.exists("leo_pause.json"):
            with open("leo_pause.json", "r") as _pf:
                _disk = json.load(_pf).get("until", 0)
                if _disk > _leo_pause_until:
                    _leo_pause_until = _disk
    except Exception:
        pass
    if _leo_pause_until and _time.time() < _leo_pause_until:
        return

    # ── Détection de boucle ──
    if _leo_detect_loop(text):
        return

    # ── Une seule action à la fois ──
    if _leo_lock is None or _leo_lock.locked():
        log("LEO", "Lock actif — message Leo ignoré")
        return

    async with _leo_lock:
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # ── 1. Erreur "Нет такого варианта ответа" → retour au menu (max 1 fois / 60s) ──
        if "нет такого варианта" in text.lower():
            if _leo_last_action_time and _time.time() - _leo_last_action_time < 60:
                log("LEO", "Erreur loop — cooldown 60s actif, pause 10 min")
                _leo_pause_until = _time.time() + 600
                try:
                    with open("leo_pause.json", "w") as _pf:
                        json.dump({"until": _leo_pause_until}, _pf)
                except Exception:
                    pass
                return
            await bot.send_message(LEO_BOT_ID, "← Назад")
            _leo_log("out", "← Назад", [])
            _leo_last_action_time = _time.time()
            log("LEO", "Erreur → retour menu")
            return

        # ── 2. Limite journalière → pause jusqu'à minuit ──
        if "слишком много" in text.lower():
            from datetime import datetime, timedelta
            tomorrow = (datetime.now().replace(hour=0, minute=5, second=0) + timedelta(days=1))
            _leo_pause_until = tomorrow.timestamp()
            log("LEO", "Limite journaliere atteinte — pause jusqu'a minuit")
            return

        # ── 3. Menu principal (Смотреть анкеты présent) → "1" ──
        if any("смотреть анкеты" in b.lower() for b in btns) or "смотреть анкеты" in text.lower():
            await asyncio.sleep(random.uniform(1.0, 2.0))
            await bot.send_message(LEO_BOT_ID, "1")
            _leo_log("out", "1", [])
            _leo_last_action_time = _time.time()
            log("LEO", "Menu → Смотреть анкеты")
            return

        # ── 4. Profil affiché (bouton ❤️ inline OU texte "Nom, âge, Ville") → like ──
        _is_profile = (
            "❤️" in btns
            or re.match(r"^[^\n]{1,40},\s*\d{1,2}[,\s]", text)
        )
        if _is_profile:
            name = text.split(",")[0].strip()[:30] if text else "profil"
            if _leo_can_like():
                _leo_last_like_time = _time.time()
                _leo_likes_this_hour.append(_leo_last_like_time)
                await bot.send_message(LEO_BOT_ID, "❤️")
                _leo_log("out", "❤️", [])
                _leo_last_action_time = _time.time()
                log("LEO", f"Like envoyé", f"profil={name}")
            else:
                log("LEO", "Rate limit atteint — skip like")
            return

        # ── 4. Écran inconnu avec "← Назад" → reculer ──
        back_btns = [b for b in btns if "назад" in b.lower()]
        if back_btns:
            await bot.send_message(LEO_BOT_ID, back_btns[0])
            _leo_log("out", back_btns[0], [])
            _leo_last_action_time = _time.time()
            log("LEO", f"Ecran inconnu — retour", f"btns={btns[:3]}")
            return

        log("LEO", f"Aucune action connue", f"text={text[:40]} btns={btns[:3]}")


async def recover_unanswered(max_age_seconds: float = 48 * 3600):
    """
    Au démarrage, répond aux messages restés sans réponse.
    Ne se fie pas à unread_count (Telegram peut auto-lire).
    Scanne toutes les conversations privées où le dernier message est entrant
    et où aucune réponse n'a été envoyée depuis ce message.
    max_age_seconds: ignorer les messages plus vieux que ça (15 min au démarrage, 48h en périodique)
    """
    await asyncio.sleep(5.0)
    log("RCV", "Scan des conversations non-repondues...")
    recovered = 0
    cutoff = _time.time() - max_age_seconds
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
                text = await describe_photo(msg0)
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
                # Envoi vidéo si demandée
                if user_wants_video(text) and user_id not in _video_sent_users:
                    await send_eva_video(bot, peer.id, user_id)
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                async with bot.action(peer.id, "typing"):
                    await asyncio.sleep(_typing_delay(reply))
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

async def _leo_react_to_last():
    """
    Lit le dernier message de LeoMatchBot et réagit de manière cohérente.
    Appelé par le watchdog périodiquement.
    """
    global _leo_last_action_time, _leo_pause_until
    try:
        msgs = await bot.get_messages(LEO_BOT_ID, limit=3)
        if not msgs:
            return
        last = msgs[0]
        # Ignorer si le dernier message est sortant (on vient d'agir)
        if last.out:
            return
        # text ou caption (profils avec photo)
        text = (last.text or getattr(last, "message", "") or "").strip()
        if not text and hasattr(last, "photo") and last.photo:
            # Photo sans texte = profil → liker directement
            name = "profil photo"
            async with _leo_lock:
                if _leo_can_like():
                    _leo_last_like_time = _time.time()
                    _leo_likes_this_hour.append(_leo_last_like_time)
                    await bot.send_message(LEO_BOT_ID, "❤️")
                    _leo_last_action_time = _time.time()
                    log("LEO", f"Watchdog: like profil photo")
                else:
                    log("LEO", "Watchdog: rate limit — skip photo")
            return
        btns = _get_buttons(last)
        log("LEO", f"Watchdog check dernier msg", f"text={text[:50]} btns={btns[:3]}")

        # ── Détection boucle : même état répété → pause 1h ──
        if _leo_detect_loop(text):
            return

        async with _leo_lock:
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Erreur → ← Назад
            if "нет такого варианта" in text.lower():
                await bot.send_message(LEO_BOT_ID, "← Назад")
                _leo_last_action_time = _time.time()
                log("LEO", "Watchdog: erreur → retour menu")
                return

            # Menu → "1"
            if any("смотреть анкеты" in b.lower() for b in btns) or "смотреть анкеты" in text.lower():
                await bot.send_message(LEO_BOT_ID, "1")
                _leo_last_action_time = _time.time()
                log("LEO", "Watchdog: menu → Смотреть анкеты")
                return

            # Limite journalière Leo atteinte → pause jusqu'à minuit
            if "слишком много" in text.lower() or ("главное меню" in [b.lower() for b in btns] and "слишком" in text.lower()):
                from datetime import datetime, timedelta
                tomorrow = (datetime.now().replace(hour=0, minute=5, second=0) + timedelta(days=1))
                _leo_pause_until = tomorrow.timestamp()
                log("LEO", "Watchdog: limite journaliere atteinte — pause jusqu'a minuit")
                return

            # Profil → ❤️ si bouton inline OU texte sans mots-clés de menu
            _menu_keywords = ("смотреть анкеты", "моя анкета", "главное меню", "premium", "активируй", "я больше не хочу")
            _looks_like_menu = any(k in text.lower() for k in _menu_keywords)
            _is_profile = (
                "❤️" in btns
                or (text and not _looks_like_menu)
            )
            if _is_profile:
                name = text.split(",")[0].strip()[:30] if text else "profil"
                if _leo_can_like():
                    _leo_last_like_time = _time.time()
                    _leo_likes_this_hour.append(_leo_last_like_time)
                    await bot.send_message(LEO_BOT_ID, "❤️")
                    _leo_last_action_time = _time.time()
                    log("LEO", f"Watchdog: like envoyé", f"profil={name}")
                else:
                    log("LEO", "Watchdog: rate limit — skip")
                return

            # Écran inconnu avec "← Назад"
            back_btns = [b for b in btns if "назад" in b.lower()]
            if back_btns:
                await bot.send_message(LEO_BOT_ID, back_btns[0])
                _leo_last_action_time = _time.time()
                log("LEO", f"Watchdog: écran inconnu → retour")
                return

            # Inconnu → ne rien faire, on repassera au prochain tick
            log("LEO", f"Watchdog: état non reconnu — attente", f"text={text[:40]} btns={btns[:3]}")

    except Exception as e:
        log("LEO", f"Erreur _leo_react_to_last: {e}")


async def leo_start_browsing():
    """
    Watchdog actif : toutes les 20s, lit le dernier message Leo et réagit.
    Ne tire pas si le lock est actif ou si on est en pause.
    """
    global _leo_last_action_time
    await asyncio.sleep(random.uniform(10.0, 15.0))  # délai initial
    while True:
        try:
            if _leo_lock is None:
                await asyncio.sleep(15.0)
                continue
            if _leo_pause_until and _time.time() < _leo_pause_until:
                remaining = (_leo_pause_until - _time.time()) / 3600
                log("LEO", f"Watchdog en pause — reprise dans {remaining:.1f}h")
                await asyncio.sleep(300.0)
                continue
            if not _leo_lock.locked():
                await _leo_react_to_last()
        except Exception as e:
            log("LEO", f"Erreur watchdog: {e}")
        await asyncio.sleep(20.0)

# ─── Entrée ───────────────────────────────────────────────────

async def go_online():
    """Passe en ligne brièvement."""
    try:
        await bot(UpdateStatusRequest(offline=False))
    except Exception:
        pass

async def go_offline():
    """Passe hors ligne."""
    try:
        await bot(UpdateStatusRequest(offline=True))
    except Exception:
        pass

async def _delayed_offline(delay: float):
    """Repasse offline après un délai."""
    await asyncio.sleep(delay)
    await go_offline()

async def keep_offline():
    """Repasse offline toutes les 20s — contre le comportement auto-online de Telethon."""
    while True:
        try:
            await go_offline()
        except Exception:
            pass
        await asyncio.sleep(20)


async def main():
    load_histories()

    log("BOT", "Demarrage Alina Bot...")
    log("TEL", f"Numero : {PHONE}")

    # Vérifie si l'agent vocal est configuré
    zg_token = os.getenv("ZVUKOGRAM_TOKEN", "")
    if _CATALOG_ENABLED:
        from zvukogram_agent import _load_catalog
        cat = _load_catalog()
        log("VOX", f"Catalogue vocal actif — {len(cat)} fichiers locaux "
                   f"(prob_spontanee={VOICE_SPONTANEOUS_PROB:.0%})")
        if not zg_token:
            log("VOX", "TTS zvukogram.com desactive (token absent) — catalogue seul")
    elif not zg_token:
        log("WRN", "ZVUKOGRAM_TOKEN absent et aucun catalogue — vocaux desactives")
    if zg_token:
        log("VOX", f"TTS actif — voix={os.getenv('ZVUKOGRAM_VOICE', 'Alena')}")

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
    asyncio.create_task(recover_unanswered(max_age_seconds=900))  # 15 min au démarrage — évite de répondre à des convos stales
    asyncio.create_task(periodic_recover())
    asyncio.create_task(keep_offline())

    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
