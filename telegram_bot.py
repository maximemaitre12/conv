"""
Katia Bot — Telegram User Account (Telethon)
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
import platform as _platform
if _platform.system() == "Windows":
    import msvcrt as _filelock
else:
    import fcntl as _filelock
import base64
import traceback
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import SendMessageRecordAudioAction, UpdateDraftMessage
from telethon.tl.functions.account import UpdateStatusRequest
import anthropic
from dotenv import load_dotenv
from soul import SOUL
from zvukogram_agent import get_voice_agent
from webhook_server import start_in_background as _start_webhook, has_clicked, has_purchased, make_short_url

# Force UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

# ─── Single-instance lock (empêche deux bots simultanés → crash SQLite) ───────
_LOCK_FILE = "eva_bot.lock"
_lock_fh = None

def _acquire_instance_lock():
    if os.getenv("EVA_TEST_MODE"):
        return  # mode test direct — pas de lock
    global _lock_fh
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        if _platform.system() == "Windows":
            _filelock.locking(_lock_fh.fileno(), _filelock.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_fh.close()
        print("[FATAL] Une autre instance du bot tourne déjà. Arrêt.", flush=True)
        sys.exit(1)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()
    atexit.register(_release_instance_lock)

def _release_instance_lock():
    if os.getenv("EVA_TEST_MODE"):
        return
    global _lock_fh
    _save_last_seen()
    if _lock_fh:
        try:
            _lock_fh.seek(0)
            if _platform.system() == "Windows":
                _filelock.locking(_lock_fh.fileno(), _filelock.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_UN)
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
_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "")
SESSION = StringSession(_SESSION_STRING) if _SESSION_STRING else "eva_session"

MY_ID = 0  # ID du compte Katia — sera mis à jour au premier démarrage

# Probabilité qu'Eva envoie un vocal spontanément (après tour 2)
VOICE_SPONTANEOUS_PROB = float(os.getenv("VOICE_PROB", "0.0"))
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "0") == "1"
LEO_ENABLED   = os.getenv("LEO_ENABLED", "1") == "1"

# Vidéo d'Eva — envoyée sur demande, 1 fois max par conversation (video note ronde)
_VIDEO_PATH = Path(__file__).parent / "videonote_katia.mp4"
_video_sent_users: set[str] = set()  # user_ids ayant déjà reçu la vidéo

# ── Résumés de contexte par conversation ──────────────────────
# maps user_id (str) → résumé compact des faits clés (prénom, ville, sujets, LE status)
_user_context: dict[str, str] = {}
_USER_CONTEXT_KEY = "__user_contexts__"

client_anthropic = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    http_client=httpx.Client(verify=False),
)

histories    = {}
HISTORY_FILE = "histories.json"

# Utilisateurs bloqués — Eva ne leur répond pas
BLOCKED_USERS = {"623346108", "563188623", "5653565646", "7497658207", "784708739", "1298399032", "7615670279", "696518838"}  # СОВЕТСКИЙ (@mkaafromspai), Vadim (@Sakaliuk01), Дима (@dddimacta), basbikb (@basbikb), Vadim (@VadimTP), Герман (@MemoryLe4k), ArsenChik (@ssskam009), danchizis (@danchizis)

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

# Re-engagement après lien London Eye — users déjà planifiés (évite double-schedule)
_link_followup_scheduled: set[str] = set()

# ── Cold re-engagement ──
_COLD_FILE = Path("cold_reengagement.json")
_COLD_MSGS = {
    "ru": ["ты куда пропал?", "всё нормально?", "ты там?", "пропал куда-то"],
    "fr": ["t'as disparu ?", "tout va bien ?", "t'es là ?", "tu as disparu"],
    "en": ["you disappeared ?", "everything ok ?", "you there ?"],
}

# ── A/B test première ouverture ──
_AB_FILE = Path("ab_test.json")

# Engagement scoring — intervalles de réponse par user (pour adapter le moment de conversion)
_user_response_gaps: dict[str, list[float]] = {}

# Cache des descriptions de photo de profil (user_id → description)
_user_profile_desc: dict[str, str] = {}

# Description des propres photos de profil d'Eva (mise à jour au démarrage)
_own_profile_desc: str = ""

# ─── Tunnel Cloudflare (webhook tracking) ─────────────────────

_NGROK_EXE    = Path(__file__).parent / "ngrok.exe"
_NGROK_DOMAIN = os.getenv("NGROK_DOMAIN", "")   # ex: settling-macaw-purely.ngrok-free.app
_tunnel_url: str = ""

def _start_tunnel():
    """Lance ngrok en arrière-plan (sans fenêtre) et extrait l'URL publique."""
    global _tunnel_url
    if not _NGROK_EXE.exists():
        log("TRK", "ngrok.exe introuvable — tunnel désactivé")
        return
    try:
        cmd = [str(_NGROK_EXE), "http", "5055", "--log=stdout", "--log-format=json"]
        if _NGROK_DOMAIN:
            cmd += [f"--domain={_NGROK_DOMAIN}"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        def _read_url():
            global _tunnel_url
            for raw in proc.stdout:
                try:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    data = json.loads(line)
                    url = data.get("url", "")
                    if url.startswith("https://"):
                        _tunnel_url = url
                        log("TRK", f"Tunnel ngrok actif", _tunnel_url)
                except Exception:
                    pass
        threading.Thread(target=_read_url, daemon=True, name="ngrok-reader").start()
    except Exception as e:
        log("TRK", f"Tunnel ngrok échoué : {e}")

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
        raw_ctx = data.pop(_USER_CONTEXT_KEY, {})
        _user_context.update(raw_ctx)
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
                detected = _detect_lang(msg)
                if detected in ("fr", "en"):
                    _user_lang[uid] = detected
                    break
        total_vox = sum(voice_sent_users.values())
        log("MEM", f"Historique charge — {len(histories)} conversation(s), "
                   f"{total_vox} vocal(ux) deja envoye(s)")
        # Reconstruire _video_sent_users depuis l'historique (marqueur [отправила видео])
        for uid, msgs in histories.items():
            if uid == _VOICE_SENT_KEY:
                continue
            for m in msgs:
                if m.get("role") == "assistant" and "[отправила видео]" in m.get("content", ""):
                    _video_sent_users.add(uid)
                    break
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
    # Recharge les pauses manuelles actives (survit au restart)
    _load_manual_pause()
    _load_closer_lock()

_save_lock = threading.Lock()

def save_histories():
    """Sauvegarde synchrone — utiliser save_histories_async() depuis un contexte async."""
    with _save_lock:
        tmp = HISTORY_FILE + ".tmp"
        data = dict(histories)
        data[_VOICE_SENT_KEY] = dict(voice_sent_users)
        data[_USER_CONTEXT_KEY] = dict(_user_context)
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
    r"|\bзвучит\b"         # toutes formes : "звучит" seul ou en phrase
    r"|звучит заманчиво"
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

# Whitelist stricte : seuls 😏 (U+1F60F) et 😐 (U+1F610) sont autorisés.
# Tout caractère emoji Unicode hors de cette whitelist est interdit.
# Plages Unicode emoji : Emoticons (1F600-1F64F), Misc symbols (1F300-1F5FF),
# Transport (1F680-1F6FF), Supplemental (1F900-1F9FF), Symbols (2600-26FF), Dingbats (2700-27BF)
_EMOJI_WHITELIST = {"\U0001f60f", "\U0001f610"}  # 😏 😐
_FORBIDDEN_EMOJIS_RE = re.compile(
    r"[\U0001F600-\U0001F64F"   # Emoticons
    r"\U0001F300-\U0001F5FF"    # Misc symbols & pictographs
    r"\U0001F680-\U0001F6FF"    # Transport & map
    r"\U0001F900-\U0001F9FF"    # Supplemental symbols
    r"\U0001FA00-\U0001FA6F"    # Chess, etc.
    r"\U0001FA70-\U0001FAFF"    # Symbols extended
    r"\u2600-\u26FF"            # Misc symbols (☀️⭐etc.)
    r"\u2700-\u27BF"            # Dingbats (✨✅etc.)
    r"\uFE00-\uFE0F"            # Variation selectors (️)
    r"\u200D"                   # Zero-width joiner
    r"\u20E3"                   # Combining enclosing keycap
    r"]"
)

def _has_forbidden_emoji_char(c: str) -> bool:
    return bool(_FORBIDDEN_EMOJIS_RE.match(c)) and c not in _EMOJI_WHITELIST

# Compat alias pour l'ancien nom
_FORBIDDEN_EMOJIS = _FORBIDDEN_EMOJIS_RE

# Détection langue française
_FRENCH_RE = re.compile(
    r"\b(je|tu|il|elle|nous|vous|ils|elles|le|la|les|un|une|des|et|est|pas|mais|avec|pour|dans|sur|qui|que|quoi|comment|bonjour|salut|merci|oui|non|vas|veux|peux|vais|suis|fait|dit|rien|bien|très|aussi|déjà|encore|toujours|jamais|parfois|c'est|j'ai|t'as|qu'est|pourquoi)\b",
    re.IGNORECASE
)
# Détection langue anglaise
_ENGLISH_RE = re.compile(
    r"\b(i|you|he|she|we|they|the|a|an|is|are|was|were|do|does|did|have|has|had|will|would|can|could|should|what|where|when|why|how|who|your|my|it|that|this|with|for|on|at|to|of|and|or|but|not|yeah|hey|hi|hello|ok|okay|bro|man|lol|omg|tbh|ngl|btw|ur|u|r|gonna|wanna|cause|cuz|bc|tho|though|if|then|so|just|like|get|got|know|think|want|need|make|come|go|see|look|tell|ask|say|send|show|give)\b",
    re.IGNORECASE
)
_user_lang: dict[str, str] = {}

async def _fetch_tg_history(user_id_int: int, current_text: str, limit: int = 30) -> list | None:
    """Lit l'historique réel depuis Telegram — source de vérité (messages supprimés absents, messages manuels opérateur inclus)."""
    try:
        messages = await bot.get_messages(user_id_int, limit=limit)
        result = []
        for m in reversed(list(messages)):
            if not getattr(m, "message", None):
                continue
            role = "assistant" if m.out else "user"
            result.append({"role": role, "content": m.message.strip()})
        # Le message entrant actuel est déjà sur TG — on l'exclut car get_eva_response l'ajoute lui-même
        if result and result[-1]["role"] == "user" and result[-1]["content"] == current_text.strip():
            result = result[:-1]
        return result if result else None
    except Exception as e:
        log("WRN", f"_fetch_tg_history erreur: {e}")
        return None

def _detect_lang(text: str) -> str:
    """Détecte la langue du message : 'fr', 'en', ou 'ru' (défaut)."""
    if _FRENCH_RE.search(text):
        return "fr"
    # Anglais : au moins 2 mots anglais distincts pour éviter les faux positifs
    en_matches = _ENGLISH_RE.findall(text)
    if len(en_matches) >= 2:
        return "en"
    return "ru"

# Mots anglais (hors noms propres acceptés)
_ENGLISH_WORD_RE = re.compile(r"\b[a-zA-Z]{3,}\b")
_ENGLISH_WHITELIST = re.compile(
    r"\b(london|eye|the|ok|wow|hmm|lol|"
    r"canning|town|canary|wharf|camden|shoreditch|hackney|brixton|"
    r"chelsea|notting|hill|soho|greenwich|westminster|victoria|"
    r"waterloo|paddington|kings|cross|bridge|street|road|lane|"
    r"central|east|west|north|south|city|bank|oval|angel|"
    r"stepney|poplar|isle|dogs|stratford|bow|bethnal|green|"
    r"piccadilly|oxford|circus|covent|garden|holborn|farringdon|"
    r"barbican|aldgate|whitechapel|shadwell|limehouse|mile|end|"
    r"wapping|bermondsey|borough|elephant|castle|kennington|"
    r"vauxhall|pimlico|sloane|knightsbridge|kensington|earl|"
    r"hammersmith|fulham|putney|wimbledon|balham|clapham|"
    r"stockwell|brixton|herne|lewisham|new|cross|deptford|"
    r"canary|canada|water|surrey|quays|rotherhithe|"
    r"dalston|stoke|newington|finsbury|park|highbury|islington|"
    r"highgate|kentish|hampstead|swiss|cottage|finchley|"
    r"tottenham|seven|sisters|manor|house|"
    r"eye|westminster|ticket|tickets|capsule|pod|"
    r"bond|regent|mayfair|marylebone|baker|carnaby|"
    r"strand|fleet|embankment|southbank|tate|modern|"
    r"tower|trafalgar|square|hyde|buckingham|marble|"
    r"arch|portobello|soho|covent|"
    r"wembley|ealing|acton|chiswick|richmond|twickenham|"
    r"kingston|surbiton|croydon|tooting|merton|sutton|"
    r"ilford|barking|dagenham|romford|hornchurch|"
    r"enfield|walthamstow|leyton|leytonstone|forest|"
    r"gate|grove|peckham|catford|bromley|woolwich|"
    r"eltham|sidcup|bexley|erith|abbey|wood|"
    r"harrow|stanmore|edgware|barnet|whetstone|"
    r"golders|hendon|mill|hill|brent|willesden|"
    r"kilburn|queens|park|shepherds|bush|"
    r"acton|gunnersbury|kew|gardens|"
    r"whitechapel|brick|lane|spitalfields|"
    r"fast|track|standard|ticket|online|booking|book|"
    r"queue|skip|pass|entry|adult|child|family|group)\b"
    r"|https?://\S+",
    re.IGNORECASE,
)

def _engagement_level(user_id: str) -> str:
    """
    Calcule le niveau d'engagement de l'user : 'hot', 'warm', ou 'cold'.
    Basé sur les 3-5 derniers intervalles de réponse (temps entre dernier envoi bot et réponse user).
    hot  : répond en < 45s en moyenne pondérée  → conversion possible dès tour 6
    warm : répond en < 5 min                    → conversion à tour 10 (défaut)
    cold : répond lentement ou peu              → pas d'urgence forcée
    Requiert au moins 3 gaps pour être classé "hot" (évite les faux positifs sur 1-2 msgs rapides).
    Le dernier gap compte double (réflète l'énergie courante).
    """
    gaps = _user_response_gaps.get(user_id, [])
    if len(gaps) < 3:   # Fix 10 : était < 2, trop peu de données
        return "warm"
    recent = gaps[-4:]
    # Fix 11 : pondération — dernier gap compte double (énergie récente > historique)
    weights = [1.0] * len(recent)
    weights[-1] = 2.0
    weighted_avg = sum(g * w for g, w in zip(recent, weights)) / sum(weights)
    if weighted_avg < 45:
        return "hot"
    if weighted_avg < 300:
        return "warm"
    return "cold"


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
    """True si le texte contient un emoji hors-whitelist (tout sauf 😏 et 😐)."""
    for ch in text:
        if _has_forbidden_emoji_char(ch):
            return True
    return False

def _strip_forbidden_emojis(text: str) -> str:
    """Supprime tous les emojis interdits du texte, conserve 😏 et 😐."""
    return "".join(ch for ch in text if not _has_forbidden_emoji_char(ch)).strip()

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
    """Post-processing non contournable : tirets, point final, minuscule, '!'.
    Note : ||| est préservé ici — il est consommé par _split_message, jamais envoyé brut."""
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
    # Remplacement mots français peu attrayants
    text = re.sub(r'\bouais\b', 'oui', text, flags=re.IGNORECASE)
    text = re.sub(r'\bwesh\b', '', text, flags=re.IGNORECASE)
    return text.strip()

_DASH_RE = re.compile(r" — |—| - ")

def _split_message(text: str) -> list[str]:
    """
    Découpe un message en plusieurs parties pour simuler plusieurs SMS.
    Priorité :
      1. ||| — token explicite du LLM (100% fiable, jamais en texte naturel)
      2. Double saut de ligne (\n\n) — marqueur legacy
      3. ". " dans le tiers central (long textes seulement)
      4. ", " dans le tiers central (long textes seulement)
    """
    # 1. ||| — split explicite demandé par le LLM, toujours prioritaire
    if "|||" in text:
        parts = [p.strip() for p in text.split("|||") if p.strip()]
        if len(parts) >= 2:
            p1 = parts[0]
            p2 = " ".join(parts[1:])  # fusionne les éventuels |||+ supplémentaires dans le 2ème
            return [_fix_reply(p1), _fix_reply(p2)]
        # Aucune ou une seule partie non vide → on nettoie et continue avec les heuristiques
        text = text.replace("|||", " ").strip()

    # 2. Double newline — marqueur legacy, toujours découper
    if "\n\n" in text:
        parts = [p.strip() for p in text.split("\n\n") if p.strip()]
        if len(parts) >= 2:
            return [_fix_reply(p) for p in parts]

    # 3. Simple newline — LLM utilise parfois \n au lieu de |||
    if "\n" in text:
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) >= 2:
            p1 = parts[0]
            p2 = " ".join(parts[1:])
            return [_fix_reply(p1), _fix_reply(p2)]

    if len(text) <= 80:
        return [text]

    # 3. ". " dans le tiers central — seulement si les deux parties sont substantielles
    lo = len(text) // 4
    hi = (3 * len(text)) // 4
    idx = text.find(". ", lo, hi)
    if idx != -1:
        a = text[:idx].strip()
        b = text[idx + 2:].strip()
        if a and b and len(a) >= 20 and len(b) >= 20:
            return [_fix_reply(a), _fix_reply(b)]

    # 4. ", " dans le tiers central
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

# ─── Détection état de conversion ────────────────────────────

# Fix 3 : objections avec contexte resserré (fenêtre hist[-4:] appliquée dans _build_conversion_state)
# Fix 3 : patterns affinés pour éviter les faux positifs hors-sujet
_OBJ_EXPENSIVE_RE = re.compile(
    # Doit concerner le prix directement, pas "дорогой друг" etc.
    r"\bдорого\b|\bдорогой\b(?!\s+друг|\s+мой|\s+мне)|\bдорогая\b(?!\s+[А-ЯЁа-яё]{3})"
    r"|c'est\s+cher|trop\s+cher|\bexpensive\b|too\s+much\s+money|costs?\s+too\s+much",
    re.IGNORECASE,
)
_OBJ_NOT_FREE_RE = re.compile(
    # Fix 3 : "не могу поверить", "can't stop thinking" → exclus avec contexte
    # Exige "не могу" SEUL (fin de phrase/msg) ou suivi de verbe de mouvement/présence
    r"занят(?:а)?\s*$|не\s+могу\s*$|не\s+свободен|не\s+смогу|не\s+получится"
    r"|занят(?:а)?\s+(?:завтра|сегодня|на этой|на следующей)"
    r"|pas\s+lib(?:re)?|pas\s+dispo(?:nible)?|je\s+(?:ne\s+)?peux\s+pas\s*$"
    r"|i\s+can't\s+(?:make|come|go)|i'm\s+busy\s+(?:tomorrow|that|this)|not\s+free\s+(?:tomorrow|that|this)",
    re.IGNORECASE | re.MULTILINE,
)
_OBJ_VAGUE_RE = re.compile(
    # Fix 3 : "может быть" seul ou en début/fin — pas "может быть это странно"
    r"^может\s+быть$|^посмотрим$|^возможно$|не\s+знаю\s+ещ[её]"
    r"|^peut-être$|^on\s+verra$|^je\s+sais\s+pas$|^maybe$|^we.ll\s+see$|^not\s+sure$",
    re.IGNORECASE | re.MULTILINE,
)
_OBJ_NO_RE = re.compile(
    # Fix 3 : "не хочу тебя обидеть" → exclu. Exige contexte de refus direct.
    r"не\s+хочу\s+(?:идти|туда|на\s+london|это|там|ехать)|не\s+пойду|не\s+интересует"
    r"|pas\s+envie\s+(?:d'y\s+aller|du\s+tout|vraiment)|je\s+(?:ne\s+)?veux\s+pas\s+(?:y\s+)?aller"
    r"|don.t\s+want\s+to\s+(?:go|come|do)|not\s+interested\s+in",
    re.IGNORECASE,
)
# Fix 4 : détection résolution d'objection (user accepte après avoir objecté)
_OBJ_RESOLVED_RE = re.compile(
    r"^ладно\b|^окей\b|^ок\b|^давай\b|договорились|^идёт$|хорошо\s+давай|ладно\s+давай"
    r"|^d.accord$|^ok$|^let.s\s+(?:go|do\s+it)$|^deal$|^parfait$|^sure$|sounds\s+good",
    re.IGNORECASE | re.MULTILINE,
)
# Fix 2 : date agreed — inclut jours seuls (sans préposition) + confirmations génériques
_DATE_AGREED_RE = re.compile(
    # Avec préposition RU
    r"\b(?:в\s+субботу|в\s+воскресенье|в\s+пятницу|в\s+четверг|в\s+среду"
    r"|в\s+понедельник|во\s+вторник|в\s+выходные|на\s+выходных|завтра|послезавтра)\b"
    # Jours seuls RU (Fix 2)
    r"|\b(?:суббота|воскресенье|пятница|четверг|среда|понедельник|вторник)\b"
    # FR
    r"|\b(?:demain|samedi|dimanche|vendredi|jeudi|mercredi|lundi|mardi|ce\s+week-end|après-demain)\b"
    # EN
    r"|\b(?:tomorrow|saturday|sunday|friday|thursday|wednesday|monday|tuesday|this\s+weekend|day\s+after\s+tomorrow)\b"
    # Confirmations génériques post-LE (Fix 2)
    r"|договорились|идёт$|d.accord$|let.s\s+do\s+it|it.s\s+a\s+date|sounds\s+good",
    re.IGNORECASE,
)
# Fix 1 : urgency regex (couvre les variantes générées par le LLM)
_URGENCY_PLAYED_RE = re.compile(
    r"улетаю\b|уезжаю\b.*москв|улечу\b|скоро\s+в\s+москву"
    r"|flying\s+back|heading\s+back\s+to\s+moscow"
    r"|rentre\s+[àa]\s+moscou|je\s+rentre\b",
    re.IGNORECASE,
)
# Fix 5 : goodbye exclusion pour meet signal
_MEET_GOODBYE_RE = re.compile(
    r"пока\s+увидимся|до\s+свидания|спокойной\s+ночи|до\s+завтра\s+увидимся"
    r"|see\s+you\s+(?:later|soon|tomorrow)|bye|cya|au\s+revoir|bonne\s+nuit",
    re.IGNORECASE,
)


def _build_conversion_state(user_id: str, lang: str) -> dict | None:
    """
    Analyse l'historique et retourne l'état courant de la voie de conversion.
    Retourne None si rien d'utile à injecter (début de conv ou conversion terminée).
    """
    hist = histories.get(user_id, [])
    if len(hist) < 4:
        return None

    assistant_msgs = [m["content"] for m in hist if m.get("role") == "assistant"]

    link_sent = any("the-londoneye.com" in m for m in assistant_msgs)
    # Fix 1 : regex plutôt que substring exact pour capturer les variantes LLM
    urgency_played = any(_URGENCY_PLAYED_RE.search(m) for m in assistant_msgs)
    london_eye_discussed = any(
        "london eye" in m.get("content", "").lower() for m in hist
    )

    # Fix 2 : date agreed — jours seuls + confirmations génériques post-LE
    date_agreed = False
    le_first_idx = next(
        (i for i, m in enumerate(hist)
         if m.get("role") == "assistant" and "london eye" in m.get("content", "").lower()),
        None,
    )
    if le_first_idx is not None:
        post_le_user = [m["content"] for m in hist[le_first_idx:] if m.get("role") == "user"]
        date_agreed = any(_DATE_AGREED_RE.search(t) for t in post_le_user)

    # Rien d'utile si lien envoyé (link_followup gère la suite)
    if link_sent:
        return None

    # Fix 15 : fenêtre d'objection réduite à hist[-4:] (2 derniers échanges)
    # → objection doit être très récente pour générer une instruction
    very_recent_user = [m["content"] for m in hist[-4:] if m.get("role") == "user"]

    # Fix 4 : résolution d'objection — si dernier message user = acceptation, annule l'objection
    last_user_msg = next(
        (m["content"] for m in reversed(hist) if m.get("role") == "user"), ""
    )
    _objection_resolved = bool(_OBJ_RESOLVED_RE.search(last_user_msg))

    obj_expensive = (not _objection_resolved) and any(_OBJ_EXPENSIVE_RE.search(t) for t in very_recent_user)
    obj_not_free  = (not _objection_resolved) and any(_OBJ_NOT_FREE_RE.search(t)  for t in very_recent_user)
    obj_vague     = (not _objection_resolved) and any(_OBJ_VAGUE_RE.search(t)     for t in very_recent_user)
    obj_no        = any(_OBJ_NO_RE.search(t) for t in very_recent_user)  # refus = jamais résolu par "ok"

    # Fix 14 : rien de notable → pas d'injection (évite le bruit sur chaque tour)
    if not any([urgency_played, london_eye_discussed, obj_expensive, obj_not_free, obj_vague, obj_no]):
        return None

    return {
        "link_sent":             link_sent,
        "urgency_played":        urgency_played,
        "london_eye_discussed":  london_eye_discussed,
        "date_agreed":           date_agreed,
        "obj_expensive":         obj_expensive,
        "obj_not_free":          obj_not_free,
        "obj_vague":             obj_vague,
        "obj_no":                obj_no,
    }


# ─── A/B test ouverture ──────────────────────────────────────

def _ab_get_variant(user_id: str) -> str:
    """Retourne 'A' ou 'B' pour ce user — attribue aléatoirement si premier contact."""
    try:
        data = json.loads(_AB_FILE.read_text(encoding="utf-8")) if _AB_FILE.exists() else {}
    except Exception:
        data = {}
    if user_id in data:
        return data[user_id].get("variant", "A")
    variant = random.choice(["A", "B"])
    data[user_id] = {"variant": variant, "assigned_at": _time.time(), "replied": False}
    try:
        _AB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    log("ABT", f"Variant assigné : {variant}", f"user={user_id}")
    return variant


def _ab_mark_replied(user_id: str):
    """Marque que l'user a répondu au premier message — mesure l'efficacité du variant."""
    try:
        data = json.loads(_AB_FILE.read_text(encoding="utf-8")) if _AB_FILE.exists() else {}
    except Exception:
        return
    if user_id in data and not data[user_id].get("replied"):
        data[user_id]["replied"] = True
        data[user_id]["replied_at"] = _time.time()
        try:
            _AB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log("ABT", f"Replied enregistré (variant={data[user_id]['variant']})", f"user={user_id}")
        except Exception:
            pass


def _build_reminders(user_id: str, turn: int, user_msg: str) -> list[dict]:
    """Construit les injections système dynamiques pour ce tour."""
    injections = []
    _lang_now = _user_lang.get(user_id, "ru")

    # ── Pré-calcul partagé (évite de recalculer dans chaque section) ──
    _hist_all = histories.get(user_id, [])
    _link_already_sent = any(
        "the-londoneye.com" in m.get("content", "")
        for m in _hist_all if m["role"] == "assistant"
    )
    # Fix 8 : urgency_used via regex (pas substring exact)
    _urgency_used = any(
        _URGENCY_PLAYED_RE.search(m.get("content", ""))
        for m in _hist_all if m["role"] == "assistant"
    )
    # Fix 9 : London Eye déjà sur la table → ne pas déclencher urgence
    _le_discussed = any("london eye" in m.get("content", "").lower() for m in _hist_all)

    # ── État de conversion — Fix 7 : priorité unique, une seule instruction à la fois ──
    # Si état contient une instruction (objection active) → skip urgence et meet signal.
    # Si meet signal → skip urgence. Ordre de priorité : meet > objection > urgence.
    _cvt = _build_conversion_state(user_id, _lang_now)
    _has_instruction = False  # Fix 7 : flag pour éviter instructions contradictoires

    # Fix 15 : l'état de conversion ne s'injecte que si le message actuel est lié
    # à une rencontre/disponibilité, OU si c'est un refus clair (toujours prioritaire).
    # Cela évite que l'injection "propose un jour précis" parasitent une conversation normale.
    _MEET_TOPIC_RE = re.compile(
        r"встрет|свобод|выходн|субботу|воскрес|london eye|когда|когд|занят"
        r"|libre|week.?end|samedi|dimanche|meet|free|busy|weekend|saturday|sunday",
        re.IGNORECASE,
    )
    _msg_is_meet_related = bool(_MEET_TOPIC_RE.search(user_msg))

    # Fix 14 : n'injecte l'état QUE si une instruction actionnable existe
    if _cvt:
        objections = []
        instruction = ""
        if _cvt["obj_no"]:
            # Refus clair → toujours injecter (important de ne pas insister)
            objections.append("refus clair")
            if _lang_now == "fr":
                instruction = "Il a refusé clairement. Ne force pas. Change de sujet, laisse tomber London Eye pour cette conv."
            elif _lang_now == "en":
                instruction = "He clearly refused. Don't push. Change subject, drop London Eye for this conv."
            else:
                instruction = "Он отказался чётко. Не дави. Смени тему, отпусти London Eye в этой конве."
        elif _msg_is_meet_related:
            # Les autres objections : seulement si la conversation porte sur une rencontre
            if _cvt["obj_not_free"]:
                objections.append("pas dispo")
                if _lang_now == "fr":
                    instruction = "Il n'est pas libre. Propose une alternative immédiate : 'le week-end t'es libre ?' — sans lâcher la conversion."
                elif _lang_now == "en":
                    instruction = "He's busy. Propose an alternative: 'what about the weekend ?' — don't give up the conversion."
                else:
                    instruction = "Он не свободен. Предложи альтернативу сразу: 'в выходные что?' — не отпускай воронку."
            elif _cvt["obj_expensive"]:
                objections.append("дорого")
                if _lang_now == "fr":
                    instruction = "Il dit que c'est cher. Légère surprise seulement : 'c'est 10 livres en ligne, c'est cher ça ?' — puis attends. Zéro justification."
                elif _lang_now == "en":
                    instruction = "He thinks it's expensive. Just slight surprise: '10 pounds online, that's expensive ?' — then wait. Zero justification."
                else:
                    instruction = "Он говорит дорого. Только лёгкое удивление: '10 фунтов онлайн серьёзно что ли' — потом ждёшь. Ноль объяснений."
            elif _cvt["obj_vague"]:
                objections.append("peut-être/посмотрим")
                if _lang_now == "fr":
                    instruction = "Il est vague. Propose un jour précis : 'samedi ou dimanche ?' — si encore flou → 'ok écris-moi quand t'es libre' et lâche."
                elif _lang_now == "en":
                    instruction = "He's vague. Pin down a day: 'saturday or sunday ?' — if still vague → 'ok let me know when you're free' and drop it."
                else:
                    instruction = "Он уклончив. Уточни день: 'в субботу или воскресенье?' — снова уклоняется → 'окей, напиши когда свободен' и отпускаешь."

        # Fix 14 : injection seulement si instruction actionnable
        if instruction:
            lines = []
            lines.append(f"Urgence jouée : {'oui' if _cvt['urgency_played'] else 'non'}")
            if _cvt["london_eye_discussed"]:
                lines.append("London Eye déjà évoqué")
            if _cvt["date_agreed"]:
                lines.append("User a confirmé une date")
            lines.append(f"Objection : {', '.join(objections)}")
            state_txt = "[ÉTAT CONVERSION] " + " | ".join(lines) + f"\n→ {instruction}"
            injections.append({"role": "user", "content": state_txt})
            _has_instruction = True  # Fix 7 : bloque urgence et meet

    # ── Rappel périodique des règles critiques (tours 5, 10, 15 …) ──
    if turn >= 5 and turn % 5 == 0:
        injections.append({
            "role": "user",
            "content": (
                "[SYSTÈME RAPPEL — règles absolues]\n"
                "1. Minuscules SEULEMENT — jamais de majuscule sauf noms propres (en russe ET en anglais)\n"
                "2. Jamais '!' dans aucun message\n"
                "3. Jamais le schéma [réaction] + [info sur toi] + [question]\n"
                "4. Jamais de liste (virgule, virgule, virgule)\n"
                "5. Réponse proportionnelle à son message — s'il est court, toi aussi\n"
                "6. Tu es Катя, 21 ans, de Moscou, habites Camden, pas étudiante\n"
                "7. ')' INTERDIT dans ce message et tous les suivants — sauf si c'est le TOUT PREMIER message et qu'il n'a écrit que 'привет/hi/bonjour'"
            ),
        })

    # ── Alerte taux de questions élevé ──
    recent_q = _count_recent_questions(user_id, window=4)
    if recent_q >= 2:
        injections.append({
            "role": "user",
            "content": (
                f"[SYSTÈME] Tu as posé {recent_q} question(s) dans tes 4 derniers messages. "
                "Pour CE message : NE TERMINE PAS par '?'. "
                "Réagis seulement, ne pose aucune question."
            ),
        })

    # ── Détection signal de rencontre → injection London Eye immédiate ──
    # Fix 5 : exclut les adieux ("увидимся" = "à plus")
    # Fix 6 : turn >= 4 (besoin d'un minimum de connexion avant de proposer un lieu)
    # Fix 7 : skip si instruction d'objection déjà injectée
    _MEET_RE = re.compile(
        r"встретимся|давай\s+встретимся|где\s+встретимся|пойдём\s+куда|куда\s+пойдём"
        r"|встретиться\b|выйдем\s+вместе|гулять\s+вместе"
        r"|on\s+se\s+voit\b|on\s+se\s+retrouve|on\s+peut\s+se\s+voir"
        r"|où\s+on\s+se\s+voit|let.s\s+meet\b|wanna\s+meet\b|wanna\s+hang\b",
        re.IGNORECASE,
    )
    _meet_signal = (
        _MEET_RE.search(user_msg)
        and not _MEET_GOODBYE_RE.search(user_msg)   # Fix 5 : pas un adieu
        and not _link_already_sent
        and not _has_instruction                     # Fix 7 : pas d'instruction concurrente
        and turn >= 4                               # Fix 6 : connexion minimale requise
    )
    if _meet_signal:
        if _lang_now == "fr":
            _meet_hint = (
                "[SYSTÈME CONVERSION URGENT] L'utilisateur propose de se voir — c'est le moment parfait. "
                "Propose IMMÉDIATEMENT le London Eye avec un créneau concret. "
                "Exemple: 'j'avais envie d'aller au london eye — samedi ça te va ?' "
                "Pas de parc, pas de café. London Eye + jour précis (billets valables toute la journée, pas d'heure)."
            )
        elif _lang_now == "en":
            _meet_hint = (
                "[SYSTÈME CONVERSION URGENT] User wants to meet — perfect moment. "
                "Propose the London Eye IMMEDIATELY with a specific slot. "
                "Example: 'i wanted to go to the london eye — saturday works for you ?' "
                "No parks, no cafes. London Eye + day (tickets are valid all day, no specific time needed)."
            )
        else:
            _meet_hint = (
                "[SYSTÈME CONVERSION URGENT] Пользователь предлагает встретиться — это твой момент. "
                "НЕМЕДЛЕННО предложи London Eye с конкретным днём. "
                "Пример: 'я как раз хотела на london eye — в субботу подходит?' "
                "Только London Eye, конкретный день (билеты на весь день, без указания часа)."
            )
        injections.append({"role": "user", "content": _meet_hint})
        _has_instruction = True  # Fix 7 : bloque urgence

    # ── Pivot London Eye si rencontre acceptée ailleurs ──
    # Si le bot a récemment accepté une sortie NON London Eye → forcer le pivot
    if not _link_already_sent and not _has_instruction and turn >= 4:
        _NON_LE_RE = re.compile(
            r"\bbar\b|café|cafe|resto\b|restaurant\b|parc\b|park\b|cinéma\b|cinema\b"
            r"|кафе|бар|ресторан|парк|кино|пойдём\s+выпьем|выпьем\s+вместе"
            r"|пойдём\s+куда.нибудь(?!\s*london)",
            re.IGNORECASE,
        )
        _recent_asst = [m["content"] for m in histories.get(user_id, [])[-6:] if m.get("role") == "assistant"]
        _non_le_accepted = any(_NON_LE_RE.search(m) for m in _recent_asst) and not any("london eye" in m.lower() for m in _recent_asst[-3:])
        if _non_le_accepted:
            if _lang_now == "fr":
                _pivot_hint = (
                    "[SYSTÈME CONVERSION] Tu as accepté une sortie sans mentionner London Eye récemment. "
                    "MAINTENANT pivote : glisse London Eye naturellement dans ce message. "
                    "Exemple : 'avant le bar, j'avais envie d'aller au london eye — ça te va ?' "
                    "Ne répète pas la même chose, juste un hook naturel vers London Eye."
                )
            elif _lang_now == "en":
                _pivot_hint = (
                    "[SYSTÈME CONVERSION] You recently agreed to meet without mentioning London Eye. "
                    "NOW pivot: slip London Eye in naturally. "
                    "Example: 'before the bar i wanted to check out london eye — you up for it?' "
                )
            else:
                _pivot_hint = (
                    "[SYSTÈME CONVERSION] Ты недавно согласилась на встречу не в London Eye. "
                    "СЕЙЧАС сделай пивот: вырони London Eye естественно. "
                    "Пример: 'до бара хотела зайти на london eye — ты как?' "
                    "Один раз, коротко, натурально."
                )
            injections.append({"role": "user", "content": _pivot_hint})
            _has_instruction = True

    return injections

def _reply_is_clean(text: str, user_id: str = "", turn: int = 0, lang: str = "ru") -> bool:
    """True si le message passe tous les filtres qualité."""
    is_fr = (lang == "fr")
    is_en = (lang == "en")
    return (
        not _has_banned(text)
        and not _has_forbidden_emoji(text)
        and (is_fr or is_en or not _has_english(text))
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

async def _generate_context_summary(user_id: str, username: str) -> None:
    """Génère un résumé compact des faits clés de la conversation (async, non-bloquant).
    Appelé toutes les 2 réponses à partir du tour 4. Persiste dans histories.json."""
    hist = histories.get(user_id, [])
    if len(hist) < 6:
        return
    lines = []
    for m in hist[-20:]:
        role = "Katia" if m["role"] == "assistant" else "User"
        content = m["content"][:120].replace("\n", " ")
        lines.append(f"{role}: {content}")
    conv_text = "\n".join(lines)
    link_sent = any("the-londoneye.com" in m.get("content", "") for m in hist if m["role"] == "assistant")
    prompt = (
        f"Conversation entre Katia (bot de rencontre Londres) et un utilisateur:\n{conv_text}\n\n"
        "Extrais les faits clés en 6 lignes max, format compact:\n"
        "- prénom: (si mentionné, sinon 'inconnu')\n"
        "- origine: (ville/pays si mentionné)\n"
        "- situation: (travail, voyage prévu à Londres, etc.)\n"
        f"- london eye: ({'lien envoyé' if link_sent else 'pas mentionné / intéressé / a refusé'})\n"
        "- sujets: (thèmes principaux abordés)\n"
        "- ton: (froid/neutre/chaleureux/hostile)\n"
        "Réponds UNIQUEMENT avec ces 6 lignes, rien d'autre."
    )
    try:
        resp = await asyncio.to_thread(
            client_anthropic.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = resp.content[0].text.strip()
        _user_context[user_id] = summary
        asyncio.create_task(save_histories_async())
        log("CTX", f"Contexte mis a jour (tour {len(hist)//2})", f"user={username}")
    except Exception as e:
        log("WRN", f"Erreur generation contexte", f"user={username} | {e}")


async def _validate_reply_coherence(user_msg: str, reply: str) -> bool:
    """Vérifie avec Haiku si la réponse est cohérente avec le message reçu.
    Retourne True (valide) par défaut en cas d'erreur pour ne jamais bloquer."""
    # Skip pour les cas où la validation n'apporte rien
    if len(reply.split()) <= 6:
        return True
    if len(user_msg.split()) <= 3:
        return True
    if "the-londoneye.com" in reply:
        return True
    prompt = (
        f'Message reçu: "{user_msg[:300]}"\n'
        f'Réponse générée: "{reply[:300]}"\n\n'
        "La réponse répond-elle logiquement au message reçu ? "
        "Réponds UNIQUEMENT par YES ou NO."
    )
    try:
        resp = await asyncio.to_thread(
            client_anthropic.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        return "YES" in resp.content[0].text.upper()
    except Exception:
        return True


def get_eva_response(user_id: str, username: str, user_message: str) -> str:
    if user_id not in histories:
        histories[user_id] = []
        log("NEW", f"Nouvelle conversation", f"user={username} id={user_id}")

    turn = len(histories[user_id]) // 2 + 1

    # Détection et mémorisation de la langue
    detected = _detect_lang(user_message)
    current_lang = _user_lang.get(user_id, "ru")
    if detected == "fr":
        _user_lang[user_id] = "fr"
    elif detected == "en":
        _user_lang[user_id] = "en"
    elif user_id not in _user_lang:
        _user_lang[user_id] = "ru"
    elif current_lang in ("fr", "en"):
        # Retour au russe seulement si 3+ messages récents clairement non-fr/non-en
        recent_u = [m["content"] for m in histories.get(user_id, []) if m["role"] == "user"][-4:]
        recent_u.append(user_message)
        clear_msgs = [m for m in recent_u if len(m.strip()) > 3]
        if len(clear_msgs) >= 3 and all(_detect_lang(m) == "ru" for m in clear_msgs):
            _user_lang[user_id] = "ru"
    lang = _user_lang[user_id]
    log("GPT", f"Generation reponse (tour {turn}) [{lang}]", f"user={username}")

    histories[user_id].append({"role": "user", "content": user_message})

    # Garde uniquement les 20 derniers messages (10 échanges)
    MAX_HISTORY = 20
    if len(histories[user_id]) > MAX_HISTORY:
        histories[user_id] = histories[user_id][-MAX_HISTORY:]

    # ── A/B test : marquer replied dès le tour 2 ──
    if turn == 2:
        _ab_mark_replied(user_id)

    # ── Injection messages multiples — répond à TOUS ──
    # Format [MSG N]: dans l'historique pour que les futurs tours voient les messages séparés
    _multi_msgs = [m.strip() for m in user_message.split("\n") if m.strip()]
    if len(_multi_msgs) >= 2:
        # Reformate le message stocké dans l'historique pour clarifier les msgs séparés
        _formatted = "\n".join(f"[MSG {i+1}]: {m}" for i, m in enumerate(_multi_msgs))
        histories[user_id][-1]["content"] = _formatted
    _multi_injection = None
    if len(_multi_msgs) >= 2:
        _listed = " / ".join(f'"{m}"' for m in _multi_msgs)
        if lang == "fr":
            _multi_injection = {
                "role": "user",
                "content": (
                    f"[SYSTÈME] Il a envoyé {len(_multi_msgs)} messages d'affilée : {_listed}. "
                    "Tu DOIS répondre à TOUS — pas seulement au dernier. "
                    "Intègre-les dans une seule réponse naturelle sans les lister."
                ),
            }
        elif lang == "en":
            _multi_injection = {
                "role": "user",
                "content": (
                    f"[SYSTÈME] He sent {len(_multi_msgs)} messages in a row: {_listed}. "
                    "You MUST address ALL of them — not just the last one. "
                    "Blend them into one natural reply, don't list them."
                ),
            }
        else:
            _multi_injection = {
                "role": "user",
                "content": (
                    f"[SYSTÈME] Он написал {len(_multi_msgs)} сообщения подряд: {_listed}. "
                    "Ты ОБЯЗАНА ответить на ВСЕ — не только на последнее. "
                    "Соедини их в один естественный ответ, не перечисляй."
                ),
            }

    # ── Injection tour 1 : forcer "привет)" SEULEMENT si l'user a envoyé un salut ──
    _GREETING_RE = re.compile(
        r"^[\W]*(привет|привіт|хай|hi|hey|hello|yo|здравствуй|ку|ку-ку|хэй|прив|хеllo|sup|salut|bonjour|coucou)[\W]*$",
        re.IGNORECASE
    )
    _is_first_greeting = (turn == 1 and _GREETING_RE.match(user_message.strip()))
    _is_first_sticker = (turn == 1 and user_message.startswith("[стикер"))
    if _is_first_greeting:
        _turn1_injection = {
            "role": "user",
            "content": (
                "[SYSTÈME] C'est ton PREMIER message dans cette conversation. "
                "Réponds UNIQUEMENT avec 'привет)' — rien d'autre, aucune question, "
                "aucun emoji, aucune salutation enthousiaste. Un seul mot."
            ),
        }
    elif _is_first_sticker:
        _turn1_injection = {
            "role": "user",
            "content": (
                "[SYSTÈME] C'est le tout premier message — un sticker. "
                "Réponds avec 'привет)' ou une question courte et légère sur qui il est. "
                "NE DIS PAS ce que tu es en train de faire (pas de 'je lis', 'je travaille', etc.). "
                "Court, curieux, naturel."
            ),
        }
    else:
        _turn1_injection = None

    # ── A/B variant B : premier message plus direct/personnel ──
    _ab_injection = None
    if turn == 1 and _ab_get_variant(user_id) == "B":
        _ab_injection = {
            "role": "user",
            "content": (
                "[SYSTÈME A/B] Variant B — premier message : sois directe et légèrement piquante. "
                "Pose une question précise sur quelque chose dans son profil (ville, activité, photo) "
                "ou sur ce qu'il fait ce soir. Évite les salutations génériques. "
                "1 phrase max, minuscules, naturel."
            ),
        }

    # ── Injection demande de photo → rappel envoi vidéo ──
    from zvukogram_agent import pick_voice_for  # noqa: F811 (already imported below)
    _wants_photo = bool(re.search(
        r"отправь\s*фот|пришли\s*фот|скинь\s*фот|покажи\s*(себ|фот)|фотк|фоточк"
        r"|отправь\s*(свое?\s*)?(фот|фото\b)|дай\s*фот|кинь\s*фот"
        r"|envoie.*(photo|toi)|photo\s*de\s*toi|montre.*(toi|photo)|t'as\s*(une\s*)?photo"
        r"|\bвид[её]о\b|скинь\s*вид[её]|пришли\s*вид[её]|покажи\s*себ|запишись|снимись"
        r"|хочу\s*(тебя\s*)?(увидеть|видеть|вид[её]о)|вид[её]о\s*(хочу|давай|дай|скинь|пришли)",
        user_message, re.IGNORECASE
    ))
    _photo_injection = {
        "role": "user",
        "content": (
            "[SYSTÈME] L'utilisateur demande une photo/vidéo de toi. "
            "Une vidéo va être envoyée automatiquement juste avant ton message. "
            "Dis UNIQUEMENT 'держи' ou 'на' ou 'тут' (1 mot) — JAMAIS 'sur mon profil', "
            "JAMAIS 'на профиле есть', JAMAIS de lien. Juste le mot d'accompagnement."
        ),
    } if _wants_photo else None

    # ── Injection photo de profil — tours 2-5 seulement (pas tour 1 pour ne pas parasiter le salut) ──
    _profile_desc = _user_profile_desc.get(user_id, "")
    _profile_injection = {
        "role": "user",
        "content": (
            f"[SYSTÈME] Photo de profil de l'utilisateur analysée : {_profile_desc}. "
            "Tu peux t'en souvenir pour personnaliser la conversation si l'occasion se présente "
            "(ex: commenter ses cheveux, son style) — mais seulement si c'est naturel et pertinent. "
            "Ne lance PAS la conversation sur la photo si le sujet n'est pas lié."
        ),
    } if (_profile_desc and 2 <= turn <= 5) else None

    # ── Injection anti-lien double ──
    _link_in_history = any(
        "the-londoneye.com" in m.get("content", "")
        for m in histories[user_id]
        if m["role"] == "assistant"
    )
    _link_injection = {
        "role": "user",
        "content": (
            "[SYSTÈME] Tu as DÉJÀ envoyé le lien https://the-londoneye.com/tickets "
            "dans cette conversation. NE PAS le renvoyer une seconde fois."
        ),
    } if _link_in_history else None

    # ── Injection langue ──
    if lang == "fr":
        _lang_injection = {
            "role": "user",
            "content": (
                "[SYSTÈME] L'utilisateur écrit en FRANÇAIS. "
                "Réponds UNIQUEMENT en français, même style court et naturel, "
                "même caractère qu'en russe. Minuscules, pas de '!', pas de listes."
            ),
        }
    elif lang == "en":
        _lang_injection = {
            "role": "user",
            "content": (
                "[SYSTÈME] The user writes in ENGLISH. "
                "Reply ONLY in English, same short casual style, same character as in Russian. "
                "Lowercase, no '!', no lists. Natural British girl texting style."
            ),
        }
    else:
        _lang_injection = None

    # ── Calcul max_tokens selon longueur du message entrant ──
    _max_tok = _max_tokens_for_input(user_message)

    # ── Injections dynamiques (rappels périodiques + alerte questions) ──
    _dynamic_injections = _build_reminders(user_id, turn, user_message)

    # ── Consolidation : toutes les injections en UN seul message ──
    # Envoyer 7 messages "user" consécutifs après le vrai message confuse le modèle.
    # On regroupe tout dans un seul bloc [SYSTÈME] pour que le modèle voit clairement
    # la séparation entre la conversation réelle et les instructions système.
    def _build_consolidated_injection(extra_retry: str = "") -> dict | None:
        parts = []
        # Contexte résumé de la conversation (faits clés mémorisés)
        _ctx = _user_context.get(user_id)
        if _ctx:
            parts.append(f"[CONTEXTE CONVERSATION]\n{_ctx}")
        # Multi-messages et tour 1 sont des instructions prioritaires : en tête
        if _multi_injection:
            parts.append(_multi_injection["content"])
        if _turn1_injection:
            parts.append(_turn1_injection["content"])
        if _ab_injection:
            parts.append(_ab_injection["content"])
        if _photo_injection:
            parts.append(_photo_injection["content"])
        if _profile_injection:
            parts.append(_profile_injection["content"])
        if _link_injection:
            parts.append(_link_injection["content"])
        if _lang_injection:
            parts.append(_lang_injection["content"])
        for inj in _dynamic_injections:
            parts.append(inj["content"])
        if extra_retry:
            parts.append(extra_retry)
        if not parts:
            return None
        return {"role": "user", "content": "\n\n".join(parts)}

    reply = ""
    for attempt in range(4):
        messages = [{"role": "system", "content": SOUL}] + histories[user_id]

        _retry_txt = ""

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
            _retry_txt = (
                f"[SYSTÈME] Problème(s) dans ta réponse précédente: {'; '.join(issues)}. "
                "Réécris-la: courte, naturelle, 100% russe minuscule, sans les éléments listés."
            )

        # Injection consolidée : un seul message [SYSTÈME] au lieu de 7 séparés
        _consolidated = _build_consolidated_injection(_retry_txt)
        if _consolidated:
            messages.append(_consolidated)

        # Sépare system des messages user/assistant pour Anthropic
        anthropic_messages = [m for m in messages if m["role"] != "system"]
        # Injecte la description des propres photos de Katia dans le system prompt si disponible
        _soul_text = SOUL
        if _own_profile_desc:
            _soul_text = (
                f"[TES PHOTOS DE PROFIL TELEGRAM — ce à quoi tu ressembles réellement : "
                f"{_own_profile_desc}. "
                f"Utilise ces infos pour répondre précisément aux questions sur ton apparence. "
                f"Ne révèle jamais que tu as 'analysé' quoi que ce soit — parle naturellement.]\n\n"
                + SOUL
            )
        # Prompt caching : le SOUL est identique à chaque appel → Anthropic le met en cache
        # après le 1er appel (TTL 5 min). Coût du cache : $0.30/M au lieu de $3/M = 10x moins cher.
        _system_cached = [{"type": "text", "text": _soul_text, "cache_control": {"type": "ephemeral"}}]
        response = client_anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=_max_tok,
            system=_system_cached,
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

    # ── Filtre placeholders — l'IA ne doit JAMAIS envoyer des textes de type [action] ──
    # Attrape : [brackets], **bold**, *italic*, (parens), texte brut — toutes variantes
    _SEND_VERB  = r"(?:sends?|sent|sended?|sending|envoie?s?|отправил[аи]?|отправляю)"
    _MEDIA_WORD = r"(?:voice|vocal|audio|video|media|кружок|видео|голос[оаое]?)"
    _PH_OPEN    = r"(?:\[|\*{1,2}|\()"
    _PH_CLOSE   = r"(?:\]|\*{1,2}|\))"
    _VOICE_PLACEHOLDER_RE = re.compile(
        # bracket classiques
        r"\[voix\s+envoy[e\xe9]+e?\]"
        r"|\[голосов[оаое][е]?\]"
        r"|\[voice\s+(?:sent|note|message)\]"
        r"|\[vocal\]"
        r"|\[отправила\s+(?:видео|кружок|голосовое)\]"
        r"|\[кружок\]|\[видео\]|\[media\]"
        r"|\[video\s*(?:sent)?\]"
        r"|\[стикер[^\]]*\]|\[фото[^\]]*\]|\[аудио[^\]]*\]"
        # *sends voice* / **sent voice** / (sends audio) … — délimiteur + verbe + media
        r"|" + _PH_OPEN + _SEND_VERB + r"\s+" + _MEDIA_WORD + _PH_CLOSE
        + r"|" + _PH_OPEN + _MEDIA_WORD + r"\s+" + _SEND_VERB + _PH_CLOSE
        + r"|" + _PH_OPEN + _MEDIA_WORD + _PH_CLOSE
        # texte brut : sends voice / sending audio / sended anything
        + r"|\b" + _SEND_VERB + r"\s+" + _MEDIA_WORD + r"\b"
        + r"|\bsended\b",
        re.IGNORECASE
    )
    if _VOICE_PLACEHOLDER_RE.search(reply):
        log("WRN", f"Placeholder media detecte et supprime", f'"{reply[:80]}"')
        reply = _VOICE_PLACEHOLDER_RE.sub("", reply).strip() or "держи"

    # ── Filtre révélation de bot ──
    _BOT_REVEAL_RE = re.compile(
        r"каково\s+(?:тебе\s+)?общаться\s+с\s+ботом"
        r"|с\s+ботом\s+каково"
        r"|за\s+\d+\s+час[а-я]*\s+не\s+понял"
        r"|потерял\s+\d+\s+час"
        r"|одновременно\s+со\s+\d+",
        re.IGNORECASE,
    )
    if _BOT_REVEAL_RE.search(reply):
        log("WRN", "Bot-reveal détecté et bloqué", f'"{reply[:80]}"')
        reply = "ладно"

    # ── Filtre monologue interne accidentel ──
    _INTERNAL_MONO_RE = re.compile(
        r"бля,?\s+я\s+тупая"
        r"|он\s+не\s+может\s+взять\s+билет"
        r"|бля,?\s+он\s+не\s+может"
        r"|а\s+точно\s+ёпт"
        r"|ой\s+ёпт",
        re.IGNORECASE,
    )
    if _INTERNAL_MONO_RE.search(reply):
        log("WRN", "Monologue interne détecté et bloqué", f'"{reply[:80]}"')
        reply = "ладно"

    # ── Filtre emojis interdits (hard strip — whitelist 😏/😐 seulement) ──
    if _has_forbidden_emoji(reply):
        log("WRN", "Emoji interdit supprimé (hard strip)", f'"{reply[:80]}"')
        reply = _strip_forbidden_emojis(reply) or "ладно"

    # ── Filtre mauvais nom ──
    _WRONG_NAME_RE = re.compile(r"\b[Аа]лин[аы]\b|\balina\b", re.IGNORECASE)
    if _WRONG_NAME_RE.search(reply):
        log("WRN", "Mauvais nom détecté (alina) — remplacé par катя", f'"{reply[:80]}"')
        reply = _WRONG_NAME_RE.sub("катя", reply)

    # ── Filtre mauvais district (Westminster et autres interdits) ──
    _WRONG_DISTRICT_RE = re.compile(
        r"\b[Вв]естминстер\b|\bWestminster\b"
        r"|\bShoreditch\b|\bHackney\b|\bIslington\b|\bBrixton\b"
        r"|\bNotting\s+Hill\b|\bEast\s+London\b",
        re.IGNORECASE,
    )
    if _WRONG_DISTRICT_RE.search(reply):
        log("WRN", "Mauvais quartier détecté — remplacé par camden", f'"{reply[:80]}"')
        reply = _WRONG_DISTRICT_RE.sub("camden", reply)

    # ── Filtre prix inventés ──
    _INVENTED_PRICE_RE = re.compile(
        r"\b\d+\s*(?:фунт|pound|£|gbp)\b"
        r"|\bонлайн\s+\d+\b"          # "онлайн 6"
        r"|\bна\s+месте\s+\d+\b"      # "на месте 29"
        r"|\bя\s+увидела\s+что\s+скидк"  # "я увидела что скидки"
        r"|\bскидк[иа]\s+(?:сейчас|есть|на)\b",  # "скидки есть"
        re.IGNORECASE,
    )
    if _INVENTED_PRICE_RE.search(reply):
        log("WRN", "Prix inventé détecté et bloqué", f'"{reply[:80]}"')
        reply = re.sub(r"\b\d+\s*(?:фунт|pound|£|gbp)\b", "не дорого", reply, flags=re.IGNORECASE)
        reply = re.sub(r"\bонлайн\s+\d+\b|\bна\s+месте\s+\d+\b", "онлайн дешевле", reply, flags=re.IGNORECASE)
        reply = re.sub(r"\bя\s+увидела\s+что\s+скидк\w*|\bскидк[иа]\s+(?:сейчас|есть|на)\b", "лучше брать онлайн", reply, flags=re.IGNORECASE)

    # ── Filtre heure fixée par le bot elle-même ──
    _AUTO_TIME_RE = re.compile(
        r"\bв\s+\d{1,2}:\d{2}\b"           # "в 14:00"
        r"|\bв\s+\d{1,2}\s+час[аов]\b"      # "в 3 часа"
        r"|\bв\s+(?:полдень|полночь)\b"      # "в полдень"
        r"|\bдавай\s+в\s+\d"                # "давай в 6"
        r"|\bвстретимся\s+в\s+\d",           # "встретимся в 14"
        re.IGNORECASE,
    )
    if _AUTO_TIME_RE.search(reply):
        log("WRN", "Heure fixée par le bot — supprimée", f'"{reply[:80]}"')
        reply = re.sub(r"\bв\s+\d{1,2}:\d{2}\b", "", reply, flags=re.IGNORECASE)
        reply = re.sub(r"\bв\s+\d{1,2}\s+час[аов]\b", "", reply, flags=re.IGNORECASE)
        reply = re.sub(r"\bдавай\s+в\s+\d+\b", "давай", reply, flags=re.IGNORECASE)
        reply = re.sub(r"\bвстретимся\s+в\s+\d+\b", "встретимся", reply, flags=re.IGNORECASE)
        reply = reply.strip() or "ок"

    # ── Filtre vulgaire (réponse neutre si le bot injure) ──
    _VULGAR_RE = re.compile(
        r"\bпизд\w*\b|\bеба\w*\b|\bнахуй\b|\bблядь\b|\bсука\b"
        r"|\bлошар\w*\b|\bдебил\w*\b|\bидиот\w*\b"
        r"|\bchat\s*gpt\s+теб[яе]\s+трах",
        re.IGNORECASE,
    )
    if _VULGAR_RE.search(reply):
        log("WRN", "Vulgaire détecté et bloqué", f'"{reply[:80]}"')
        reply = "не интересно"

    # Comptabilise понятно après validation finale
    if re.search(r"понятно", reply, re.IGNORECASE):
        _poniatno_count[user_id] = _poniatno_count.get(user_id, 0) + 1
    if "😏" in reply:
        _smirk_last[user_id] = turn

    # Stocker en historique la version sans |||  (le LLM ne doit pas voir ce token dans le contexte)
    _history_reply = reply.replace("|||", " ").strip()
    histories[user_id].append({"role": "assistant", "content": _history_reply})
    # La sauvegarde est faite par l'appelant async via save_histories_async()

    tokens = response.usage.input_tokens + response.usage.output_tokens
    _cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    _cache_info = f" cache={_cache_read}tok" if _cache_read else ""
    log("OK ", f"Reponse generee ({tokens} tokens{_cache_info}, tour {turn})", f"user={username}")
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

        # Appel synchrone Anthropic → thread séparé pour ne pas bloquer l'event loop
        from functools import partial
        _vision_call = partial(
            client_anthropic.messages.create,
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
        _img_loop = asyncio.get_running_loop()
        resp = await _img_loop.run_in_executor(None, _vision_call)
        desc = resp.content[0].text.strip()
        log("IMG", f"Vision GPT: {desc[:60]}")
        return f"[фото: {desc}]"
    except Exception as e:
        log("ERR", f"Vision GPT echec: {e}")
        return "[фото]"


# ─── Analyse photo de profil ─────────────────────────────────

async def _fetch_profile_desc(bot_client, user_id: str, username: str) -> str | None:
    """
    Télécharge la photo de profil Telegram de l'utilisateur et la décrit
    en une phrase via Claude Vision.
    Résultat mis en cache dans _user_profile_desc.
    """
    if user_id in _user_profile_desc:
        return _user_profile_desc[user_id]
    try:
        buf = io.BytesIO()
        bytes_downloaded = await bot_client.download_profile_photo(
            int(user_id), file=buf, download_big=False
        )
        if not bytes_downloaded:
            _user_profile_desc[user_id] = ""
            return None
        buf.seek(0)
        data = buf.read()
        if not data:
            _user_profile_desc[user_id] = ""
            return None
        b64 = base64.b64encode(data).decode("utf-8")

        from functools import partial
        _call = partial(
            client_anthropic.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
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
                            "Décris cette photo de profil en 1 courte phrase en russe minuscule "
                            "du point de vue d'Katia qui regarde le profil de quelqu'un sur une app de rencontres. "
                            "Sois factuelle : apparence physique visible (cheveux, yeux, sourire, style, "
                            "fond, ambiance). Pas de jugement. Exemple: 'парень с короткими тёмными волосами, "
                            "улыбается, городской фон' / 'девушка со светлыми волосами, море на фоне'"
                        )
                    }
                ]
            }]
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, _call)
        desc = resp.content[0].text.strip()
        _user_profile_desc[user_id] = desc
        log("PFP", f"Photo profil analysee: {desc[:70]}", f"user={username}")
        return desc
    except Exception as e:
        log("WRN", f"Photo profil echec: {e}", f"user={username}")
        _user_profile_desc[user_id] = ""
        return None


async def _analyze_own_profile_photos(bot_client) -> str:
    """
    Télécharge les photos de profil du compte Katia et les décrit via Claude Vision.
    Appelé une fois au démarrage. Résultat stocké dans _own_profile_desc.
    Eva s'en sert pour répondre cohéremment aux questions sur son apparence.
    """
    global _own_profile_desc
    try:
        from telethon.tl.functions.photos import GetUserPhotosRequest
        photos_result = await bot_client(GetUserPhotosRequest(
            user_id="me", offset=0, max_id=0, limit=5
        ))
        photos = photos_result.photos if hasattr(photos_result, "photos") else []
        if not photos:
            log("PFP", "Aucune photo de profil trouvée pour Katia")
            return ""

        descriptions = []
        from functools import partial
        for i, photo in enumerate(photos[:3]):  # max 3 photos
            try:
                buf = io.BytesIO()
                await bot_client.download_media(photo, file=buf)
                buf.seek(0)
                data = buf.read()
                if not data:
                    continue
                b64 = base64.b64encode(data).decode("utf-8")

                _call = partial(
                    client_anthropic.messages.create,
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
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
                                    "Réponds en russe minuscule. Décris uniquement ce que tu vois "
                                    "dans cette image de profil : couleur dominante des cheveux "
                                    "(blonds/bruns/noirs/roux/châtains), longueur des cheveux "
                                    "(courts/mi-longs/longs), style des vêtements visibles, "
                                    "décor ou fond (intérieur/extérieur/nature/ville), ambiance générale. "
                                    "Format court, factuel, sans jugement. "
                                    "Exemple: 'светлые длинные волосы, casual одежда, городской фон' / "
                                    "'тёмные волосы до плеч, нарядный стиль, ресторан'"
                                )
                            }
                        ]
                    }]
                )
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(None, _call)
                desc = resp.content[0].text.strip()
                descriptions.append(f"photo {i+1}: {desc}")
                log("PFP", f"Photo Katia analysee ({i+1}/{min(len(photos),3)}): {desc[:70]}")
            except Exception as e:
                log("WRN", f"Photo Katia {i+1} echec: {e}")

        if descriptions:
            _own_profile_desc = " | ".join(descriptions)
            log("PFP", f"Description Katia complete: {_own_profile_desc[:100]}")
        return _own_profile_desc
    except Exception as e:
        log("WRN", f"Analyse photos Katia echec: {e}")
        return ""


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
    # Russe — vidéo (large : "видео я хочу", "хочу видео", "пришли видео", etc.)
    r"скинь\s*вид[её]|пришли\s*вид[её]|покажи\s*(себ[яе]|вид[её])|вид[её](о|ос|осик|ео)?\s*(от\s*тебя|свое|пришли|скинь)"
    r"|хочу\s*(тебя\s*)?(увидеть|видеть|вид[её]о)|вид[её]о\s*(хочу|давай|дай|скинь|пришли|покажи)"
    r"|\bвид[её]о\b"  # "видео" seul dans un court message = demande implicite
    r"|запишись|покажись|снимись"
    # Russe — photo (large : отправь фото, пришли фото, скинь фото, покажи себя, дай фото)
    r"|скинь\s*фот|пришли\s*фот|покажи\s*(себ[яе]|фот)|фотк|фоточк"
    r"|отправь\s*(свое?\s*)?(фот|фотк|фоточк|фото\b)"
    r"|дай\s*(свое?\s*)?фот|кинь\s*(свое?\s*)?фот"
    r"|\bфото\s*отправь\b|\bфото\s*пришли\b"
    # Français — photo/vidéo
    r"|montre.*(toi|photo|vid[eé]o)|envoie.*(photo|vid[eé]o|toi)"
    r"|photo\s*de\s*toi|vid[eé]o\s*de\s*toi|te\s*voir|voir\s*(toi|une\s*photo)"
    r"|t'as\s*(une\s*)?photo|montres?\s*toi"
    r"|renvoi[es]?\s*(la\s*)?(vid[eé]o|photo)|envoie\s*(encore|de\s*nouveau)\s*(la\s*)?(vid[eé]o|photo)"
    r"|\bla\s+vid[eé]o\b|vid[eé]o.*\benvoie[sz]?\b|envoie\s*la\b",
    re.IGNORECASE,
)

def user_wants_video(text: str) -> bool:
    """L'utilisateur demande une vidéo d'Eva."""
    return bool(_VIDEO_REQUEST_RE.search(text))

async def send_eva_video(bot: TelegramClient, chat_id: int, user_id: str) -> bool:
    """
    Envoie la vidéo d'Eva. Maximum 1 fois par conversation.
    Vérifie l'historique persisté — résiste aux redémarrages.
    Retourne True si envoyée, False sinon.
    """
    log("VID", f"Tentative envoi vidéo", f"user={user_id}")
    if not _VIDEO_PATH.exists():
        log("VID", f"Vidéo introuvable", str(_VIDEO_PATH))
        return False
    # Vérification mémoire
    if user_id in _video_sent_users:
        log("VID", f"Vidéo déjà envoyée (mémoire)", f"user={user_id}")
        return False
    # Vérification historique (résiste aux redémarrages)
    already_in_history = any(
        "[отправила видео]" in m.get("content", "")
        for m in histories.get(user_id, [])
        if m.get("role") == "assistant"
    )
    if already_in_history:
        _video_sent_users.add(user_id)  # resync mémoire
        log("VID", f"Vidéo déjà envoyée (historique)", f"user={user_id}")
        return False
    try:
        await bot.send_file(chat_id, str(_VIDEO_PATH), video_note=True)
        _video_sent_users.add(user_id)
        # Inject dans l'historique pour que Claude sache qu'une vidéo vient d'être envoyée
        if user_id in histories:
            histories[user_id].append({"role": "assistant", "content": "[отправила видео]"})
        log("VID", f"Vidéo envoyée avec succes", f"user={user_id}")
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
    Cherche un vocal pertinent (remplace le texte) — match fuzzy >= 0.42.
    """
    if not VOICE_ENABLED or not _CATALOG_ENABLED:
        return None
    from zvukogram_agent import pick_voice_for
    already_sent = _user_voice_history.get(user_id, set())
    # fallback=False : pas de sélection contextuelle aléatoire — uniquement match direct
    # min_score=0.80 : seuil élevé pour éviter les faux positifs
    return pick_voice_for(reply_text, user_text=user_text, fallback=False,
                          exclude=already_sent, min_score=0.80)


def pick_spontaneous_voice(reply_text: str, user_text: str, user_id: str, turn: int) -> "dict | None":
    """
    Vocal d'accompagnement spontané envoyé AVANT le texte (pas à la place).
    Déclenché avec VOICE_SPONTANEOUS_PROB après le tour 2.
    Uniquement pour les users russophones (les vocaux du catalogue sont en russe).
    """
    if not VOICE_ENABLED or not _CATALOG_ENABLED:
        return None
    if turn < 2:
        return None
    # Vocaux en russe uniquement — pas pour FR/EN
    if _user_lang.get(user_id, "ru") != "ru":
        return None
    if random.random() >= VOICE_SPONTANEOUS_PROB:
        return None

    from zvukogram_agent import _detect_context, _NSFW_TRANSCRIPTS, _load_catalog
    already_sent = _user_voice_history.get(user_id, set())
    ctx = _detect_context(reply_text, user_text)

    # Pool de tags selon contexte — casual ouvre un pool large mais sûr
    _ctx_tags = {
        "greeting": ["greeting"],
        "bye":      ["bye"],
        "confirm":  ["confirm"],
        "deny":     ["deny"],
        "reaction": ["reaction"],
        "busy":     ["busy"],
        "flirt":    ["flirt"],
        "casual":   ["casual", "reaction", "confirm"],
    }
    tags = _ctx_tags.get(ctx, ["casual", "reaction"])

    candidates = [
        e for e in _load_catalog()
        if any(t in e.get("tags", []) for t in tags)
        and e.get("transcript", "").lower() not in _NSFW_TRANSCRIPTS
        and e.get("filename") not in already_sent
    ]
    if not candidates:
        return None
    return random.choice(candidates)

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

async def human_think_delay(received_text: str, user_id: str = "", turn: int = 0,
                            chat_id: int = 0) -> None:
    """
    Simule le comportement humain complet avant de répondre :
    1. Reste OFFLINE pendant toute la réflexion (analyse du message)
    2. Passe ONLINE + marque les messages comme lus (double tick bleu)
    3. Reste online le temps de "lire" le message (proportionnel à sa longueur)
    4. Retour → le handler lance le typing immédiatement après

    Mode miroir (tours 1–8) : si l'autre répond vite, elle répond vite — même énergie.
    Tour 1 sans historique d'envoi : délai court par défaut (conv fraîche).
    """
    chars    = len(received_text.strip())
    words_in = len(received_text.split())

    now       = _time.time()
    # Utilise le timestamp du message PRÉCÉDENT pour mesurer l'intervalle réel
    # (last_message_time contient déjà le message courant au moment où on arrive ici)
    last_recv = _prev_message_time.get(user_id, 0)
    last_sent = _last_sent_to.get(user_id, 0)

    gap            = (now - last_recv) if last_recv > 0 else 999.0
    rapid_exchange = gap < 20

    # Temps de réponse de l'interlocuteur (combien de temps il a mis pour répondre à notre dernier msg)
    user_resp_time = (now - last_sent) if last_sent > 0 else 999.0
    # Mode miroir : début de conv + l'autre répond vite (<25s)
    # Étendu au-delà de turn 8 si l'engagement est hot/warm (conversation active)
    _eng = _engagement_level(user_id) if user_id else "warm"
    _mirror_turn_limit = 99 if _eng in ("hot", "warm") else 8
    mirroring = (_mirror_turn_limit >= turn or turn <= 8) and (last_sent > 0) and (user_resp_time < 25)
    # Premier contact (aucun envoi précédent) → réponse rapide par défaut
    first_contact = (last_sent == 0)

    # ── Délai de réflexion offline ──
    if first_contact:
        # Première fois qu'on lui parle — on ne sait pas son rythme, on répond rapidement
        think = random.uniform(5.0, 14.0)
        label = "premier contact"
    elif mirroring:
        # Elle reflète son rythme (légèrement plus lente — elle lit + réfléchit)
        if user_resp_time < 6:
            think = random.uniform(3.0, 9.0)
            label = "miroir express"
        elif user_resp_time < 15:
            think = random.uniform(7.0, 16.0)
            label = "miroir rapide"
        else:
            think = random.uniform(12.0, 22.0)
            label = "miroir modéré"
    elif chars <= 20 and rapid_exchange:
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

    # ── Fenêtre de collecte de rafale (absorbe les messages envoyés en rafale) ──
    # Avant de vraiment "réfléchir", attend 2-3s pour grouper les messages rapides
    burst_window = random.uniform(1.8, 3.2)
    await asyncio.sleep(burst_window)
    think = max(0.0, think - burst_window)  # déduit du think pour ne pas rallonger

    log("DLY", f"Reflexion {label} {think:.1f}s (offline)", f"user={user_id}")

    # Reste offline pendant toute la réflexion
    await asyncio.sleep(think)

    # ── Passe online (elle ouvre l'appli) ──
    await go_online()

    # ── Marque les messages comme lus (double tick bleu) ──
    if chat_id:
        try:
            await bot.send_read_acknowledge(chat_id)
        except Exception:
            pass

    # ── Temps de lecture online — proportionnel à la longueur du message ──
    # Court pour les messages courts, plus long pour les messages longs
    if words_in <= 2:
        read_online = random.uniform(0.4, 1.2)        # "salut", "ok", "да" → lecture rapide
    elif words_in <= 6:
        read_online = random.uniform(0.8, 2.0)        # phrase courte
    elif words_in <= 15:
        read_online = random.uniform(1.5, 3.5)        # message moyen
    else:
        read_online = random.uniform(2.5, 5.0)        # long message
    log("DLY", f"Lecture en ligne {read_online:.1f}s", f"user={user_id}")
    await asyncio.sleep(read_online)

    # → Retour au handler qui lance le typing

# ─── Bot Telegram ─────────────────────────────────────────────

bot = TelegramClient(SESSION, API_ID, API_HASH, catch_up=True)

# Verrou par utilisateur — une seule réponse en vol à la fois
user_locks: dict[str, bool] = {}
# Etat en ligne courant du bot (True = online visible)
_bot_is_online: bool = False
# Timestamp du dernier message entrant par utilisateur (message courant)
last_message_time: dict[str, float] = {}
# Timestamp du message précédent — utilisé pour calculer rapid_exchange correctement
_prev_message_time: dict[str, float] = {}
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

# ── Co-writing : détecte quand l'opérateur tape un draft ───────
# maps chat_id (int) → timestamp du dernier draft reçu
_co_writing: dict[int, float] = {}
_CO_WRITE_TIMEOUT = 30.0  # secondes sans draft event → verrou levé automatiquement

# ── Pause manuelle : quand l'opérateur envoie un message manuellement ──
# maps user_id (str) → timestamp de reprise (time.time() + durée)
_manual_pause: dict[str, float] = {}
_MANUAL_PAUSE_SECONDS = 300.0  # 5 minutes par message envoyé
_MANUAL_PAUSE_FILE = Path(__file__).parent / "manual_pause.json"

# ── Verrou closer : // dans une conv → bot bloqué jusqu'au prochain // ──
_closer_lock: set[str] = set()
_CLOSER_LOCK_FILE = Path(__file__).parent / "closer_lock.json"

def _save_closer_lock():
    try:
        _CLOSER_LOCK_FILE.write_text(json.dumps(list(_closer_lock)), encoding="utf-8")
    except Exception:
        pass

def _load_closer_lock():
    global _closer_lock
    try:
        if _CLOSER_LOCK_FILE.exists():
            _closer_lock = set(json.loads(_CLOSER_LOCK_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass

def _save_manual_pause():
    """Persiste _manual_pause sur disque (fichier JSON)."""
    try:
        now = _time.time()
        active = {uid: ts for uid, ts in _manual_pause.items() if ts > now}
        _MANUAL_PAUSE_FILE.write_text(json.dumps(active), encoding="utf-8")
    except Exception:
        pass

def _load_manual_pause():
    """Recharge _manual_pause depuis le disque en filtrant les entrées expirées."""
    global _manual_pause
    try:
        if _MANUAL_PAUSE_FILE.exists():
            data = json.loads(_MANUAL_PAUSE_FILE.read_text(encoding="utf-8"))
            now = _time.time()
            _manual_pause = {uid: ts for uid, ts in data.items() if ts > now}
    except Exception:
        pass

def _manual_pause_active(user_id: str) -> bool:
    """True si l'opérateur a récemment écrit manuellement dans cette conv (pause bot)."""
    resume = _manual_pause.get(user_id)
    if resume is None:
        return False
    if _time.time() >= resume:
        _manual_pause.pop(user_id, None)
        _save_manual_pause()
        return False
    return True


def _co_writing_active(chat_id: int) -> bool:
    """True si l'opérateur est en train de rédiger pour ce chat (draft < 30s)."""
    t = _co_writing.get(chat_id)
    if t is None:
        return False
    if _time.time() - t > _CO_WRITE_TIMEOUT:
        _co_writing.pop(chat_id, None)
        return False
    return True


async def _co_write_suggest(chat_id: int, draft_text: str):
    """Génère une complétion Haiku et l'affiche dans le terminal (jamais dans le chat)."""
    if not draft_text.strip():
        return
    user_id = str(chat_id)
    hist = histories.get(user_id, [])[-10:]
    ctx_lines = []
    for m in hist:
        role = "Katia" if m.get("role") == "assistant" else "User"
        ctx_lines.append(f"{role}: {m.get('content', '')[:120]}")
    ctx = "\n".join(ctx_lines) or "(début de conversation)"
    prompt = (
        f"Contexte de la conv :\n{ctx}\n\n"
        f"L'opérateur vient d'écrire le draft suivant : \"{draft_text}\"\n\n"
        "Continue ou complète ce draft pour Katia (minuscules, naturel, concis, 1 phrase max). "
        "Réponds UNIQUEMENT avec le texte de complétion, sans guillemets, sans explication."
    )
    def _call_haiku():
        return client_anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
    try:
        resp = await asyncio.to_thread(_call_haiku)
        suggestion = resp.content[0].text.strip().lower()
        print(f"\n✍️  SUGGESTION : {draft_text}  →  {suggestion}\n", flush=True)
    except Exception as e:
        print(f"\n✍️  CO-WRITE ERR : {e}\n", flush=True)


@bot.on(events.Raw(UpdateDraftMessage))
async def handle_draft(event):
    """Détecte les drafts de l'opérateur pour suspendre les réponses auto."""
    from telethon.tl.types import DraftMessage, PeerUser
    peer = getattr(event, "peer", None)
    if not isinstance(peer, PeerUser):
        return
    chat_id = peer.user_id
    draft = getattr(event, "draft", None)
    if draft is None:
        return
    draft_text = getattr(draft, "message", "") or ""
    if not draft_text:
        # Draft effacé → message envoyé ou annulé → déverrouille
        if chat_id in _co_writing:
            _co_writing.pop(chat_id, None)
            log("CWR", f"Draft effacé — reprise auto-réponses", f"chat={chat_id}")
        return
    first_draft = chat_id not in _co_writing
    _co_writing[chat_id] = _time.time()
    if first_draft:
        log("CWR", f"Draft détecté — auto-réponses suspendues", f"chat={chat_id}")
    # Suggestion Haiku non-bloquante
    asyncio.create_task(_co_write_suggest(chat_id, draft_text))


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

    # ── Commandes closer depuis Saved Messages (conv à soi-même) ──
    # //nom  → bot suspendu pour cet utilisateur
    # ///nom → bot reprend pour cet utilisateur
    if MY_ID and chat.id == MY_ID:
        m = re.match(r'^(///)(.+)$', text) or re.match(r'^(//)(.+)$', text)
        if m:
            try:
                await bot.delete_messages(chat.id, [event.message.id])
            except Exception:
                pass
            unlock = m.group(1) == "///"
            target_name = m.group(2).strip().lstrip("@")
            target_id = None
            try:
                entity = await bot.get_entity(target_name)
                target_id = str(entity.id)
            except Exception:
                # Fallback : cherche dans les dialogues par prénom/nom
                async for dialog in bot.iter_dialogs():
                    peer = dialog.entity
                    full = " ".join(filter(None, [
                        getattr(peer, "first_name", None),
                        getattr(peer, "last_name", None),
                        getattr(peer, "username", None),
                    ])).lower()
                    if target_name.lower() in full:
                        target_id = str(peer.id)
                        break
            if target_id:
                if unlock:
                    _closer_lock.discard(target_id)
                    _save_closer_lock()
                    log("CLO", f"Verrou closer DÉSACTIVÉ — bot reprend", f"user={target_id} ({target_name})")
                else:
                    _closer_lock.add(target_id)
                    _save_closer_lock()
                    _manual_pause.pop(target_id, None)
                    _save_manual_pause()
                    log("CLO", f"Verrou closer ACTIVÉ — bot suspendu", f"user={target_id} ({target_name})")
            else:
                log("CLO", f"Utilisateur introuvable", f"nom={target_name}")
        return  # ne pas traiter les messages Saved Messages dans l'historique

    if user_id not in histories:
        histories[user_id] = []
    histories[user_id].append({"role": "assistant", "content": text})
    await save_histories_async()
    # Failsafe : efface le verrou co-writing si l'opérateur vient d'envoyer
    _co_writing.pop(chat.id, None)
    log("MAN", f"Message manuel enregistre ({user_id})", f'"{text}"')
    # Pause bot 5 min pour ce user (chaque message ajoute 5 min depuis maintenant)
    existing_resume = _manual_pause.get(user_id)
    now = _time.time()
    if existing_resume and existing_resume > now:
        new_resume = existing_resume + _MANUAL_PAUSE_SECONDS
    else:
        new_resume = now + _MANUAL_PAUSE_SECONDS
    _manual_pause[user_id] = new_resume
    _save_manual_pause()
    remaining = int((new_resume - now) / 60)
    log("MAN", f"Bot en pause {remaining} min pour ({user_id})", f"reprise a {_time.strftime('%H:%M:%S', _time.localtime(new_resume))}")

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

    if user_id in _closer_lock:
        username_tmp = getattr(sender, "username", None) or f"id:{sender.id}"
        log("CLO", f"Verrou closer actif — ignoré", f"user={username_tmp}")
        return

    if _manual_pause_active(user_id):
        resume = _manual_pause.get(user_id, 0)
        remaining = max(0, int((resume - _time.time()) / 60))
        username_tmp = getattr(sender, "username", None) or f"id:{sender.id}"
        log("MAN", f"Bot en pause — ignoré ({remaining} min restantes)", f"user={username_tmp}")
        return

    username = getattr(sender, "username", None) or f"id:{sender.id}"
    name     = getattr(sender, "first_name", "") or username
    text     = (event.message.message or "").strip()

    # ── Co-writing : si l'opérateur rédige un draft pour ce chat, suspendre l'auto-réponse ──
    if _co_writing_active(sender.id):
        if user_id not in histories:
            histories[user_id] = []
        histories[user_id].append({"role": "user", "content": text or "[media]"})
        await save_histories_async()
        log("CWR", f"Draft actif — message mis en file (bot suspendu)", f"user={username} | \"{text[:60]}\"")
        return

    # Gestion des messages sans texte
    if not text:
        if event.message.voice:
            log("VOX", f"Vocal recu de {name} (@{username}) — transcription...")
            transcribed = await transcribe_voice(event.message)
            if transcribed:
                text = f"[голосовое:] {transcribed}"
                log("VOX", f"Transcrit", f'"{transcribed}"')
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

    # ── Analyse photo de profil (une seule fois par user, en arrière-plan) ──
    if user_id not in _user_profile_desc:
        asyncio.create_task(_fetch_profile_desc(bot, user_id, username))

    # Sauvegarde l'ancien timestamp AVANT d'écraser — permet à human_think_delay
    # de calculer rapid_exchange sur l'intervalle réel entre messages
    _prev_message_time[user_id] = last_message_time.get(user_id, 0)
    last_message_time[user_id] = _time.time()

    # Track response gap (pour engagement score)
    _last_bot_sent = _last_sent_to.get(user_id, 0)
    if _last_bot_sent > 0:
        _gap = _time.time() - _last_bot_sent
        if _gap < 3600:  # ignore les gaps > 1h (user était absent)
            _user_response_gaps.setdefault(user_id, []).append(_gap)
            _user_response_gaps[user_id] = _user_response_gaps[user_id][-5:]

    # Si une réponse est déjà en cours, accumule dans l'historique
    # NE PAS go_offline : elle est peut-être en train de taper — on ne coupe pas sa connexion
    if user_locks.get(user_id):
        if user_id not in histories:
            histories[user_id] = []
        histories[user_id].append({"role": "user", "content": text})
        await save_histories_async()
        # Si elle est déjà en ligne ou en train de taper → marque comme lu immédiatement
        if _bot_is_online:
            async def _ack():
                try:
                    await bot.send_read_acknowledge(event.chat_id)
                except Exception:
                    pass
            asyncio.create_task(_ack())
        log("SKP", f"Message accumule (reponse en cours)", f"user={username}")
        return

    # Aucune réponse en cours → repasse offline (Telethon met online automatiquement)
    asyncio.create_task(go_offline())

    # Sync historique réel depuis Telegram — source de vérité
    # Capture les messages supprimés (absents), les messages manuels opérateur, les modifications
    real_hist = await _fetch_tg_history(sender.id, text)
    if real_hist is not None:
        histories[user_id] = real_hist

    user_locks[user_id] = True
    skip_think = False  # True quand on répond directement (message reçu pendant typing)
    _response_count = 0  # Nombre de réponses envoyées dans ce cycle — skip cooldown si > 0
    try:
        while True:
            # ── Phase 1 : attente de réflexion (AVANT génération) ──
            if skip_think:
                skip_think = False  # réinitialise pour les tours suivants
            else:
                turn_estimate = len(histories.get(user_id, [])) // 2 + 1
                await human_think_delay(text, user_id, turn=turn_estimate, chat_id=event.chat_id)

            # ── Collecte des messages arrivés pendant l'attente ──
            # Petit yield pour que Telethon vide son buffer réseau avant le pop
            await asyncio.sleep(0.2)
            hist_now = histories.get(user_id, [])
            pending_during_wait = []
            while hist_now and hist_now[-1]["role"] == "user":
                pending_during_wait.insert(0, hist_now.pop()["content"])
            if pending_during_wait:
                log("ACC", f"{len(pending_during_wait)} msg(s) integres avant generation",
                    f"user={username}")
                text = text + "\n" + "\n".join(pending_during_wait)

            # ── Phase 2 : génération réponse (synchrone — bloque l'event loop) ──
            # IMPORTANT : on NE PAS utiliser run_in_executor ici.
            # run_in_executor libère l'event loop → les nouveaux messages arrivent et se coincent
            # au milieu de l'historique (avant la réponse assistant), pas à la fin.
            # Le check REGEN ne les voit pas → messages ignorés.
            # Synchrone = event loop bloqué → messages mis en buffer réseau → traités APRÈS
            # → ils arrivent à la FIN de l'historique → REGEN fonctionne correctement.
            hist_len_before = len(histories.get(user_id, []))
            # Envoie l'indicateur typing one-shot avant l'appel bloquant
            # (le context manager ne peut pas être maintenu pendant un appel synchrone)
            try:
                from telethon.tl.functions.messages import SetTypingRequest
                from telethon.tl.types import SendMessageTypingAction
                await bot(SetTypingRequest(peer=event.chat_id, action=SendMessageTypingAction()))
            except Exception:
                pass
            reply = get_eva_response(user_id, username, text)

            # Yield post-API : laisse Telethon livrer les messages mis en buffer pendant l'appel bloquant
            await asyncio.sleep(0.2)
            _regen_hist = histories.get(user_id, [])
            _regen_pending = []
            while _regen_hist and _regen_hist[-1]["role"] == "user":
                _regen_pending.insert(0, _regen_hist.pop()["content"])
            if _regen_pending:
                # Messages arrivés pendant la génération — rollback + régénère avec contexte complet
                log("ACC",
                    f"{len(_regen_pending)} msg(s) recu(s) pendant generation — REGEN",
                    f"user={username} | \"{_regen_pending[0][:60]}\"")
                histories[user_id] = histories[user_id][:hist_len_before]
                text = text + "\n" + "\n".join(_regen_pending)
                reply = get_eva_response(user_id, username, text)

            turn  = len(histories[user_id]) // 2

            # ── Génération contexte résumé (feature A) — toutes les 2 réponses dès le tour 4 ──
            if turn >= 4 and turn % 2 == 0:
                asyncio.create_task(_generate_context_summary(user_id, username))

            # ── Validation cohérence (feature B) — parallel avec typing delay, log-only ──
            _coherence_task = asyncio.create_task(_validate_reply_coherence(text, reply))

            # ── Cooldown anti-burst : délai minimum entre deux envois au même user ──
            # Exception : si on répond à des messages accumulés (2ème+ réponse du cycle),
            # on enchaîne naturellement en 3-6s — pas 45s comme un humain qui répond à 2 msgs
            since_last = _time.time() - _last_sent_to.get(user_id, 0)
            if _response_count == 0 and since_last < SEND_COOLDOWN_MIN:
                extra_wait = SEND_COOLDOWN_MIN - since_last
                log("DLY", f"Cooldown anti-burst {extra_wait:.0f}s", f"user={username}")
                await asyncio.sleep(extra_wait)
            elif _response_count > 0:
                # Enchaînement naturel entre deux réponses consécutives
                await asyncio.sleep(random.uniform(3.0, 6.0))

            # ── Politesse de frappe : si l'user vient d'écrire (< 18s), attendre qu'il finisse ──
            # Évite d'envoyer un 2ème message pendant qu'il est en train de taper sa réponse.
            _since_last_msg = _time.time() - last_message_time.get(user_id, 0)
            if _since_last_msg < 18.0 and _last_sent_to.get(user_id, 0) > 0:
                _typing_wait = 18.0 - _since_last_msg + random.uniform(2.0, 6.0)
                log("DLY", f"Politesse frappe {_typing_wait:.1f}s (user actif recemment)", f"user={username}")
                await asyncio.sleep(_typing_wait)

            # ── Vidéo d'Eva si demandée (avant vocal/texte) ──
            if user_wants_video(text):
                _vid_sent = await send_eva_video(bot, event.chat_id, user_id)
                if _vid_sent:
                    # Remplacer la réponse LLM (générée sans savoir qu'une vidéo partait)
                    _vid_replies = ["тут", "держи", "на", "вот", "смотри"]
                    if _user_lang.get(user_id) == "fr":
                        _vid_replies = ["tiens", "voilà", "regarde", "là"]
                    reply = random.choice(_vid_replies)
                await asyncio.sleep(random.uniform(1.5, 3.0))

            # ── Normalise le lien London Eye (toujours https://the-londoneye.com/tickets) ──
            # Le tracking webhook est désactivé — jamais envoyer l'URL ngrok à un user.
            if "the-londoneye.com" in reply:
                reply = re.sub(
                    r"https?://(?:www\.)?the-londoneye\.com(?:/[^\s]*)?(?:\?[^\s]*)?",
                    "https://the-londoneye.com/tickets",
                    reply,
                )

            # ── Vocal spontané d'accompagnement (avant texte, 40% de chance) ──
            # ── Collecte résultat validation cohérence (log-only, jamais bloquant) ──
            try:
                _coherence_ok = await asyncio.wait_for(_coherence_task, timeout=3.0)
                if not _coherence_ok:
                    log("COH", f"Reponse potentiellement incoherente (log-only)", f"user={username} | msg=\"{text[:60]}\" | reply=\"{reply[:60]}\"")
            except Exception:
                pass

            _reply_has_link = "the-londoneye.com" in reply
            if not _reply_has_link:
                _spont = pick_spontaneous_voice(reply, text, user_id, turn)
                if _spont:
                    log("VOX", f"Vocal spontané", f"user={username} | {_spont.get('transcript','')[:40]}")
                    await send_voice(bot, event.chat_id, user_id, name, username, entry=_spont)
                    await asyncio.sleep(random.uniform(0.8, 2.0))

            # ── Décision : vocal ou texte ? ──
            # Jamais au tour 1 (trop tôt, risque de faux positif)
            # Jamais si le message contient le lien London Eye
            voice_entry = None if (_reply_has_link or turn <= 1) else pick_voice_for_user(reply, text, user_id)

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
                # ── Envoi texte classique — avec découpage multi-messages si long ──
                # Si le lien London Eye est dans la réponse, l'envoyer SEUL pour activer la prévisualisation
                if _reply_has_link:
                    _le_url = "https://the-londoneye.com/tickets"
                    _text_sans_lien = re.sub(
                        r"https?://(?:www\.)?the-londoneye\.com(?:/[^\s]*)?",
                        "", reply
                    ).strip().rstrip(':').rstrip('.').strip()
                    # Nettoie ||| — jamais visible pour l'utilisateur
                    _text_sans_lien = _text_sans_lien.replace("|||", " ").strip()
                    parts = ([_fix_reply(_text_sans_lien)] if _text_sans_lien else []) + [_le_url]
                else:
                    parts = _split_message(reply)
                _link_actually_sent = False
                for i, part in enumerate(parts):
                    part_dur = _typing_delay(part)
                    async with bot.action(event.chat_id, "typing"):
                        await asyncio.sleep(part_dur)
                    _bot_sent_mark(event.chat_id, part)
                    await event.respond(part)
                    log("OUT", f"ENVOYE a {name} (@{username})", f'"{part}"')
                    if "the-londoneye.com" in part:
                        _link_actually_sent = True
                    if i < len(parts) - 1:
                        # Si le lien est dans une partie suivante → ne JAMAIS interrompre
                        _next_has_link = any("the-londoneye.com" in p for p in parts[i+1:])
                        if not _next_has_link:
                            hist_now = histories.get(user_id, [])
                            if hist_now and hist_now[-1]["role"] == "user":
                                log("SKP", f"Split interrompu — nouveaux msgs en attente", f"user={username}")
                                # Retire le lien de l'historique si jamais envoyé
                                if _reply_has_link and not _link_actually_sent:
                                    hist = histories.get(user_id, [])
                                    if hist and hist[-1]["role"] == "assistant":
                                        hist[-1]["content"] = re.sub(
                                            r"https?://(?:www\.)?the-londoneye\.com(?:/[^\s]*)?",
                                            "", hist[-1]["content"]
                                        ).strip()
                                        log("LNK", f"Lien retiré de l'historique (non livré)", f"user={username}")
                                break
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                await save_histories_async()

                # ── Lien London Eye vient d'être envoyé ──
                if _reply_has_link and _link_actually_sent:
                    # Vocal de confirmation après le lien (договорились / хорошо / до завтра)
                    _confirm_entry = pick_voice_for_user("договорились до завтра хорошо", "", user_id)
                    if _confirm_entry:
                        await asyncio.sleep(random.uniform(1.5, 3.5))
                        await send_voice(bot, event.chat_id, user_id, name, username, entry=_confirm_entry)
                        log("CNV", f"Vocal confirmation après lien", f"user={username} | {_confirm_entry.get('transcript','')}")
                    # ── Ticket bought : Eva revient ~2 min plus tard et dit qu'elle a pris son billet ──
                    if user_id not in _ticket_bought_sent:
                        _current_lang = _user_lang.get(user_id, "ru")
                        asyncio.create_task(_send_ticket_bought(
                            bot, user_id, event.chat_id, name, username, _current_lang
                        ))
                        log("CNV", f"Ticket-bought programme (2 min)", f"user={username}")
                    # Follow-up automatique désactivé — closer gère la conversion
                    pass

            # ── Vidéo proactive — tour 4-6, une seule fois, moment de rapport ──
            # Envoie le krug spontanément si la conv est chaude mais avant le pitch LE
            _should_send_proactive_video = (
                4 <= turn <= 6
                and user_id not in _video_sent_users
                and not _reply_has_link
                and not user_wants_video(text or "")
                and len(text or "") > 8   # message substantiel, pas juste "ok"
            )
            if _should_send_proactive_video:
                await asyncio.sleep(random.uniform(2.0, 4.0))
                _vid_proactive = await send_eva_video(bot, event.chat_id, user_id)
                if _vid_proactive:
                    log("VID", f"Vidéo proactive envoyée (tour {turn})", f"user={username}")

            _last_sent_to[user_id] = _time.time()
            _response_count += 1
            log("---", "-" * 55)

            # ── Check messages accumulés pendant l'ENVOI ──
            hist = histories.get(user_id, [])
            pending = []
            while hist and hist[-1]["role"] == "user":
                pending.insert(0, hist.pop()["content"])

            if pending:
                # Des messages sont arrivés pendant qu'elle tapait → reste online, répond direct
                text = "\n".join(pending)
                await save_histories_async()
                log("ACC",
                    f"{len(pending)} msg(s) recu(s) pendant typing — rep direct",
                    f"user={username} | \"{text[:60]}\"")
                # Marque comme lus immédiatement (déjà online)
                try:
                    await bot.send_read_acknowledge(event.chat_id)
                except Exception:
                    pass
                # Pause lecture courte (elle est déjà online, juste lit le message)
                read_dur = max(1.0, len(text.split()) * 0.35) + random.uniform(0.3, 1.2)
                await asyncio.sleep(read_dur)
                skip_think = True  # saute le think_delay au prochain tour
                continue
            else:
                # Aucun message en attente — repasse offline après un délai naturel
                asyncio.create_task(_delayed_offline(random.uniform(30.0, 180.0)))
                break

    except Exception as e:
        log("ERR", f"Erreur", str(e))
        log("ERR", traceback.format_exc())
    finally:
        user_locks[user_id] = False

# ─── LeoMatchBot automation ───────────────────────────────────

LEO_BOT_ID = 1234060895
LEO_LOG_FILE = "leo_conversation.jsonl"

_leo_lock = None          # asyncio.Lock() créé dans main()
_leo_start_time: float = 0.0  # défini dans main() après connexion — ignore les catch_up avant

_leo_likes_this_hour: list[float] = []
_LEO_RATE_LIMIT = 120            # sécurité taux horaire absolu
_leo_last_like_time: float = 0.0
_leo_last_action_time: float = 0.0  # timestamp de la dernière action envoyée à Leo
_leo_pause_until: float = 0.0    # chargé depuis leo_pause.json au démarrage

# ── Session burst (30 likes en 6-12s, puis pause 30 min) ──
_leo_session_likes: int = 0
_leo_session_max: int = 30
_leo_session_pause_secs: float = 1800.0  # 30 min

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
    """Vérifie si un like/dislike peut être envoyé (session + sécurité horaire)."""
    global _leo_last_like_time
    now = _time.time()
    _leo_likes_this_hour[:] = [t for t in _leo_likes_this_hour if now - t < 3600]
    if len(_leo_likes_this_hour) >= _LEO_RATE_LIMIT:
        return False
    if now - _leo_last_like_time < 5:   # intervalle minimum 5s
        return False
    if _leo_session_likes >= _leo_session_max:
        return False
    return True

def _leo_pick_action() -> str:
    """90% like ❤️, 10% dislike 💔."""
    return "💔" if random.random() < 0.10 else "❤️"

def _leo_session_end():
    """Fin de session — déclenche la pause 30 min et réinitialise le compteur."""
    global _leo_pause_until, _leo_session_likes
    _leo_pause_until = _time.time() + _leo_session_pause_secs
    _leo_session_likes = 0
    log("LEO", f"Session terminée ({_leo_session_max} actions) — pause 30 min")
    try:
        with open("leo_pause.json", "w") as _pf:
            json.dump({"until": _leo_pause_until}, _pf)
    except Exception:
        pass

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
    Demande à Claude Haiku quelle action prendre selon l'état de LeoMatchBot.
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
        resp = client_anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        action = resp.content[0].text.strip()
        if action.lower() == "rien":
            return None
        for btn in btns:
            if action.strip() == btn.strip():
                return btn
        return None
    except Exception as e:
        log("LEO", f"Claude decide erreur: {e}")
        return None

@bot.on(events.NewMessage(chats=LEO_BOT_ID, incoming=True))
async def handle_leobot(event):
    """
    Handler LeoMatchBot — une seule action à la fois via _leo_lock.
    Ignore les messages antérieurs au démarrage (catch_up).
    """
    if not LEO_ENABLED:
        return
    global _leo_last_like_time, _leo_last_action_time, _leo_pause_until, _leo_session_likes

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
        await asyncio.sleep(random.uniform(4.0, 9.0))   # lecture humaine 4-9s

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
            if not _leo_is_london(text):
                log("LEO", f"Skip like (hors Londres)", f"profil={name}")
                return
            if _leo_can_like():
                action = _leo_pick_action()
                _leo_last_like_time = _time.time()
                _leo_likes_this_hour.append(_leo_last_like_time)
                _leo_session_likes += 1
                await bot.send_message(LEO_BOT_ID, action)
                _leo_log("out", action, [])
                _leo_last_action_time = _time.time()
                label = "Dislike" if action == "💔" else "Like"
                log("LEO", f"{label} envoyé ({_leo_session_likes}/{_leo_session_max})", f"profil={name}")
                if _leo_session_likes >= _leo_session_max:
                    _leo_session_end()
            else:
                log("LEO", f"Skip — session {_leo_session_likes}/{_leo_session_max}")
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


async def _click_no_purchase_check(bot_client: TelegramClient):
    """
    Toutes les 5 min : détecte les users qui ont cliqué le lien sans acheter.
    Entre 10 et 30 min après le clic → envoie un message de relance immédiate.
    """
    await asyncio.sleep(120)  # délai initial 2 min
    _TRK = Path("tracking.json")
    while True:
        try:
            if _TRK.exists():
                trk = json.loads(_TRK.read_text(encoding="utf-8"))
                now = _time.time()
                CLICK_MIN, CLICK_MAX = 600, 1800  # 10 min → 30 min
                changed = False

                for user_id, events in trk.items():
                    if user_id in BLOCKED_USERS:
                        continue
                    if events.get("purchase"):
                        continue
                    if not events.get("click"):
                        continue
                    if events.get("click_followup_sent"):
                        continue

                    try:
                        from datetime import datetime as _dt
                        click_ts = _dt.fromisoformat(events["click"]).timestamp()
                    except Exception:
                        continue

                    elapsed = now - click_ts
                    if elapsed < CLICK_MIN or elapsed > CLICK_MAX:
                        continue

                    # Trouver chat_id depuis link_followup.json
                    chat_id = None
                    try:
                        lf = json.loads(_FOLLOWUP_FILE.read_text(encoding="utf-8")) if _FOLLOWUP_FILE.exists() else {}
                        chat_id = lf.get(user_id, {}).get("chat_id")
                    except Exception:
                        pass
                    if not chat_id:
                        continue

                    lang = _infer_lang_from_history(user_id, "ru")
                    msgs = {
                        "ru": "ты на сайте? там быстро берётся",
                        "fr": "t'es sur le site là ? c'est rapide",
                        "en": "you on the site ? it's quick",
                    }
                    msg = msgs.get(lang, msgs["ru"])

                    try:
                        async with bot_client.action(chat_id, "typing"):
                            await asyncio.sleep(random.uniform(1.5, 3.0))
                        await bot_client.send_message(chat_id, msg)
                        if user_id in histories:
                            histories[user_id].append({"role": "assistant", "content": msg})
                        await save_histories_async()
                        trk[user_id]["click_followup_sent"] = datetime.now().isoformat()
                        changed = True
                        log("TRK", f"Click follow-up envoyé", f"user={user_id} | '{msg}'")
                    except Exception as e:
                        log("ERR", f"Click follow-up échoué user={user_id}: {e}")

                if changed:
                    _TRK.write_text(json.dumps(trk, ensure_ascii=False, indent=2), encoding="utf-8")

        except Exception as e:
            log("ERR", f"_click_no_purchase_check: {e}")

        await asyncio.sleep(300)  # check toutes les 5 min


async def _cold_reengagement_check(bot_client: TelegramClient):
    """
    Toutes les 2h : détecte les convos froides (dernier msg sortant > 48h, ≥ 3 tours)
    et envoie un message de re-engagement naturel, 1 fois max par semaine par user.
    """
    await asyncio.sleep(600)  # délai initial 10 min après démarrage
    while True:
        try:
            cold_data: dict = {}
            if _COLD_FILE.exists():
                cold_data = json.loads(_COLD_FILE.read_text(encoding="utf-8"))

            now = _time.time()
            ONE_WEEK = 7 * 86400
            MIN_TURNS = 3       # ignorer les convos d'un seul échange
            COLD_AFTER = 48 * 3600  # 48h sans réponse = froid

            async for dialog in bot_client.iter_dialogs():
                if not dialog.is_user:
                    continue
                peer = dialog.entity
                user_id = str(peer.id)

                if peer.id == MY_ID or user_id == str(LEO_BOT_ID):
                    continue
                if peer.id == 777000 or getattr(peer, "bot", False):
                    continue
                if user_id in BLOCKED_USERS:
                    continue

                hist = histories.get(user_id, [])
                if len(hist) < MIN_TURNS * 2:
                    continue  # conversation trop courte
                if hist[-1].get("role") != "assistant":
                    continue  # dernier message est de l'user → pas froid

                # Vérifier si déjà re-engagé cette semaine
                entry = cold_data.get(user_id, {})
                last_cold_sent = entry.get("sent_at", 0)
                if now - last_cold_sent < ONE_WEEK:
                    continue

                # Ne pas re-engager si link_followup gère déjà cet user
                try:
                    link_data: dict = {}
                    if _FOLLOWUP_FILE.exists():
                        link_data = json.loads(_FOLLOWUP_FILE.read_text(encoding="utf-8"))
                    if user_id in link_data:
                        continue
                except Exception:
                    pass

                # Récupérer le dernier message sortant réel (timestamp Telegram)
                try:
                    msgs = await bot_client.get_messages(peer, limit=10)
                    last_out = next((m for m in msgs if m.out), None)
                    if not last_out:
                        continue
                    age = now - last_out.date.timestamp()
                    if age < COLD_AFTER:
                        continue
                except Exception:
                    continue

                # S'assurer que l'user n'a pas répondu depuis
                last_in = next((m for m in msgs if not m.out), None)
                if last_in and last_in.date.timestamp() > last_out.date.timestamp():
                    continue  # l'user a répondu — pas froid

                lang = _infer_lang_from_history(user_id, "ru")
                msg = random.choice(_COLD_MSGS.get(lang, _COLD_MSGS["ru"]))
                username = getattr(peer, "username", None) or f"id:{peer.id}"
                name     = getattr(peer, "first_name", "") or username

                try:
                    async with bot_client.action(peer.id, "typing"):
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                    await bot_client.send_message(peer.id, msg)
                    if user_id in histories:
                        histories[user_id].append({"role": "assistant", "content": msg})
                    await save_histories_async()
                    cold_data[user_id] = {"sent_at": now, "msg": msg}
                    _COLD_FILE.write_text(
                        json.dumps(cold_data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    log("CLD", f"Re-engagement froid envoyé", f"user={username} | '{msg}'")
                    await asyncio.sleep(random.uniform(30.0, 90.0))  # pause entre envois
                except Exception as e:
                    log("ERR", f"Cold re-engagement échoué pour {username}: {e}")

        except Exception as e:
            log("ERR", f"_cold_reengagement_check: {e}")

        await asyncio.sleep(7200)  # 2h entre chaque scan


_LAST_SEEN_PATH = Path(__file__).parent / "last_seen.json"

def _save_last_seen():
    """Sauvegarde le timestamp d'arrêt du bot pour le recovery au redémarrage."""
    try:
        _LAST_SEEN_PATH.write_text(json.dumps({"ts": _time.time()}), encoding="utf-8")
    except Exception:
        pass


async def _extract_msg_text(msg) -> str:
    """Extrait le texte d'un message Telegram (texte, vocal, sticker, photo, vidéo)."""
    if msg.text:
        return msg.text.strip()
    if getattr(msg, "voice", None):
        transcribed = await transcribe_voice(msg)
        return f"[голосовое:] {transcribed}" if transcribed else "[голосовое сообщение]"
    if getattr(msg, "sticker", None):
        return f"[стикер {getattr(msg.sticker, 'alt', '') or ''}]".strip()
    if getattr(msg, "photo", None):
        return await describe_photo(msg)
    if getattr(msg, "gif", None) or getattr(msg, "video", None):
        return "[видео/гиф]"
    return ""


async def recover_since_shutdown():
    """
    Au démarrage, récupère TOUS les messages reçus pendant la déconnexion,
    les injecte dans l'historique dans l'ordre chronologique, puis génère
    une réponse cohérente avec le contexte complet.
    Utilise last_seen.json pour connaître la fenêtre exacte de downtime.
    Chaque user est isolé dans son propre try/except — une erreur n'avorte pas les autres.
    """
    since_ts = _time.time() - 86400  # fallback : 24h si pas de last_seen
    if _LAST_SEEN_PATH.exists():
        try:
            data = json.loads(_LAST_SEEN_PATH.read_text(encoding="utf-8"))
            since_ts = float(data.get("ts", since_ts))
            _LAST_SEEN_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    downtime_min = (_time.time() - since_ts) / 60
    log("RCV", f"Shutdown recovery — deconnexion de {downtime_min:.0f} min")

    await asyncio.sleep(4.0)
    recovered = 0

    # Collecte tous les dialogs — le filtre précis se fait après get_messages()
    pending: list[tuple] = []
    try:
        async for dialog in bot.iter_dialogs():
            if not dialog.is_user:
                continue
            peer = dialog.entity
            user_id = str(peer.id)
            if peer.id == MY_ID or user_id == str(LEO_BOT_ID):
                continue
            if peer.id == 777000 or getattr(peer, "bot", False):
                continue
            if user_id in BLOCKED_USERS:
                continue
            pending.append((peer, user_id))
    except Exception as e:
        log("ERR", f"Shutdown recovery — erreur iter_dialogs: {e}")

    log("RCV", f"Shutdown recovery — {len(pending)} conversations à traiter")

    for peer, user_id in pending:
        # Re-vérifier BLOCKED_USERS ici au cas où il aurait été mis à jour
        if user_id in BLOCKED_USERS:
            continue
        if user_id in _closer_lock:
            continue

        username = getattr(peer, "username", None) or f"id:{peer.id}"
        name = getattr(peer, "first_name", "") or username

        try:
            recent = await bot.get_messages(peer, limit=30)
        except Exception as e:
            log("ERR", f"Shutdown recovery — get_messages échoué pour {username}: {e}")
            continue  # passe au user suivant, ne coupe pas la boucle

        missed = [m for m in recent if not m.out and m.date.timestamp() > since_ts]
        if not missed:
            continue

        # Vérifie qu'aucun message sortant n'est déjà postérieur au dernier entrant
        last_in_ts = max(m.date.timestamp() for m in missed)
        already_replied = any(m.out and m.date.timestamp() > last_in_ts for m in recent)
        if already_replied:
            continue

        # Ordre chronologique
        missed = list(reversed(missed))

        log("RCV", f"Shutdown recovery : {len(missed)} msg(s) de {name} (@{username})")

        if user_id not in histories:
            histories[user_id] = []

        try:
            last_text = await _extract_msg_text(missed[-1])
        except Exception as e:
            log("ERR", f"Shutdown recovery — extract text échoué pour {username}: {e}")
            continue

        # Sync historique réel depuis TG — source de vérité complète
        real_hist = await _fetch_tg_history(peer.id, last_text)
        if real_hist is not None:
            histories[user_id] = real_hist
        else:
            # Fallback : injection manuelle des messages manquants
            for msg in missed[:-1]:
                text_m = await _extract_msg_text(msg)
                if not text_m:
                    continue
                recent_contents = {m["content"] for m in histories[user_id][-4:] if isinstance(m, dict) and m.get("role") == "user"}
                if text_m not in recent_contents:
                    histories[user_id].append({"role": "user", "content": text_m})

        if not last_text:
            continue

        # Délai fixe et court — pas multiplicatif pour ne pas rater les derniers users
        await asyncio.sleep(random.uniform(2.0, 5.0))

        if user_locks.get(user_id):
            log("RCV", f"Shutdown recovery — {username} verrouillé, skip")
            continue

        user_locks[user_id] = True
        try:
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(None, get_eva_response, user_id, username, last_text)
            if user_wants_video(last_text) and user_id not in _video_sent_users:
                await send_eva_video(bot, peer.id, user_id)
                await asyncio.sleep(random.uniform(1.5, 3.0))
            async with bot.action(peer.id, "typing"):
                await asyncio.sleep(_typing_delay(reply))
            _bot_sent_mark(peer.id, reply)
            await bot.send_message(peer.id, reply)
            await save_histories_async()
            log("RCV", f"Shutdown recovery -> {name} (@{username})", f'"{reply[:50]}"')
            recovered += 1
        except Exception as e:
            log("ERR", f"Shutdown recovery — envoi échoué pour {username}: {e}")
            hist = histories.get(user_id, [])
            if hist and isinstance(hist[-1], dict) and hist[-1].get("role") == "user" and hist[-1].get("content") == last_text:
                histories[user_id].pop()
                await save_histories_async()
        finally:
            user_locks[user_id] = False

    log("RCV", f"Shutdown recovery terminé — {recovered}/{len(pending)} conversation(s) rattrapée(s)")


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
                text = f"[голосовое:] {transcribed}" if transcribed else "[голосовое сообщение]"
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
                # Sync historique réel depuis TG avant génération (source de vérité)
                if user_id not in histories:
                    histories[user_id] = []
                real_hist = await _fetch_tg_history(peer.id, text)
                if real_hist is not None:
                    histories[user_id] = real_hist

                _rcv_loop = asyncio.get_running_loop()
                reply = await _rcv_loop.run_in_executor(None, get_eva_response, user_id, username, text)
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
                    await save_histories_async()
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

_VIDEO_TRIGGER_PATH = Path(__file__).parent / "video_trigger.json"

async def _check_video_trigger(bot: TelegramClient):
    """Vérifie toutes les 3s si un fichier video_trigger.json existe et envoie la vidéo ou un vocal."""
    while True:
        await asyncio.sleep(3)
        if not _VIDEO_TRIGGER_PATH.exists():
            continue
        try:
            data = json.loads(_VIDEO_TRIGGER_PATH.read_text(encoding="utf-8"))
            target = data.get("target")
            user_id = str(data.get("user_id", target))
            action_type = data.get("type", "video")
            _VIDEO_TRIGGER_PATH.unlink()
            if not target:
                continue
            chat_id = int(user_id) if user_id.lstrip("-").isdigit() else target
            if action_type == "voice":
                filename = data.get("filename")
                from zvukogram_agent import _load_catalog
                entry = next((v for v in _load_catalog() if v.get("filename") == filename), None)
                if entry:
                    log("VOX", f"Trigger fichier detecte — envoi vocal a {target}", f'"{entry.get("transcript","")}"')
                    name = data.get("name", target)
                    username = data.get("username", target)
                    await send_voice(bot, chat_id, user_id, name, username, entry)
                else:
                    log("WRN", f"Trigger vocal : fichier introuvable dans catalogue : {filename}")
            else:
                log("VID", f"Trigger fichier detecte — envoi video a {target}")
                await send_eva_video(bot, chat_id, user_id)
        except Exception as e:
            log("VID", f"Erreur trigger media : {e}")
            try:
                _VIDEO_TRIGGER_PATH.unlink(missing_ok=True)
            except Exception:
                pass

async def _leo_react_to_last():
    """
    Lit le dernier message de LeoMatchBot et réagit de manière cohérente.
    Appelé par le watchdog périodiquement.
    """
    global _leo_last_action_time, _leo_pause_until, _leo_last_like_time, _leo_session_likes
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
                    action = _leo_pick_action()
                    _leo_last_like_time = _time.time()
                    _leo_likes_this_hour.append(_leo_last_like_time)
                    _leo_session_likes += 1
                    await bot.send_message(LEO_BOT_ID, action)
                    _leo_last_action_time = _time.time()
                    label = "Dislike" if action == "💔" else "Like"
                    log("LEO", f"Watchdog: {label} profil photo ({_leo_session_likes}/{_leo_session_max})")
                    if _leo_session_likes >= _leo_session_max:
                        _leo_session_end()
                else:
                    log("LEO", f"Watchdog: skip photo — session {_leo_session_likes}/{_leo_session_max}")
            return
        btns = _get_buttons(last)
        log("LEO", f"Watchdog check dernier msg", f"text={text[:50]} btns={btns[:3]}")

        # ── Détection boucle : même état répété → pause 1h ──
        if _leo_detect_loop(text):
            return

        async with _leo_lock:
            await asyncio.sleep(random.uniform(4.0, 9.0))

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
                if not _leo_is_london(text):
                    log("LEO", f"Watchdog: skip like (hors Londres)", f"profil={name}")
                    return
                if _leo_can_like():
                    action = _leo_pick_action()
                    _leo_last_like_time = _time.time()
                    _leo_likes_this_hour.append(_leo_last_like_time)
                    _leo_session_likes += 1
                    await bot.send_message(LEO_BOT_ID, action)
                    _leo_last_action_time = _time.time()
                    label = "Dislike" if action == "💔" else "Like"
                    log("LEO", f"Watchdog: {label} envoyé ({_leo_session_likes}/{_leo_session_max})", f"profil={name}")
                    if _leo_session_likes >= _leo_session_max:
                        _leo_session_end()
                else:
                    log("LEO", f"Watchdog: skip — session {_leo_session_likes}/{_leo_session_max}")
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
    if not LEO_ENABLED:
        log("LEO", "LeoMatchBot désactivé (LEO_ENABLED=0)")
        return
    global _leo_last_action_time
    await asyncio.sleep(random.uniform(10.0, 15.0))  # délai initial
    while True:
        try:
            if _leo_lock is None:
                await asyncio.sleep(15.0)
                continue
            if _leo_pause_until and _time.time() < _leo_pause_until:
                remaining_s = _leo_pause_until - _time.time()
                if remaining_s < 3600:
                    log("LEO", f"Session en pause — reprise dans {remaining_s/60:.0f} min")
                else:
                    log("LEO", f"Session en pause — reprise dans {remaining_s/3600:.1f}h")
                # Dormir jusqu'à la fin de la pause, max 1h par tranche
                await asyncio.sleep(min(remaining_s, 3600.0))
                continue
            if not _leo_lock.locked():
                await _leo_react_to_last()
        except Exception as e:
            log("LEO", f"Erreur watchdog: {e}")
        await asyncio.sleep(20.0)

# ─── Entrée ───────────────────────────────────────────────────

async def go_online():
    """Passe en ligne brièvement."""
    global _bot_is_online
    _bot_is_online = True
    try:
        await bot(UpdateStatusRequest(offline=False))
    except Exception:
        pass

async def go_offline():
    """Passe hors ligne."""
    global _bot_is_online
    _bot_is_online = False
    try:
        await bot(UpdateStatusRequest(offline=True))
    except Exception:
        pass

# Fix 12 : follow-up persisté sur disque — résiste aux redémarrages
_FOLLOWUP_FILE = Path("link_followup.json")

_FOLLOWUP_MSGS = {
    "first": {
        "ru": ["ну как, идём?", "ты взял билет?", "всё ещё идём завтра?"],
        "fr": ["alors, t'as pris le billet ?", "on y va toujours ?", "tu l'as pris ?"],
        "en": ["hey, got the ticket ?", "still on for tomorrow ?", "you got it ?"],
    },
    "second": {
        "ru": ["ты там?", "пропал куда-то", "ну и ладно"],
        "fr": ["t'es là ?", "tu as disparu ?", "bon"],
        "en": ["hey", "you there ?", "all good ?"],
    },
}

# Messages "j'ai pris mon billet" — envoyés ~2 min après le lien
# Eva revient après s'être "déconnectée" et annonce qu'elle a acheté son ticket
_TICKET_BOUGHT_MSGS = {
    "ru": [
        "взяла свой билет только что",
        "ок взяла себе, ты когда берёшь?",
        "взяла свой, ты берёшь?",
    ],
    "fr": [
        "j'ai pris le mien",
        "j'ai pris mon billet, t'as pris le tien ?",
        "j'ai pris le mien à l'instant, t'as fait pareil ?",
    ],
    "en": [
        "just got mine",
        "just bought mine, did you get yours ?",
        "got mine just now, you ?",
    ],
}

# Suivi des users pour lesquels le message "ticket acheté" a déjà été envoyé
_ticket_bought_sent: set[str] = set()


def _extract_time_slot(user_id: str) -> str | None:
    """Extrait le jour convenu depuis les derniers messages de l'historique.
    Les billets London Eye sont pour toute la journée — pas d'heure à extraire."""
    _DAY_RE = re.compile(
        r'\b(завтра|в субботу|в воскресенье|в пятницу|в четверг|в среду|в понедельник'
        r'|demain|samedi|dimanche|vendredi|jeudi|mercredi|lundi'
        r'|tomorrow|saturday|sunday|friday|thursday|wednesday|monday)\b',
        re.IGNORECASE,
    )
    all_msgs = [m["content"] for m in histories.get(user_id, []) if m.get("role") in ("assistant", "user")][-10:]
    for msg in reversed(all_msgs):
        d = _DAY_RE.search(msg)
        if d:
            return d.group(0)
    return None


async def _send_ticket_bought(bot_client, user_id: str, chat_id: int, name: str, username: str, lang: str):
    """
    Envoyé ~2 minutes après le lien London Eye.
    Eva revient en ligne et dit qu'elle a acheté son billet — crée de la pression sociale.
    Ne s'envoie que si l'user n'a pas encore répondu depuis l'envoi du lien.
    """
    if user_id in BLOCKED_USERS:
        return
    delay = random.uniform(90, 150)   # 1m30 à 2m30
    await asyncio.sleep(delay)

    # Si l'user a répondu entre-temps → inutile, la conv gère ça
    hist = histories.get(user_id, [])
    if hist and hist[-1].get("role") == "user":
        log("CNV", f"Ticket-bought annulé (user a déjà répondu)", f"user={username}")
        return

    # Vérifie que le lien a bien été envoyé (sanity check)
    if not any("the-londoneye.com" in m.get("content", "") for m in hist if m.get("role") == "assistant"):
        return

    # Si la conv est active (2+ échanges depuis le lien) → ne pas interrompre mid-objection
    _link_idx = next(
        (i for i, m in enumerate(hist)
         if m.get("role") == "assistant" and "the-londoneye.com" in m.get("content", "")),
        None
    )
    if _link_idx is not None:
        _post_link_msgs = hist[_link_idx + 1:]
        _post_link_user_count = sum(1 for m in _post_link_msgs if m.get("role") == "user")
        if _post_link_user_count >= 2:
            log("CNV", f"Ticket-bought annulé (conv active, {_post_link_user_count} msgs depuis lien)", f"user={username}")
            return
        # Vérifie aussi si le dernier msg user contient une objection prix/hésitation
        _OBJ_RE = re.compile(
            r"на\s*месте|дорог|дороже|дорого|зачем\s*брать|не\s*надо\s*брать"
            r"|c.est\s+cher|trop\s+cher|sur\s+place|expensive|too\s+much|don.t\s+need",
            re.IGNORECASE,
        )
        _recent_user_msgs = [m["content"] for m in _post_link_msgs if m.get("role") == "user"]
        if any(_OBJ_RE.search(t) for t in _recent_user_msgs):
            log("CNV", f"Ticket-bought annulé (objection active après lien)", f"user={username}")
            return

    # Choisit le message
    lang_key = lang if lang in _TICKET_BOUGHT_MSGS else "ru"
    msgs = _TICKET_BOUGHT_MSGS[lang_key]

    # Si on a extrait une heure précise → personnalise le premier message
    slot = _extract_time_slot(user_id)
    if slot and lang == "ru":
        msg = f"взяла себе на {slot}"
    elif slot and lang == "fr":
        msg = f"j'ai pris le mien pour {slot}"
    elif slot and lang == "en":
        msg = f"just got mine for {slot}"
    else:
        msg = random.choice(msgs)

    try:
        async with bot_client.action(chat_id, "typing"):
            await asyncio.sleep(random.uniform(1.5, 3.0))
        _bot_sent_mark(chat_id, msg)
        await bot_client.send_message(chat_id, msg)
        histories[user_id].append({"role": "assistant", "content": msg})
        await save_histories_async()
        _ticket_bought_sent.add(user_id)
        log("CNV", f"Ticket-bought envoye", f"user={username} | '{msg}'")
    except Exception as e:
        log("ERR", f"Ticket-bought echoue pour {username}: {e}")


def _infer_lang_from_history(user_id: str, default: str = "ru") -> str:
    """Infère la langue depuis l'historique en mémoire — Fix 16 : résiste aux redémarrages."""
    lang = _user_lang.get(user_id)
    if lang:
        return lang
    user_msgs = [m["content"] for m in histories.get(user_id, []) if m.get("role") == "user"]
    for msg in reversed(user_msgs[-5:]):
        detected = _detect_lang(msg)
        if detected in ("fr", "en"):
            return detected
    return default


async def generate_followup_message(user_id: str, username: str, wave: int, lang: str) -> str:
    """
    Génère un message de relance contextualisé via Claude, basé sur l'historique réel
    de la conversation. Aucun template — chaque message est unique et adapté.
    wave=1 : relance douce (2-3h après le lien)
    wave=2 : relance 24h après, plus détachée
    """
    hist = histories.get(user_id, [])

    if wave == 1:
        if lang == "fr":
            instruction = (
                "Tu as envoyé un lien London Eye à cette personne il y a quelques heures. "
                "Elle n'a pas encore répondu. Écris UN message court et naturel qui relance la conversation "
                "de façon fluide — adapté au ton et au contexte de vos échanges précédents. "
                "Ne mentionne pas le lien directement. Pas d'explication. Juste le message."
            )
        elif lang == "en":
            instruction = (
                "You sent this person a London Eye link a few hours ago. "
                "They haven't replied yet. Write ONE short natural message to re-engage them — "
                "matching the tone and context of your previous exchange. "
                "Don't mention the link directly. No explanation. Just the message."
            )
        else:
            instruction = (
                "Ты отправила этому человеку ссылку на London Eye несколько часов назад. "
                "Он/она ещё не ответил(а). Напиши ОДНО короткое и естественное сообщение, "
                "чтобы возобновить разговор — в том же тоне и контексте, что и предыдущие сообщения. "
                "Не упоминай ссылку напрямую. Никаких объяснений. Только сообщение."
            )
    else:
        if lang == "fr":
            instruction = (
                "Cette personne n'a pas répondu depuis 24h. "
                "Écris UN message très court, détaché, naturel — comme si tu pensais à elle en passant. "
                "Adapté au ton de la conversation. Pas de relance agressive. Juste le message."
            )
        elif lang == "en":
            instruction = (
                "This person hasn't replied in 24h. "
                "Write ONE very short, casual, natural message — like you just thought of them. "
                "Match the tone of the conversation. No pressure. Just the message."
            )
        else:
            instruction = (
                "Этот человек не отвечает уже 24 часа. "
                "Напиши ОДНО очень короткое, непринуждённое сообщение — как будто просто вспомнила. "
                "В том же тоне, что и разговор. Без давления. Только сообщение."
            )

    # Construit les messages Anthropic avec l'historique réel + instruction cachée
    anthropic_messages = []
    for m in hist[-16:]:  # 8 derniers échanges max
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            anthropic_messages.append({"role": m["role"], "content": m["content"]})

    # Assure que ça commence par un message user
    if not anthropic_messages or anthropic_messages[0]["role"] != "user":
        anthropic_messages.insert(0, {"role": "user", "content": "[début de conversation]"})

    # Alterne correctement user/assistant si nécessaire
    if anthropic_messages and anthropic_messages[-1]["role"] == "assistant":
        anthropic_messages.append({"role": "user", "content": instruction})
    else:
        anthropic_messages[-1]["content"] += f"\n\n[INSTRUCTION SYSTÈME — ne pas reproduire dans la réponse : {instruction}]"

    try:
        _soul_text = SOUL
        if _own_profile_desc:
            _soul_text = (
                f"[TES PHOTOS DE PROFIL TELEGRAM — ce à quoi tu ressembles réellement : "
                f"{_own_profile_desc}.]\n\n" + SOUL
            )
        response = await asyncio.to_thread(
            client_anthropic.messages.create,
            model="claude-sonnet-4-6",
            max_tokens=80,
            system=[{"type": "text", "text": _soul_text, "cache_control": {"type": "ephemeral"}}],
            messages=anthropic_messages,
        )
        msg = _fix_reply(response.content[0].text.strip())
        log("CNV", f"Follow-up GPT genere (vague {wave})", f"user={username} | '{msg}'")
        return msg
    except Exception as e:
        log("ERR", f"generate_followup_message echoue: {e}")
        # Fallback minimal si GPT échoue
        fallback = {"ru": ["ты там?", "ну как?"], "fr": ["t'es là ?", "alors ?"], "en": ["hey", "still there ?"]}
        return random.choice(fallback.get(lang, fallback["ru"]))


def _followup_schedule(user_id: str, chat_id: int, name: str, username: str):
    """
    Enregistre un follow-up London Eye dans link_followup.json.
    Calcule dès maintenant les timestamps cibles individuels pour chaque vague —
    ainsi même après redémarrage, chaque user a sa propre heure, pas de burst possible.
    """
    try:
        data: dict = {}
        if _FOLLOWUP_FILE.exists():
            data = json.loads(_FOLLOWUP_FILE.read_text(encoding="utf-8"))
        if user_id not in data:   # ne pas écraser un suivi déjà en cours
            now = _time.time()
            data[user_id] = {
                "chat_id":        chat_id,
                "name":           name,
                "username":       username,
                "lang":           _user_lang.get(user_id, "ru"),
                "link_sent_at":   now,
                "first_send_at":  now + random.uniform(7200, 10800),   # 2h–3h, unique par user
                "second_send_at": now + random.uniform(86400, 93600),  # 24h–26h, unique par user
                "first_sent_at":  None,
                "second_sent_at": None,
            }
            _FOLLOWUP_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception as e:
        log("ERR", f"_followup_schedule: {e}")


async def _periodic_followup_check(bot_client: TelegramClient):
    """
    Vérifie toutes les 15 min si des follow-ups London Eye sont en attente.
    Fix 12 : basé sur fichier → résiste aux redémarrages.
    Fix 13 : vagues 1 et 2 indépendantes (try/except séparés).
    Fix 16 : langue reconstituée depuis l'historique si nécessaire.
    """
    while True:
        await asyncio.sleep(900)   # 15 min
        if not _FOLLOWUP_FILE.exists():
            continue
        try:
            data: dict = json.loads(_FOLLOWUP_FILE.read_text(encoding="utf-8"))
        except Exception:
            continue

        now = _time.time()
        changed = False

        for user_id, entry in list(data.items()):
            if user_id in BLOCKED_USERS:
                continue
            if user_id in _closer_lock:
                continue
            hist = histories.get(user_id, [])

            # Si user a répondu → marque les deux vagues pour ne plus les déclencher
            if hist and hist[-1].get("role") == "user":
                if not entry.get("first_sent_at") or not entry.get("second_sent_at"):
                    entry["first_sent_at"]  = entry["first_sent_at"]  or now
                    entry["second_sent_at"] = entry["second_sent_at"] or now
                    changed = True
                continue

            chat_id   = entry.get("chat_id")
            name_     = entry.get("name", "")
            username_ = entry.get("username", "")
            lang      = _infer_lang_from_history(user_id, entry.get("lang", "ru"))

            # Migration : anciens entries sans first_send_at → leur assigner un timestamp futur
            if not entry.get("first_send_at") and not entry.get("first_sent_at"):
                entry["first_send_at"] = entry.get("link_sent_at", now) + random.uniform(300, 900)
                changed = True
            if not entry.get("second_send_at") and not entry.get("second_sent_at"):
                entry["second_send_at"] = entry.get("link_sent_at", now) + random.uniform(86400, 93600)
                changed = True

            # ── Vague 1 : déclenche uniquement à l'heure individuelle prévue ──
            if not entry.get("first_sent_at") and now >= entry.get("first_send_at", float("inf")):
                msg1 = await generate_followup_message(user_id, username_, wave=1, lang=lang)
                try:   # Fix 13 : try/except indépendant
                    async with bot_client.action(chat_id, "typing"):
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                    await bot_client.send_message(chat_id, msg1)
                    if user_id in histories:
                        histories[user_id].append({"role": "assistant", "content": msg1})
                    await save_histories_async()
                    entry["first_sent_at"] = now
                    changed = True
                    log("CNV", f"Follow-up 1 envoye", f"user={username_} | '{msg1}'")
                except Exception as e:
                    log("ERR", f"Follow-up 1 echoue pour {username_}: {e}")
                    if "Could not find the input entity" in str(e):
                        entry["unreachable"] = True
                        changed = True

            if entry.get("unreachable"):
                continue

            # ── Vague 2 : déclenche uniquement à l'heure individuelle prévue ──
            if not entry.get("second_sent_at") and now >= entry.get("second_send_at", float("inf")):
                msg2 = await generate_followup_message(user_id, username_, wave=2, lang=lang)
                try:   # Fix 13 : try/except indépendant de la vague 1
                    async with bot_client.action(chat_id, "typing"):
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                    await bot_client.send_message(chat_id, msg2)
                    if user_id in histories:
                        histories[user_id].append({"role": "assistant", "content": msg2})
                    await save_histories_async()
                    entry["second_sent_at"] = now
                    changed = True
                    sent_this_cycle += 1
                    log("CNV", f"Follow-up 24h envoye", f"user={username_} | '{msg2}'")
                except Exception as e:
                    log("ERR", f"Follow-up 24h echoue pour {username_}: {e}")

        if changed:
            try:
                _FOLLOWUP_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as e:
                log("ERR", f"Sauvegarde link_followup.json echouee: {e}")


async def _delayed_offline(delay: float):
    """Repasse offline après un délai — annulé si un lock est actif."""
    await asyncio.sleep(delay)
    if not any(user_locks.values()):
        await go_offline()

async def keep_offline():
    """Repasse offline toutes les 20s — sauf si une réponse est en cours (typing actif)."""
    while True:
        # Ne pas couper si quelqu'un est en train de recevoir une réponse
        anyone_active = any(user_locks.values())
        if not anyone_active:
            try:
                await go_offline()
            except Exception:
                pass
        await asyncio.sleep(20)


_SHUTDOWN_FILE = Path(__file__).parent / ".shutdown"

async def _shutdown_watcher():
    """Surveille le fichier .shutdown — sauvegarde proprement avant de quitter.
    Ignore les fichiers créés avant le démarrage du bot (évite la race condition au restart)."""
    while True:
        await asyncio.sleep(0.5)
        if _SHUTDOWN_FILE.exists():
            try:
                file_mtime = _SHUTDOWN_FILE.stat().st_mtime
            except Exception:
                continue
            # Ignorer si le fichier existait avant ce démarrage du bot
            if file_mtime < BOT_START_TIME:
                continue
            log("SYS", "Signal .shutdown détecté — sauvegarde et arrêt propre")
            try:
                _SHUTDOWN_FILE.unlink()
            except Exception:
                pass
            save_histories()
            _save_last_seen()
            log("SYS", "Historiques sauvegardés — arrêt.")
            os._exit(0)


async def main():
    load_histories()

    log("BOT", "Demarrage Katia Bot...")
    log("TEL", f"Numero : {PHONE}")

    # ── Webhook tracking (click/purchase) ──
    _start_webhook(port=5055)
    threading.Thread(target=_start_tunnel, daemon=True, name="tunnel-starter").start()

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
    global MY_ID
    MY_ID = me.id  # Auto-détecté au démarrage
    log("OK ", f"Connectee : {me.first_name} (@{me.username})", f"id={me.id}")
    log("LEO", f"LeoMatchBot automation active — rate limit {_LEO_RATE_LIMIT} likes/h")
    log("RDY", "En attente de messages...\n")

    # Analyse les photos de profil d'Katia pour qu'elle sache ce qu'elle "ressemble"
    asyncio.create_task(_analyze_own_profile_photos(bot))

    asyncio.create_task(leo_start_browsing())
    asyncio.create_task(recover_since_shutdown())  # Répond aux messages reçus pendant le downtime
    asyncio.create_task(periodic_recover())
    asyncio.create_task(keep_offline())
    asyncio.create_task(_periodic_followup_check(bot))  # Fix 12 : follow-ups persistants
    asyncio.create_task(_cold_reengagement_check(bot))   # Re-engagement convos froides
    asyncio.create_task(_click_no_purchase_check(bot))  # Click sans achat → relance 10-30 min
    asyncio.create_task(_check_video_trigger(bot))       # Trigger fichier video_trigger.json
    asyncio.create_task(_shutdown_watcher())             # Arrêt gracieux via fichier .shutdown

    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
