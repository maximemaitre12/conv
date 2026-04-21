"""
ZvukoGram Voice Agent — sélectionne et fournit des vocaux pour Eva.

Priorité :
  1. Fichiers téléchargés depuis zvukogram.com  (voice_catalog.json)
  2. Synthèse TTS via API zvukogram.com          (si token disponible)
  3. None                                         (Eva envoie du texte)

Usage standalone :
    python zvukogram_agent.py --list-voices      # voix TTS disponibles
    python zvukogram_agent.py --test "как дела"  # génère un vocal TTS test
    python zvukogram_agent.py --pick "занята"    # cherche dans le catalogue
"""

import asyncio
import hashlib
import json
import os
import re
import argparse
from io import BytesIO
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher

from dotenv import load_dotenv

load_dotenv()

ZVUKOGRAM_TOKEN = os.getenv("ZVUKOGRAM_TOKEN", "")
ZVUKOGRAM_EMAIL = os.getenv("ZVUKOGRAM_EMAIL", "")
DEFAULT_VOICE   = os.getenv("ZVUKOGRAM_VOICE", "Alena")

CATALOG_PATH = Path("voice_catalog.json")
TTS_CACHE    = Path("voice_cache")
TTS_CACHE.mkdir(exist_ok=True)

# ─── Logs ─────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(tag, msg, extra=""):
    line = f"[{ts()}] [{tag}] {msg}"
    if extra:
        line += f"  |  {extra}"
    print(line, flush=True)

# ─── Catalogue (fichiers téléchargés) ────────────────────────

_catalog: list[dict] | None = None

def _load_catalog() -> list[dict]:
    global _catalog
    if _catalog is not None:
        return _catalog
    if not CATALOG_PATH.exists():
        _catalog = []
        return _catalog
    _catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    # Filtre les entrées dont le fichier n'existe plus
    _catalog = [e for e in _catalog if Path(e["file"]).exists()]
    log("CAT", f"Catalogue chargé : {len(_catalog)} vocaux")
    return _catalog


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ─── Détection de contexte pour sélection vocale ─────────────

_CTX_CONFIRM  = re.compile(r"\bок\b|хорошо|договорились|согласна|ладно|давай|ага|угу|конечн|приду|буду там", re.I)
_CTX_DENY     = re.compile(r"\bнет\b|не могу|не хочу|не буду|не получится|нельзя|пока нет|не сейчас", re.I)
_CTX_GREET    = re.compile(r"привет|здравствуй|доброе утро|добрый вечер|добрый день|хай\b", re.I)
_CTX_BYE      = re.compile(r"пока[\-\s]*пока|до свидания|спокойной ночи|до завтра|до встречи|удачи тебе|ну пока[\.!]?$|окей пока[\.!]?$|ладно пока[\.!]?$", re.I)
_CTX_FLIRT    = re.compile(r"нравишься|хочу тебя|красивый|симпатичн|скучаю|влюбилась|милый|солнышко", re.I)
_CTX_BUSY     = re.compile(r"занята|смотрю|гуляю|читаю|работаю|крашу|иду спать|устала|позже напишу", re.I)
_CTX_REACTION = re.compile(r"интересно|странно|прикольно|не поняла|серьёзно|ничего себе|ой\b|вот это", re.I)

_TAG_POOLS = {
    "confirm":  ["confirm"],
    "deny":     ["deny"],
    "greeting": ["greeting"],
    "bye":      ["bye"],
    "flirt":    ["flirt"],
    "busy":     ["busy"],
    "reaction": ["reaction"],
    "casual":   ["casual"],
}

# Vocaux à ne jamais envoyer de façon non sollicitée (trop sexuels / hors contexte
# ou réservés à des contextes spécifiques comme le post-lien)
_NSFW_TRANSCRIPTS = {
    "как тебе моя попа", "хочешь меня?", "хочешь меня", "я тебя люблю",
    "мм,какой большой хочу тебя", "м-м-м, какой большой, хочу тебя",
    "а грудь нравится?", "а грудь нравится",
    "и я тебя люблю",
    "можешь мне парнушку посоветовать интересную",
    "женский голос стон", "женский звук уеееее ху",
    "женский голос восклицание", "женский голос возглас",
    # Réservés post-lien uniquement — ne pas envoyer comme remplacement générique
    "договорились", "до завтра",
    # Clips "au revoir" — trop sensibles au contexte, jamais automatiques
    # (score peut exploser à 1.47+ à cause du word-boost sur "пока")
    "пока-пока", "чмоки-чмоки", "спокойной ночи", "сладких снов",
    # Vocaux à forte valeur sémantique — trop spécifiques, faux positifs fréquents
    "можешь фильм посоветовать", "можешь мне парнушку посоветовать интересную",
}

def _detect_context(eva_text: str, user_text: str = "") -> str:
    """Détecte le contexte dominant du message d'Eva pour choisir la bonne pool vocale."""
    t = eva_text.lower() + " " + user_text.lower()
    if _CTX_GREET.search(t):   return "greeting"
    if _CTX_BYE.search(t):     return "bye"
    if _CTX_FLIRT.search(t):   return "flirt"
    if _CTX_CONFIRM.search(t): return "confirm"
    if _CTX_DENY.search(t):    return "deny"
    if _CTX_BUSY.search(t):    return "busy"
    if _CTX_REACTION.search(t): return "reaction"
    return "casual"


def pick_voice_for(text: str, user_text: str = "", fallback: bool = True,
                   exclude: set | None = None, min_score: float = 0.68) -> dict | None:
    """
    Sélectionne le meilleur vocal pour ce message.
    1. Essaie une correspondance exacte/fuzzy sur le transcript.
    2. Si aucun match satisfaisant ET fallback=True → sélection par contexte.
    exclude : set de filenames déjà envoyés à ce user (jamais répéter).
    min_score : seuil minimum (défaut 0.68 — assez élevé pour éviter les faux positifs).
    """
    import random as _random
    catalog = _load_catalog()
    if not catalog:
        return None

    exclude = exclude or set()
    text_lower = text.lower()
    best_score = 0.0
    best_entry = None

    for entry in catalog:
        if entry.get("filename") in exclude:
            continue
        transcript = entry.get("transcript", "").lower()
        tags       = entry.get("tags", [])

        score = _similarity(text_lower, transcript)

        for word in re.findall(r'\w+', text_lower):
            if len(word) > 3 and word in transcript:
                score += 0.25
        for tag in tags:
            if tag in text_lower:
                score += 0.15

        # Boost pour les transcripts courts (≤3 mots) : si le mot-clé est présent
        # Limité aux mots de ≥4 lettres pour éviter que "нет", "да", "ну" matchent n'importe quoi
        transcript_words = re.findall(r'\w+', transcript)
        if len(transcript_words) <= 3:
            for word in transcript_words:
                if len(word) >= 4 and word in text_lower:
                    score += 0.30

        if score > best_score:
            best_score = score
            best_entry = entry

    # Seuil configurable (défaut 0.68) — en dessous le match est trop flou/hors-sujet
    if best_score >= min_score and best_entry:
        transcript_low = best_entry.get("transcript", "").lower()
        if transcript_low not in _NSFW_TRANSCRIPTS:
            log("VOX", f"Match transcript (score={best_score:.2f})", best_entry.get("transcript", ""))
            return best_entry
        else:
            log("VOX", f"Match NSFW ignoré (score={best_score:.2f})", best_entry.get("transcript", ""))

    # ── Fallback contextuel : pool par catégorie ──
    if not fallback:
        return None

    context = _detect_context(text, user_text)

    # Pas de fallback sur "casual" — trop vague, risque de vocaux hors-sujet
    if context == "casual":
        return None

    target_tags = _TAG_POOLS.get(context, [])
    if not target_tags:
        return None

    # Pool filtrée : bonne catégorie + pas NSFW + pas déjà envoyé
    safe = [
        e for e in catalog
        if any(t in e.get("tags", []) for t in target_tags)
        and e.get("transcript", "").lower() not in _NSFW_TRANSCRIPTS
        and e.get("filename") not in exclude
    ]
    if not safe:
        return None

    choice = _random.choice(safe)
    log("VOX", f"Fallback contextuel (ctx={context})", choice.get("transcript", ""))
    return choice


def pick_random_voice(tags: list[str] | None = None) -> dict | None:
    """Retourne un vocal aléatoire depuis le catalogue, filtré par tags optionnels."""
    import random
    catalog = _load_catalog()
    if not catalog:
        return None

    if tags:
        pool = [e for e in catalog if any(t in e.get("tags", []) for t in tags)]
        if not pool:
            pool = catalog
    else:
        pool = catalog

    choice = random.choice(pool)
    log("VOX", f"Pick aléatoire", choice.get("transcript", ""))
    return choice


# ─── TTS API (fallback) ───────────────────────────────────────

class VoiceAgent:
    """
    Agent TTS — utilise l'API zvukogram.com pour synthétiser des vocaux.
    Utilisé uniquement si le catalogue ne contient pas de match satisfaisant.
    """

    def __init__(self):
        self._api = None
        self.voice = DEFAULT_VOICE
        self._tts_available = bool(ZVUKOGRAM_TOKEN and ZVUKOGRAM_EMAIL)
        if not self._tts_available:
            log("WRN", "TTS désactivé (ZVUKOGRAM_TOKEN/EMAIL manquants)")

    def _get_api(self):
        if not self._tts_available:
            return None
        if self._api is None:
            from zvukogram import ZvukoGram
            self._api = ZvukoGram(ZVUKOGRAM_TOKEN, ZVUKOGRAM_EMAIL)
        return self._api

    async def close(self):
        if self._api:
            await self._api.session.close()
            self._api = None

    def _cache_path(self, text: str) -> Path:
        key = hashlib.md5(f"{self.voice}:{text}".encode()).hexdigest()
        return TTS_CACHE / f"{key}.mp3"

    async def generate_tts(self, text: str) -> BytesIO | None:
        """Synthétise `text` via TTS. Retourne BytesIO ou None."""
        if not self._tts_available:
            return None

        cached = self._cache_path(text)
        if cached.exists():
            log("TTS", "Cache hit", text[:40])
            buf = BytesIO(cached.read_bytes())
            buf.name = "voice.mp3"
            return buf

        log("TTS", f"Génération", f"voice={self.voice} text={text[:60]}")
        api = self._get_api()
        try:
            from zvukogram import ZvukoGramError
            if len(text) <= 300:
                audio = await api.tts(voice=self.voice, text=text, format="mp3", speed=0.95)
            else:
                audio = await api.tts_long(voice=self.voice, text=text, format="mp3", speed=0.95)
                for _ in range(30):
                    await asyncio.sleep(1)
                    audio = await api.check_progress(audio.id)
                    if audio.status == 1 and audio.file:
                        break

            if not audio.file:
                log("ERR", "Aucun fichier retourné par l'API TTS")
                return None

            buf = await audio.download()
            if not isinstance(buf, BytesIO):
                return None

            cached.write_bytes(buf.getvalue())
            buf.seek(0)
            buf.name = "voice.mp3"
            log("TTS", f"Vocal généré ({len(buf.getvalue())} bytes)", f"solde={audio.balance}")
            return buf

        except Exception as e:
            log("ERR", f"TTS échoué : {e}")
            return None

    async def generate(self, text: str, user_text: str = "") -> tuple[BytesIO | None, str | None]:
        """
        Point d'entrée principal.
        1. Match catalogue (fuzzy + fallback contextuel) — toujours tente de trouver quelque chose
        2. TTS si token disponible (fallback)
        Retourne (BytesIO, transcript_utilisé) ou (None, None).
        """
        # 1. Catalogue (avec fallback contextuel intégré)
        entry = pick_voice_for(text, user_text=user_text, fallback=True)
        if entry:
            path = Path(entry["file"])
            if path.exists():
                buf = BytesIO(path.read_bytes())
                buf.name = "voice.mp3"
                return buf, entry["transcript"]

        # 2. TTS (seulement si token configuré)
        buf = await self.generate_tts(text)
        if buf:
            return buf, text

        return None, None

    async def list_voices(self, lang_filter: str = "Русский") -> list:
        api = self._get_api()
        if not api:
            return []
        all_voices = await api.get_voices()
        result = []
        for lang, voices in all_voices.items():
            if lang_filter.lower() in lang.lower():
                for v in voices:
                    result.append({"voice": v.voice, "sex": v.sex, "price": v.price, "pro": v.pro})
        return result


# ─── Singleton partagé avec telegram_bot.py ───────────────────

_agent: VoiceAgent | None = None

def get_voice_agent() -> VoiceAgent:
    global _agent
    if _agent is None:
        _agent = VoiceAgent()
    return _agent


# ─── CLI standalone ───────────────────────────────────────────

async def cli_main():
    parser = argparse.ArgumentParser(description="ZvukoGram Voice Agent pour Eva")
    parser.add_argument("--list-voices", action="store_true", help="Lister les voix TTS russes")
    parser.add_argument("--test",  metavar="TEXT",  help="Générer un vocal TTS test")
    parser.add_argument("--pick",  metavar="TEXT",  help="Chercher dans le catalogue")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    args = parser.parse_args()

    agent = VoiceAgent()
    agent.voice = args.voice

    try:
        if args.list_voices:
            voices = await agent.list_voices()
            if not voices:
                print("TTS non configuré (ZVUKOGRAM_TOKEN manquant) ou aucune voix trouvée.")
            else:
                print(f"\n{'Voix':<25} {'Sexe':<10} {'Prix':<8} Pro")
                print("-" * 50)
                for v in voices:
                    print(f"{v['voice']:<25} {v['sex']:<10} {v['price']:<8} {'✓' if v['pro'] else ''}")

        elif args.pick:
            entry = pick_voice_for(args.pick)
            if entry:
                print(f"\nMeilleur match pour \"{args.pick}\":")
                print(f"  → {entry['transcript']}")
                print(f"     Tags   : {', '.join(entry['tags'])}")
                print(f"     Fichier: {entry['file']}")
            else:
                print(f"Aucun match dans le catalogue pour \"{args.pick}\"")

        elif args.test:
            buf, transcript = await agent.generate(args.test)
            if buf:
                out = Path("test_voice.mp3")
                out.write_bytes(buf.read())
                print(f"\nVocal sauvegardé : {out.resolve()}")
                print(f"Transcript utilisé : {transcript}")
            else:
                print("Échec de génération.")
        else:
            parser.print_help()
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(cli_main())
