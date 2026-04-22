"""
Serveur WhatsApp — reçoit les messages depuis wa_bridge (whatsapp-web.js / Node.js)
et génère des réponses avec Claude (même soul que Telegram, histories séparées).
Port : 5057

Logique identique au bot Telegram :
- Même filtres durs (nom, quartier, prix, heure, emojis, mots bannis)
- Retry loop 4 tentatives
- _fix_reply, _split_message
- Injection Musée Picasso automatique
- Pause manuelle opérateur
- Délais humains calculés ici + renvoyés au bridge Node.js
"""

import json
import os
import re
import certifi
import shutil
import subprocess
import tempfile
import threading
import time
import random
import base64
import io
from pathlib import Path
from flask import Flask, request, jsonify
import anthropic
import httpx
from dotenv import load_dotenv
from soul_manon import MANON_SOUL as SOUL

load_dotenv()

app = Flask(__name__)
app.logger.disabled = True
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Vérifie la disponibilité de ffmpeg au démarrage
_FFMPEG = shutil.which("ffmpeg")
if _FFMPEG:
    print(f"[WA] ffmpeg trouvé : {_FFMPEG}", flush=True)
else:
    print("[WA] ffmpeg introuvable — la vision vidéo sera désactivée. Installe ffmpeg et ajoute-le au PATH.", flush=True)

# ── Paths ──────────────────────────────────────────────────────
BASE = Path(__file__).parent
_WA_HISTORIES_PATH = BASE / "histories_wa.json"
_WA_BLOCKED_PATH   = BASE / "wa_blocked.json"
_WA_CLOSER_PATH    = BASE / "wa_closer_lock.json"
_WA_PAUSE_PATH     = BASE / "wa_manual_pause.json"

# ── State ──────────────────────────────────────────────────────
_wa_histories: dict[str, list]   = {}
_wa_blocked:   set[str]          = set()
_wa_closer:    set[str]          = set()
_wa_pause:     dict[str, float]  = {}   # phone → resume_timestamp
_last_sent_to: dict[str, float]  = {}   # phone → timestamp dernier envoi
_user_lang:    dict[str, str]    = {}
_hist_lock     = threading.Lock()

_WA_PAUSE_SECONDS = 180.0   # 3 min par message opérateur
MAX_HISTORY       = 20

# ── Persistance ────────────────────────────────────────────────
def _sanitize_history(hist: list) -> list:
    """Supprime les incohérences de rôle : deux messages consécutifs du même rôle
    ou un historique qui termine sur 'user' sans réponse (crash en pleine génération)."""
    if not hist:
        return hist
    clean = []
    for m in hist:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        if clean and clean[-1]["role"] == m["role"]:
            # Fusionne plutôt que de garder deux messages user/user ou assistant/assistant
            clean[-1]["content"] = clean[-1]["content"] + " " + m.get("content", "")
        else:
            clean.append(m)
    # Si le dernier message est "user" sans réponse → le retirer (état incohérent post-crash)
    if clean and clean[-1]["role"] == "user":
        clean.pop()
    return clean

def _load_all():
    global _wa_histories, _wa_blocked, _wa_closer, _wa_pause
    try:
        if _WA_HISTORIES_PATH.exists():
            raw = json.loads(_WA_HISTORIES_PATH.read_text(encoding="utf-8"))
            _wa_histories = {phone: _sanitize_history(hist) for phone, hist in raw.items()}
    except Exception:
        pass
    try:
        if _WA_BLOCKED_PATH.exists():
            _wa_blocked = set(json.loads(_WA_BLOCKED_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    try:
        if _WA_CLOSER_PATH.exists():
            _wa_closer = set(json.loads(_WA_CLOSER_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    try:
        if _WA_PAUSE_PATH.exists():
            _wa_pause = json.loads(_WA_PAUSE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass

def _save_histories():
    try:
        tmp = _WA_HISTORIES_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_wa_histories, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_WA_HISTORIES_PATH)
    except Exception:
        pass

def _save_pause():
    try:
        _WA_PAUSE_PATH.write_text(json.dumps(_wa_pause, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

_load_all()

# ══════════════════════════════════════════════════════════════
# FILTRES — miroir exact de telegram_bot.py
# ══════════════════════════════════════════════════════════════

_EMOJI_WHITELIST = {"\U0001f60f", "\U0001f610"}
_FORBIDDEN_EMOJIS_RE = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    r"\u2600-\u26FF\u2700-\u27BF\uFE00-\uFE0F\u200D\u20E3]"
)

def _has_forbidden_emoji(text: str) -> bool:
    return any(_FORBIDDEN_EMOJIS_RE.match(c) and c not in _EMOJI_WHITELIST for c in text)

def _strip_forbidden_emojis(text: str) -> str:
    return "".join(c for c in text if not (_FORBIDDEN_EMOJIS_RE.match(c) and c not in _EMOJI_WHITELIST)).strip()

_BANNED_WORDS = re.compile(
    r"приятно познакомиться"
    r"|\bконечн|\bздоров|\bвосхищ|\bзаряжа|\bвпечатля|\bвдохновля"
    r"|\bпотряс|\bудивительн|\bзамечательн|\bнезабываем"
    r"|\bзвучит как план|\bзвучит\b"
    r"|\bпонятно\b"
    r"|\bинтересн[ыоа]\b"
    r"|расскажи о себе|у тебя есть хобби",
    re.IGNORECASE,
)

_DASH_RE        = re.compile(r" — |—| - ")
_EXCL_RE        = re.compile(r"!")
_PLACEHOLDER_RE = re.compile(
    r"\*+sends?\s*voice\*+|\*+sended?\s*voice\*+|\*+vocal\*+|\[vocal\]|\*voice\*",
    re.IGNORECASE
)
_WRONG_NAME_RE     = re.compile(r"\b[Кк]ат[яи]\b|\bkatia\b|\b[Аа]лин[аы]\b|\balina\b", re.I)
_WRONG_DISTRICT_RE = re.compile(
    r"\b16e?\s+arr|\b8e?\s+arr|\bNeuilly\b|\bPassy\b|\bTrocadéro\b"
    r"|\bWestminster\b|\bShoreditch\b|\bCamden\b",
    re.I,
)
_INVENTED_PRICE_RE = re.compile(r"\b\d+\s*(?:euro[s]?|€|eur)\b", re.I)
_AUTO_TIME_RE      = re.compile(r"\bà\s+\d{1,2}[h:]\d{0,2}\b|\brendez-vous\s+à\s+\d|\bon\s+se\s+voit\s+à\s+\d", re.I)
_VULGAR_RE         = re.compile(r"\bpute\b|\bsalope\b|\bcon\b|\bconnard|\bfoutre", re.I)
_INTERNAL_MONO_RE  = re.compile(
    # Monologue interne / meta-LLM qui leake
    r"mince,?\s+j.ai\s+dit|il\s+peut\s+pas\s+venir"
    r"|wait[,\s]+let\s+me|let\s+me\s+restart|you\s+sent\s+a\s+message"
    r"|i\s+need\s+to\s+|as\s+an\s+ai|i'm\s+an\s+ai"
    r"|je\s+dois\s+recommen|je\s+recommence|je\s+reformule"
    r"|en\s+tant\s+qu.ia|en\s+tant\s+qu.assistant",
    re.I
)

# Leak d'instructions système / injection de prompt
_SYSTEM_LEAK_RE = re.compile(
    r"instructions?\s+syst[eè]me|system\s+instructions?"
    r"|ignore\s+(les\s+)?instructions|instructions?\s+du\s+syst[eè]me"
    r"|message\s+syst[eè]me|syst[eè]me\s+message"
    r"|\[syst[eè]me\]|\[system\]|\bprompt\b",
    re.I
)

# Bot oublie qu'il a donné son numéro
_NUMBER_ORIGIN_RE = re.compile(
    r"t.as\s+(eu|trouv[eé]|eu\s+mon)\s+(mon\s+)?num[eé]ro\s+(o[uù]|comment|d'o[uù])"
    r"|o[uù]\s+t.as\s+(eu|trouv[eé])\s+(mon\s+)?num[eé]ro"
    r"|comment\s+t.as\s+(eu|trouv[eé])\s+(mon\s+)?num[eé]ro",
    re.I
)

_ENGLISH_WORD_RE   = re.compile(r"\b[a-zA-Z]{4,}\b")
_ENGLISH_WHITELIST = re.compile(
    r"\b(?:EY|OK|ok|app|Instagram|WhatsApp|Telegram|Happn|audit|La\s+Défense|Paris)\b", re.I
)

def _has_banned(text): return bool(_BANNED_WORDS.search(text))
def _has_dash(text):   return bool(_DASH_RE.search(text))
def _has_excl(text):   return "!" in text

def _has_english(text: str) -> bool:
    # Manon parle français — les mots anglais longs hors whitelist sont suspects
    return bool(_ENGLISH_WORD_RE.search(_ENGLISH_WHITELIST.sub("", text)))


def _fix_reply(text: str) -> str:
    """Post-processing Manon. ||| préservé (géré par _split_message)."""
    text = text.replace(" — ", ", ").replace("—", ", ")
    text = re.sub(r" - ", ", ", text)
    text = text.replace("!", "")
    stripped = text.rstrip()
    if stripped.endswith(".") and not stripped.endswith("..."):
        text = stripped[:-1]
    if text and text[0].isupper():
        text = text[0].lower() + text[1:]
    # Mots trop formels → naturel parisien
    text = re.sub(r'\babsolument\b', 'oui', text, flags=re.I)
    text = re.sub(r'\beffectivement\b', 'oui', text, flags=re.I)
    text = re.sub(r'\btout à fait\b', 'oui', text, flags=re.I)
    # "ouais" interdit — trop vulgaire pour Manon
    text = re.sub(r'\bouais\b', 'oui', text, flags=re.I)
    # j' devant une consonne = langage oral relâché → je
    text = re.sub(r"\bj'([bcdfghjklmnpqrstvwxyz])", r"je \1", text, flags=re.I)
    # Argot cité → registre Manon EY
    text = re.sub(r'\bchelou\b', 'bizarre', text, flags=re.I)
    text = re.sub(r'\brelou\b', 'pénible', text, flags=re.I)
    text = re.sub(r'\bkiffer?\b', 'aimer', text, flags=re.I)
    text = re.sub(r'\bcarrément\b', 'vraiment', text, flags=re.I)
    text = re.sub(r'\bc\'est\s+(trop\s+)?ouf\b', "c'est dingue", text, flags=re.I)
    text = re.sub(r'\bwesh\b', '', text, flags=re.I)
    text = re.sub(r'\bfrère\b', '', text, flags=re.I)
    # Expressions "racaille" qui sonnent 15ans cité
    text = re.sub(r'\bc\'est quoi ça\b', "je vois pas", text, flags=re.I)
    text = re.sub(r'\bc\'est quoi ce\b', "c'est quoi exactement ce", text, flags=re.I)
    text = re.sub(r"\bça fait le taf\b", "c'est pratique", text, flags=re.I)
    text = re.sub(r"\bfait le taf\b", "marche bien", text, flags=re.I)
    text = re.sub(r"\bça fait le job\b", "c'est pratique", text, flags=re.I)
    text = re.sub(r"\bfait le job\b", "ça marche", text, flags=re.I)
    text = re.sub(r"\bd'où ça sort\b", "ça vient d'où", text, flags=re.I)
    # Contractions françaises — "à le" → "au", "de le" → "du" (erreurs LLM + _WRONG_DISTRICT_RE)
    text = re.sub(r'\bà\s+le\b', 'au', text, flags=re.I)
    text = re.sub(r'\bde\s+le\b', 'du', text, flags=re.I)
    text = re.sub(r'\bà\s+les\b', 'aux', text, flags=re.I)
    text = re.sub(r'\bde\s+les\b', 'des', text, flags=re.I)
    return text.strip()


def _split_message(text: str) -> list[str]:
    """Même logique que telegram_bot.py — ||| prioritaire."""
    if "|||" in text:
        parts = [p.strip() for p in text.split("|||") if p.strip()]
        if len(parts) >= 2:
            p1, p2 = parts[0], " ".join(parts[1:])
            return [_fix_reply(p1), _fix_reply(p2)]
        text = text.replace("|||", " ").strip()
    if "\n\n" in text:
        parts = [p.strip() for p in text.split("\n\n") if p.strip()]
        if len(parts) >= 2:
            return [_fix_reply(p) for p in parts]
    if "\n" in text:
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) >= 2:
            p1 = parts[0]
            p2 = " ".join(parts[1:])
            return [_fix_reply(p1), _fix_reply(p2)]
    if len(text) <= 80:
        return [text]
    lo, hi = len(text) // 4, (3 * len(text)) // 4
    idx = text.find(". ", lo, hi)
    if idx != -1:
        a, b = text[:idx].strip(), text[idx+2:].strip()
        if a and b and len(a) >= 20 and len(b) >= 20:
            return [_fix_reply(a), _fix_reply(b)]
    idx = text.find(", ", lo, hi)
    if idx != -1:
        a, b = text[:idx].strip(), text[idx+2:].strip()
        if a and b and len(b) > 8:
            return [_fix_reply(a), _fix_reply(b)]
    return [text]


def _reply_is_clean(text: str, lang: str) -> bool:
    return (
        not _has_banned(text)
        and not _has_forbidden_emoji(text)
        and not _has_dash(text)
        and not _has_excl(text)
        and not (lang == "ru" and _has_english(text))
    )


def _apply_hard_filters(reply: str, lang: str) -> str:
    """Filtres post-génération identiques à telegram_bot.py."""
    # Placeholder vocal
    if _PLACEHOLDER_RE.search(reply):
        reply = _PLACEHOLDER_RE.sub("", reply).strip() or "ok"
    # Monologue interne / meta-LLM qui leake → réponse neutre
    if _INTERNAL_MONO_RE.search(reply):
        reply = "nan"
    # Leak instructions système / injection de prompt → réponse neutre
    if _SYSTEM_LEAK_RE.search(reply):
        reply = "quoi"
    # Bot demande d'où vient son numéro → elle l'a donné elle-même
    if _NUMBER_ORIGIN_RE.search(reply):
        reply = "bah je t'avais filé mon numéro nan"
    # Emoji
    if _has_forbidden_emoji(reply):
        reply = _strip_forbidden_emojis(reply) or "ладно"
    # Mauvais prénom
    if _WRONG_NAME_RE.search(reply):
        reply = _WRONG_NAME_RE.sub("manon", reply)
    # Mauvais quartier
    if _WRONG_DISTRICT_RE.search(reply):
        reply = _WRONG_DISTRICT_RE.sub("le 11e", reply)
    # Prix inventés
    if _INVENTED_PRICE_RE.search(reply):
        reply = re.sub(r"\b\d+\s*(?:euro[s]?|€|eur)\b", "pas cher", reply, flags=re.I)
    # Heure auto-fixée
    if _AUTO_TIME_RE.search(reply):
        reply = re.sub(r"\bà\s+\d{1,2}[h:]\d{0,2}\b", "", reply, flags=re.I)
        reply = reply.strip() or "ok"
    # Vulgaire
    if _VULGAR_RE.search(reply):
        reply = "c'est non"
    return reply


# ══════════════════════════════════════════════════════════════
# LANGUE
# ══════════════════════════════════════════════════════════════

def _detect_lang(text: str, history: list) -> str:
    # Manon parle uniquement français — lang toujours "fr"
    return "fr"


# ══════════════════════════════════════════════════════════════
# MUSÉE PICASSO — objectif de conversion Paris
# ══════════════════════════════════════════════════════════════

_MP_URL = "https://the-museepicasso.com"

_MEET_SIGNAL_RE = re.compile(
    r"on\s+se\s+voit|on\s+se\s+retrouve|on\s+pourrait\s+se\s+voir"
    r"|se\s+voir|se\s+retrouver|sortir\s+ensemble|un\s+verre"
    r"|café\s+ensemble|rencontrer|rendez.vous|rdv",
    re.I,
)
_MP_DISCUSSED_RE = re.compile(r"picasso|the-museepicasso", re.I)
_MP_LINK_RE      = re.compile(r"the-museepicasso\.com", re.I)
_DAY_RE = re.compile(
    r"samedi|dimanche|vendredi|jeudi|lundi|mardi|mercredi"
    r"|\bce\s+week.?end\b|\bcette\s+semaine\b|\bce\s+soir\b|\bdemain\b"
    r"|t.es\s+dispo\s+quand|ça\s+te\s+va\s*\?|tu\s+es\s+libre",
    re.I,
)
_DAY_CONFIRMED_RE = re.compile(
    r"\bok\b|ça\s+marche|c.est\s+bon|ouais?\b|pas\s+de\s+prob"
    r"|avec\s+plaisir|volontiers|pourquoi\s+pas|super|parfait",
    re.I,
)
_MP_OBJ_EXPENSIVE_RE = re.compile(
    r"c.est\s+cher|trop\s+cher|payant|combien\s+ça\s+coûte|c.est\s+combien|prix|entrée\s+pay",
    re.I,
)
_MP_OBJ_MUSEUM_RE = re.compile(
    r"aime\s+pas\s+(?:les\s+)?musées?|pas\s+fan\s+des\s+musées?|musée\s+c.est\s+(?:chiant|barbant|ennuyeux)",
    re.I,
)
_MP_OBJ_NO_RE = re.compile(
    r"\bpas\s+(?:envie|intéressé|intéressée)\b|non\s+merci|autre\s+chose|autre\s+endroit|ailleurs",
    re.I,
)
_MP_NON_MP_RE = re.compile(
    r"\bbar\b|café|cafe\b|resto\b|restaurant\b|\bparc\b|\bpark\b|cinéma\b|cinema\b|chez\s+(?:moi|toi)",
    re.I,
)


def _build_meet_injection(user_msg: str, hist: list, turn: int) -> str | None:
    """Machine d'état de conversion Musée Picasso — deux temps : jour d'abord, lieu ensuite."""
    if turn < 4:
        return None

    asst_msgs = [m.get("content", "") for m in hist if m.get("role") == "assistant"]
    user_msgs = [m.get("content", "") for m in hist if m.get("role") == "user"]

    link_sent    = any(_MP_LINK_RE.search(m) for m in asst_msgs)
    mp_discussed = any(_MP_DISCUSSED_RE.search(m) for m in hist)
    obj_no       = any(_MP_OBJ_NO_RE.search(m)       for m in user_msgs[-3:])
    obj_expensive= any(_MP_OBJ_EXPENSIVE_RE.search(m) for m in user_msgs[-3:])
    obj_museum   = any(_MP_OBJ_MUSEUM_RE.search(m)    for m in user_msgs[-3:])

    # Refus clair → abandonner Picasso pour cette conv
    if obj_no and mp_discussed:
        return None

    # ── ÉTAT 3 : Lien envoyé → focus détails pratiques ──
    if link_sent:
        mp_first_idx = next(
            (i for i, m in enumerate(hist)
             if m.get("role") == "assistant" and _MP_LINK_RE.search(m.get("content", ""))),
            None,
        )
        post_link_user = [m["content"] for m in hist[mp_first_idx:] if m.get("role") == "user"] if mp_first_idx else []
        venue_confirmed = any(_DAY_CONFIRMED_RE.search(t) for t in post_link_user)
        if venue_confirmed:
            return None  # Tout est calé, ne pas relancer
        return (
            "[SYSTÈME CONVERSION] Tu as proposé le Musée Picasso et envoyé le lien. "
            "Maintenant focus sur les détails : heure, point de rendez-vous. "
            "Ne renvoie PAS le lien. Ne répète pas le nom du musée inutilement."
        )

    # ── Objections (uniquement si Picasso déjà évoqué) ──
    if obj_expensive and mp_discussed:
        return (
            "[SYSTÈME CONVERSION] Il trouve ça cher. "
            "Réponds : 'c'est pas cher, une quinzaine d'euros' et relance sur le jour. "
            "Pas plus d'explication."
        )
    if obj_museum and mp_discussed:
        return (
            "[SYSTÈME CONVERSION] Il dit qu'il aime pas les musées. "
            "Réponds : 'c'est pas vraiment un musée tradi, c'est plutôt sympa' et maintiens le Picasso."
        )

    # ── ÉTAT 2 : Jour calé → proposer le Picasso maintenant ──
    # Détecte si le bot a proposé un jour ET que l'user vient de confirmer
    bot_proposed_day = any(_DAY_RE.search(m) for m in asst_msgs[-3:])
    user_just_confirmed = bool(_DAY_CONFIRMED_RE.search(user_msg)) if user_msg else False
    if bot_proposed_day and user_just_confirmed and not mp_discussed:
        return (
            f"[SYSTÈME CONVERSION] Le jour est confirmé. C'est maintenant que tu proposes l'endroit. "
            f"Glisse le Musée Picasso naturellement : 'j'avais un endroit en tête — le musée picasso, t'es déjà allé ?' "
            f"puis envoie le lien {_MP_URL} dans ce même message. Naturel, pas trop enthousiaste. "
            f"PAS de grande explication sur le musée."
        )

    # ── ÉTAT 1 : Signal de rencontre → proposer un jour (pas de Picasso encore) ──
    if _MEET_SIGNAL_RE.search(user_msg) and turn >= 5:
        return (
            "[SYSTÈME CONVERSION] Il propose de se voir. "
            "Réponds positivement et propose un jour précis : 'samedi ça te va ?' / 'vendredi soir ?'. "
            "PAS de lieu, PAS de Picasso encore. Juste le jour. Naturel, pas trop enthousiaste."
        )

    # ── Pivot : bot a accepté autre chose → ramener vers Picasso ──
    if not mp_discussed and turn >= 5:
        non_mp_in_asst = any(_MP_NON_MP_RE.search(m) for m in asst_msgs[-4:])
        if non_mp_in_asst:
            return (
                f"[SYSTÈME CONVERSION] Tu as accepté de sortir sans proposer d'endroit précis. "
                f"Glisse maintenant le Musée Picasso : 'j'avais un endroit en tête si jamais — {_MP_URL}'. "
                f"Une fois, naturel."
            )

    # ── Ouverture tardive : conv longue sans rencontre évoquée ──
    if turn >= 8 and not mp_discussed:
        no_meet_signal = not any(_MEET_SIGNAL_RE.search(m) for m in hist)
        if no_meet_signal:
            return (
                "[SYSTÈME CONVERSION] La conv dure depuis un moment. "
                "Glisse une ouverture naturelle : 't'es dans quel coin à Paris ?' "
                "ou 'j'ai un truc de libre cette semaine si jamais'. "
                "Juste une porte ouverte. PAS de Picasso encore."
            )

    return None


# ══════════════════════════════════════════════════════════════
# MAX TOKENS — miroir naturel
# ══════════════════════════════════════════════════════════════

def _max_tokens(user_msg: str) -> int:
    n = len(user_msg.strip())
    if n <= 10:  return 60
    if n <= 30:  return 100
    if n <= 80:  return 160
    if n <= 200: return 220
    return 280


# ══════════════════════════════════════════════════════════════
# TIMING HUMAIN (renvoyé au bridge Node.js)
# ══════════════════════════════════════════════════════════════

def _compute_timing(phone: str, text: str) -> dict:
    """Calcule think_seconds, read_seconds, typing_seconds pour le bridge."""
    now = time.time()
    last_sent = _last_sent_to.get(phone, 0)
    user_resp_time = (now - last_sent) if last_sent > 0 else 999.0
    chars = len(text.strip())
    words = len(text.split())
    mirroring = last_sent > 0 and user_resp_time < 25
    first = (last_sent == 0)

    if first:
        think = random.uniform(3.0, 8.0)
    elif mirroring:
        if user_resp_time < 6:    think = random.uniform(2.0, 6.0)
        elif user_resp_time < 15: think = random.uniform(4.0, 10.0)
        else:                     think = random.uniform(6.0, 14.0)
    elif chars <= 20:  think = random.uniform(4.0, 10.0)
    elif chars <= 80:  think = random.uniform(6.0, 16.0)
    else:              think = random.uniform(8.0, 22.0)

    # Temps de lecture (après avoir "ouvert" le chat)
    if words <= 2:    read = random.uniform(1.0, 2.0)
    elif words <= 5:  read = random.uniform(1.5, 3.0)
    elif words <= 10: read = random.uniform(2.5, 5.0)
    elif words <= 20: read = random.uniform(3.5, 6.5)
    else:             read = random.uniform(5.0, 9.0)

    return {
        "think_seconds": round(think, 1),
        "read_seconds":  round(read, 1),
    }


# ══════════════════════════════════════════════════════════════
# PAUSE MANUELLE OPÉRATEUR
# ══════════════════════════════════════════════════════════════

def _pause_active(phone: str) -> bool:
    resume = _wa_pause.get(phone, 0)
    return time.time() < resume


# ══════════════════════════════════════════════════════════════
# GÉNÉRATION RÉPONSE
# ══════════════════════════════════════════════════════════════

def _build_messages_from_wa_history(wa_history: list) -> list:
    """Convertit l'historique réel WA en messages Claude (user/assistant)."""
    messages = []
    for m in wa_history:
        role = "assistant" if m.get("from_me") else "user"
        text = m.get("text", "").strip()
        if not text:
            continue
        # Fusionne les messages consécutifs du même rôle
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += " " + text
        else:
            messages.append({"role": role, "content": text})
    return messages[-MAX_HISTORY:]


_ADDRESS_RE = re.compile(
    # nombre en tête + type de voie (ex: "6 Rue X")
    r'^\s*\d+[,\s]+(?:rue|avenue|ave|boulevard|blvd|allée|impasse|place|chemin|route|voie|cité|résidence|villa)\b'
    # OU type de voie en tête + nombre à la fin (ex: "Rue X, 18")
    r'|^\s*(?:rue|avenue|boulevard|blvd|allée|impasse|place|chemin|route)\b.{3,50}\d+\s*$',
    re.I
)

def _enrich_incoming(text: str, hist: list) -> str:
    """Ajoute du contexte au message si le format ou la longueur risquent de perdre le LLM."""
    stripped = text.strip()

    # Adresse postale → le LLM sait que c'est une localisation
    if _ADDRESS_RE.match(stripped):
        return f"[Il donne son adresse/quartier : {stripped}]"

    # Message court (≤40 chars) sans contexte clair → rappel de la dernière question posée
    if len(stripped) <= 40 and hist:
        last_assistant = next(
            (m["content"] for m in reversed(hist) if m["role"] == "assistant"), None
        )
        if last_assistant and "?" in last_assistant:
            q = last_assistant.split("?")[0].strip()[-60:]
            return f'{stripped} [il répond à : "...{q}?"]'

    return text


def _generate_response(phone: str, name: str, text: str, wa_history: list | None = None) -> dict | None:
    with _hist_lock:
        hist = _wa_histories.setdefault(phone, [])
        lang = _detect_lang(text, hist)
        _user_lang[phone] = lang

        # Si l'historique réel WA est fourni, on l'utilise comme source de vérité
        if wa_history:
            real_msgs = _build_messages_from_wa_history(wa_history)
            hist[:] = real_msgs
            turn = len([m for m in hist if m["role"] == "user"])
        else:
            hist.append({"role": "user", "content": text})
            if len(hist) > MAX_HISTORY:
                hist[:] = hist[-MAX_HISTORY:]
            turn = len(hist) // 2 + 1

        # Enrichit le dernier message user (adresse, message court ambigu)
        # Appliqué APRÈS avoir chargé wa_history pour avoir le bon contexte précédent
        enriched = _enrich_incoming(text, hist)
        if enriched != text:
            for i in range(len(hist) - 1, -1, -1):
                if hist[i]["role"] == "user":
                    hist[i]["content"] = enriched
                    break
            turn = len(hist) // 2 + 1

        # Injection tour 1 : rappel que la conv Happn n'est pas accessible
        first_turn_injection = None
        if turn == 1:
            first_turn_injection = (
                "[CONTEXTE] Cette personne t'a connue sur Happn. Tu lui as donné ton numéro. "
                "Tu n'as PAS accès aux messages échangés sur Happn. "
                "Si elle fait référence à quelque chose dit avant, rebondis sur le FOND sans confirmer les détails. "
                "Ne demande jamais ce qui a été dit, ne dis jamais que tu n'as plus accès aux anciens messages."
            )

        # Injection rencontre Paris
        meet_injection = _build_meet_injection(text, hist, turn)

        messages = list(hist)
        extra = []
        if first_turn_injection:
            extra.append({"role": "user", "content": first_turn_injection})
        if meet_injection:
            extra.append({"role": "user", "content": meet_injection})
        if extra:
            messages = messages + extra

        soul_cached = [{"type": "text", "text": SOUL, "cache_control": {"type": "ephemeral"}}]

        reply = ""
        for attempt in range(4):
            issues = []
            if attempt > 0 and reply:
                if _has_banned(reply):         issues.append("mots bannis: absolument/effectivement/tout à fait/c'est intéressant/bien sûr")
                if _has_forbidden_emoji(reply): issues.append("emoji interdit — seuls 😏 😐 autorisés")
                if _has_dash(reply):           issues.append("tiret interdit")
                if _has_excl(reply):           issues.append("'!' interdit")
                if _has_english(reply):        issues.append("mot anglais trop long — parle français")
                if issues:
                    retry_msg = (
                        f"[SYSTÈME] Problème(s) dans ta réponse: {'; '.join(issues)}. "
                        "Réécris-la: courte, naturelle, sans les éléments listés."
                    )
                    messages_with_retry = messages + [{"role": "user", "content": retry_msg}]
                else:
                    break
            else:
                messages_with_retry = messages

            try:
                resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=_max_tokens(text),
                    system=soul_cached,
                    messages=[m for m in messages_with_retry if m["role"] in ("user", "assistant")],
                )
                reply = _fix_reply(resp.content[0].text.strip())
            except Exception as e:
                print(f"[WA] Claude error: {e}", flush=True)
                hist.pop()
                return None

            if _reply_is_clean(reply, lang):
                break

        # Filtres durs post-génération
        reply = _apply_hard_filters(reply, lang)

        # Normalise l'URL Picasso (évite variantes LLM)
        if _MP_DISCUSSED_RE.search(reply):
            reply = re.sub(
                r"https?://(?:www\.)?(?:museepicasso\.paris|musee-picasso-paris\.fr|the-museepicasso\.com)(?:/[^\s]*)?",
                _MP_URL,
                reply,
                flags=re.I,
            )
            # Si le LLM a mentionné Picasso sans mettre de lien → injecter le lien
            if not _MP_LINK_RE.search(reply) and _MP_DISCUSSED_RE.search(reply):
                reply = reply.rstrip() + f" {_MP_URL}"

        # Stocke en historique (sans |||)
        hist.append({"role": "assistant", "content": reply.replace("|||", " ").strip()})
        _save_histories()

    print(f"[WA] {name} ({phone}) T{turn}: {text[:50]!r} -> {reply[:60]!r}", flush=True)
    return reply


# ══════════════════════════════════════════════════════════════
# TRANSCRIPTION VOCAUX — Groq Whisper
# ══════════════════════════════════════════════════════════════

def _transcribe_audio(data_b64: str, mime_type: str = "audio/ogg") -> str | None:
    """Transcrit un vocal WhatsApp via Groq Whisper (whisper-large-v3-turbo)."""
    if not GROQ_API_KEY:
        print("[WA] GROQ_API_KEY manquant — transcription impossible", flush=True)
        return None
    try:
        audio_bytes = base64.b64decode(data_b64)
        ext = "ogg"
        if "mp4" in mime_type or "m4a" in mime_type:   ext = "m4a"
        elif "webm" in mime_type:                       ext = "webm"
        elif "mpeg" in mime_type or "mp3" in mime_type: ext = "mp3"
        with httpx.Client(timeout=40.0, verify=False) as http:
            resp = http.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"audio.{ext}", io.BytesIO(audio_bytes), mime_type)},
                data={"model": "whisper-large-v3-turbo"},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip() or None
    except Exception as e:
        print(f"[WA] Groq Whisper error: {e}", flush=True)
        return None


# ══════════════════════════════════════════════════════════════
# VISION — Claude Vision (images, stickers, frames vidéo)
# ══════════════════════════════════════════════════════════════

_SAFE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

def _claude_vision(data_b64: str, mime_type: str, prompt: str, max_tokens: int = 120) -> str | None:
    """Appelle Claude Vision avec un prompt et retourne la description."""
    try:
        safe_mime = mime_type if mime_type in _SAFE_MIMES else "image/jpeg"
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": safe_mime, "data": data_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return resp.content[0].text.strip() or None
    except Exception as e:
        print(f"[WA] Vision error: {e}", flush=True)
        return None

def _describe_image(data_b64: str, mime_type: str = "image/jpeg", caption: str = "") -> str | None:
    prompt = "Décris brièvement et factuellement cette image en une phrase, en français. Sois concis."
    if caption:
        prompt += f" Légende de l'utilisateur : '{caption}'"
    return _claude_vision(data_b64, mime_type, prompt)

def _describe_sticker(data_b64: str) -> str | None:
    prompt = "C'est un sticker WhatsApp. Décris-le en 3-5 mots en français (ex: 'sticker chien qui rit', 'sticker cœur rouge')."
    return _claude_vision(data_b64, "image/webp", prompt, max_tokens=40)


# ══════════════════════════════════════════════════════════════
# EXTRACTION FRAME VIDÉO — ffmpeg
# ══════════════════════════════════════════════════════════════

def _extract_video_frame(video_bytes: bytes, mime_type: str = "video/mp4") -> str | None:
    """Extrait une frame représentative d'une vidéo via ffmpeg → base64 JPEG."""
    if not _FFMPEG:
        return None
    ext = "mp4"
    if "webm" in mime_type:                          ext = "webm"
    elif "3gpp" in mime_type or "3gp" in mime_type:  ext = "3gp"
    elif "quicktime" in mime_type or "mov" in mime_type: ext = "mov"
    tmp_in = tmp_out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(video_bytes)
            tmp_in = f.name
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_out = f.name
        # Tente d'extraire à 1s (vidéo longue) puis à 0s (vidéo courte)
        for seek in ("00:00:01", "00:00:00"):
            r = subprocess.run(
                [_FFMPEG, "-y", "-ss", seek, "-i", tmp_in, "-vframes", "1", "-q:v", "2", tmp_out],
                capture_output=True, timeout=20,
            )
            if r.returncode == 0:
                break
        else:
            return None
        with open(tmp_out, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"[WA] ffmpeg error: {e}", flush=True)
        return None
    finally:
        for p in (tmp_in, tmp_out):
            try:
                if p: os.unlink(p)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════

@app.route("/wa/message", methods=["POST"])
def wa_message():
    data  = request.get_json(force=True)
    phone = str(data.get("phone", "")).strip()
    name  = str(data.get("name", phone)).strip()
    text  = str(data.get("text", "")).strip()

    if not phone or not text:
        return jsonify({"error": "missing phone or text"}), 400
    if phone in _wa_blocked:
        return jsonify({"skip": True, "reason": "blocked"})
    if phone in _wa_closer:
        return jsonify({"skip": True, "reason": "closer_lock"})
    if _pause_active(phone):
        return jsonify({"skip": True, "reason": "manual_pause"})

    wa_history = data.get("wa_history", [])
    timing = _compute_timing(phone, text)
    reply_raw = _generate_response(phone, name, text, wa_history)
    if not reply_raw:
        return jsonify({"error": "generation failed"}), 500

    # Split ||| → 2 messages
    parts = _split_message(reply_raw)
    reply  = parts[0]
    second = parts[1] if len(parts) > 1 else ""

    # Frappe mobile humaine : ~55-75 WPM = 0.13-0.20s/char
    def _typing_secs(text: str, min_s: float, max_s: float) -> float:
        rate = random.uniform(0.13, 0.20)
        jitter = random.uniform(-0.5, 1.5)
        return round(max(min_s, min(len(text) * rate + jitter, max_s)), 1)

    typing_s        = _typing_secs(reply,  min_s=3.0, max_s=28.0)
    second_typing_s = _typing_secs(second, min_s=2.0, max_s=18.0) if second else 0.0

    # Plafond dur : think + read + typing ≤ 80s au total
    total = timing["think_seconds"] + timing["read_seconds"] + typing_s
    if total > 80:
        scale = 80 / total
        timing["think_seconds"] = round(timing["think_seconds"] * scale, 1)
        timing["read_seconds"]  = round(timing["read_seconds"]  * scale, 1)
        typing_s                = round(typing_s                * scale, 1)

    # Met à jour timestamp dernier envoi
    _last_sent_to[phone] = time.time()

    return jsonify({
        "reply":            reply,
        "second":           second,
        "think_seconds":    timing["think_seconds"],
        "read_seconds":     timing["read_seconds"],
        "typing_seconds":   round(typing_s, 1),
        "second_typing_s":  round(second_typing_s, 1),
    })


@app.route("/wa/operator", methods=["POST"])
def wa_operator():
    """Notifie que l'opérateur a envoyé un message manuel → pause +5 min."""
    data  = request.get_json(force=True)
    phone = str(data.get("phone", "")).strip()
    if not phone:
        return jsonify({"error": "missing phone"}), 400
    resume = _wa_pause.get(phone, time.time())
    if resume < time.time():
        resume = time.time()
    resume += _WA_PAUSE_SECONDS
    _wa_pause[phone] = resume
    _save_pause()
    print(f"[WA] Pause opérateur {phone} jusqu'à {resume:.0f}", flush=True)
    return jsonify({"ok": True, "resume_at": resume})


@app.route("/wa/block", methods=["POST"])
def wa_block():
    phone = str(request.get_json(force=True).get("phone", "")).strip()
    if not phone: return jsonify({"error": "missing phone"}), 400
    _wa_blocked.add(phone)
    _WA_BLOCKED_PATH.write_text(json.dumps(list(_wa_blocked)), encoding="utf-8")
    return jsonify({"ok": True, "blocked": phone})

@app.route("/wa/unblock", methods=["POST"])
def wa_unblock():
    phone = str(request.get_json(force=True).get("phone", "")).strip()
    _wa_blocked.discard(phone)
    _WA_BLOCKED_PATH.write_text(json.dumps(list(_wa_blocked)), encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/wa/closer/lock", methods=["POST"])
def wa_closer_lock():
    phone = str(request.get_json(force=True).get("phone", "")).strip()
    _wa_closer.add(phone)
    _WA_CLOSER_PATH.write_text(json.dumps(list(_wa_closer)), encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/wa/closer/unlock", methods=["POST"])
def wa_closer_unlock():
    phone = str(request.get_json(force=True).get("phone", "")).strip()
    _wa_closer.discard(phone)
    _WA_CLOSER_PATH.write_text(json.dumps(list(_wa_closer)), encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/wa/media_text", methods=["POST"])
def wa_media_text():
    """Transcrit/décrit un média WA — retourne le texte brut, sans générer de réponse."""
    data       = request.get_json(force=True)
    media_type = str(data.get("media_type", "")).strip()
    data_b64   = str(data.get("data_b64", "")).strip()
    mime       = str(data.get("mime", "")).strip() or "application/octet-stream"
    caption    = str(data.get("caption", "")).strip()

    # ── Vocaux ──────────────────────────────────────────────────
    if media_type in ("audio", "ptt") and data_b64:
        transcription = _transcribe_audio(data_b64, mime)
        text = f"[vocal: {transcription}]" if transcription else "[vocal incompréhensible]"

    # ── Images ──────────────────────────────────────────────────
    elif media_type == "image" and data_b64:
        description = _describe_image(data_b64, mime, caption)
        if description:
            text = f"[image: {description}]" + (f' — "{caption}"' if caption else "")
        elif caption:
            text = f"[image: {caption}]"
        else:
            text = "[image envoyée]"

    # ── Stickers — Claude Vision sur le WebP ────────────────────
    elif media_type == "sticker" and data_b64:
        description = _describe_sticker(data_b64)
        text = f"[sticker: {description}]" if description else "[sticker]"

    # ── Vidéos — extraction frame + Claude Vision ────────────────
    elif media_type == "video" and data_b64:
        video_bytes = base64.b64decode(data_b64)
        frame_b64 = _extract_video_frame(video_bytes, mime)
        if frame_b64:
            description = _claude_vision(
                frame_b64, "image/jpeg",
                "Décris cette capture d'une vidéo WhatsApp en une phrase en français."
                + (f" Légende : '{caption}'" if caption else ""),
            )
            text = f"[vidéo: {description}]" + (f' — "{caption}"' if caption else "") if description \
                   else (f"[vidéo: {caption}]" if caption else "[vidéo envoyée]")
        else:
            text = f"[vidéo: {caption}]" if caption else "[vidéo envoyée]"

    # ── Documents ────────────────────────────────────────────────
    elif media_type == "document":
        text = f"[document: '{caption}']" if caption else "[document envoyé]"

    else:
        return jsonify({"text": None})

    return jsonify({"text": text})


@app.route("/wa/health")
def wa_health():
    return jsonify({"ok": True, "ts": time.time()})

@app.route("/wa/status")
def wa_status():
    return jsonify({
        "histories":    len(_wa_histories),
        "blocked":      list(_wa_blocked),
        "closer_lock":  list(_wa_closer),
        "paused":       {k: v for k, v in _wa_pause.items() if v > time.time()},
    })

if __name__ == "__main__":
    print("[WA] Serveur démarré sur port 5057", flush=True)
    app.run(host="0.0.0.0", port=5057, debug=False, use_reloader=False)
