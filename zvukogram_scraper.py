"""
ZvukoGram Scraper — télécharge les vocaux féminins depuis zvukogram.com
et construit un catalogue avec tags pour Eva.

Usage :
    python zvukogram_scraper.py               # télécharge tout
    python zvukogram_scraper.py --catalog     # affiche le catalogue existant
    python zvukogram_scraper.py --test-pick "прости занята" # simule un pick Eva
"""

import asyncio
import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime

import httpx

# ─── Config ───────────────────────────────────────────────────

BASE_URL   = "https://zvukogram.com"
VOICES_DIR = Path("voices")
CATALOG    = Path("voice_catalog.json")

CATEGORIES = [
    ("pak-golos-devushki",   "female_pack"),    # 65 phrases Eva
    ("zvuki-jenskih-golosov","female_voices"),   # sons féminins supplémentaires
]

# Filtres — on ne garde pas les sons hors sujet pour une conversation dating
TITLE_BLACKLIST = re.compile(
    r"кри[кч]|вопль|истери|пение|Новый год|чиханье|мольба|невнятн",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://zvukogram.com/",
}

# ─── Tags automatiques ────────────────────────────────────────

TAG_RULES: list[tuple[str, re.Pattern]] = [
    ("greeting",  re.compile(r"привет|доброе утро|добрый (вечер|день)|доброго времени", re.I)),
    ("bye",       re.compile(r"пока|до завтра|спокойной ночи|сладких снов|пошлых снов", re.I)),
    ("confirm",   re.compile(r"^(да|хорошо|договорились|давай|буду ждать|ясно|понятно)", re.I)),
    ("deny",      re.compile(r"^нет|не могу|не поняла|не угадал|не доверяю", re.I)),
    ("flirt",     re.compile(r"хочешь меня|люблю|нравишься|пошалим|грудь|попа|хочу тебя|чмоки|пошлых", re.I)),
    ("question",  re.compile(r"как дела|что (делаешь|сейчас)|в чём|как думаешь|поможешь|посоветуй|подскажи|скинь|фото", re.I)),
    ("reaction",  re.compile(r"обидно|жаль|ой.?все|обиделась|интересно|новое|блин", re.I)),
    ("busy",      re.compile(r"занята|долго не отвечала|ногти|книгу|гулять|плохо|болит|смотрю|устала", re.I)),
    ("affection", re.compile(r"люблю|нравишься|тебя|сильно", re.I)),
    ("casual",    re.compile(r".", re.I)),   # fallback — tout le monde reçoit ce tag
]

def auto_tag(title: str) -> list[str]:
    tags = []
    for tag, pattern in TAG_RULES:
        if pattern.search(title):
            tags.append(tag)
            if len(tags) >= 4:
                break
    return tags or ["casual"]

# ─── Logs ─────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(tag, msg):
    print(f"[{ts()}] [{tag}] {msg}", flush=True)

# ─── Scraping ─────────────────────────────────────────────────

async def fetch_category(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Récupère tous les sons d'une catégorie (toutes les pages)."""
    url     = f"{BASE_URL}/category/{slug}/"
    tracks  = []
    page    = 1

    while True:
        page_url = url if page == 1 else f"{url}page/{page}/"
        log("GET", f"Fetching {page_url}")
        try:
            r = await client.get(page_url, headers=HEADERS)
        except Exception as e:
            log("ERR", f"Requête échouée : {e}")
            break

        if r.status_code != 200:
            log("WRN", f"HTTP {r.status_code} pour {page_url}")
            break

        # Parse sans BeautifulSoup pour éviter la dépendance
        found = _parse_tracks(r.text)
        if not found:
            break

        tracks.extend(found)
        log("OK ", f"Page {page} — {len(found)} sons")

        # Vérifie s'il y a une page suivante
        if not re.search(rf'/category/{slug}/page/{page+1}/', r.text):
            break
        page += 1

    return tracks

def _parse_tracks(html: str) -> list[dict]:
    """Extrait les blocs data-track depuis le HTML brut."""
    # Trouve tous les éléments avec data-track
    blocks = re.finditer(
        r'data-id="(\d+)"[^>]*data-track="(/mp3/[^"]+)"',
        html,
    )
    tracks = []
    for m in blocks:
        track_id   = m.group(1)
        track_path = m.group(2)

        # Cherche le waveTitle dans les 500 chars qui suivent
        start = m.end()
        chunk = html[start:start + 500]
        title_m = re.search(r'class="waveTitle">([^<]+)<', chunk)
        title = title_m.group(1).strip() if title_m else track_path.split("/")[-1].replace("-", " ")

        tracks.append({
            "id":    track_id,
            "track": track_path,
            "title": title,
        })
    return tracks

# ─── Téléchargement ───────────────────────────────────────────

async def download_mp3(client: httpx.AsyncClient, track_path: str, dest: Path) -> bool:
    """Télécharge un fichier MP3 si non déjà présent."""
    if dest.exists() and dest.stat().st_size > 0:
        return True   # déjà téléchargé

    url = BASE_URL + track_path
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 500:
            dest.write_bytes(r.content)
            return True
        log("WRN", f"Echec download {url} — HTTP {r.status_code} / {len(r.content)} bytes")
        return False
    except Exception as e:
        log("ERR", f"Download {url} : {e}")
        return False

# ─── Pipeline principal ───────────────────────────────────────

async def run_scraper():
    VOICES_DIR.mkdir(exist_ok=True)

    # Charge catalogue existant pour éviter re-téléchargements
    existing: dict[str, dict] = {}
    if CATALOG.exists():
        for entry in json.loads(CATALOG.read_text(encoding="utf-8")):
            existing[entry["id"]] = entry
    log("CAT", f"{len(existing)} entrées déjà dans le catalogue")

    catalog: list[dict] = list(existing.values())
    existing_ids = set(existing.keys())

    async with httpx.AsyncClient(verify=False, timeout=20) as client:
        for slug, label in CATEGORIES:
            log("CAT", f"Scraping catégorie : {slug}")
            raw_tracks = await fetch_category(client, slug)
            log("CAT", f"{len(raw_tracks)} sons trouvés dans {slug}")

            new_count = ok_count = skip_count = 0
            for track in raw_tracks:
                if TITLE_BLACKLIST.search(track["title"]):
                    skip_count += 1
                    continue

                if track["id"] in existing_ids:
                    ok_count += 1
                    continue

                fname = track["track"].split("/")[-1]
                dest  = VOICES_DIR / fname

                ok = await download_mp3(client, track["track"], dest)
                if not ok:
                    continue

                entry = {
                    "id":         track["id"],
                    "file":       str(dest),
                    "filename":   fname,
                    "transcript": track["title"],
                    "category":   label,
                    "tags":       auto_tag(track["title"]),
                }
                catalog.append(entry)
                existing_ids.add(track["id"])
                new_count += 1

                log("DL ", f"[{new_count:>3}] {track['title'][:45]:<45}  →  {fname}")

            log("OK ", f"{slug} — {new_count} nouveaux, {ok_count} déjà présents, {skip_count} filtrés")

    # Sauvegarde catalogue
    CATALOG.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log("CAT", f"Catalogue sauvegardé : {len(catalog)} entrées  →  {CATALOG}")
    return catalog

# ─── CLI ──────────────────────────────────────────────────────

def show_catalog():
    if not CATALOG.exists():
        print("Pas de catalogue. Lance d'abord le scraper.")
        return
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    print(f"\n{len(data)} vocaux dans le catalogue\n")
    print(f"{'ID':>6}  {'Tags':<30}  Transcript")
    print("-" * 80)
    for e in data:
        tags = ", ".join(e.get("tags", []))
        print(f"{e['id']:>6}  {tags:<30}  {e['transcript']}")

def test_pick(query: str):
    """Simule la sélection d'un vocal Eva pour un texte donné."""
    from zvukogram_agent import pick_voice_for
    result = pick_voice_for(query)
    if result:
        print(f"\nMeilleur match pour : \"{query}\"")
        print(f"  → {result['transcript']}  [{', '.join(result['tags'])}]  ({result['filename']})")
    else:
        print("Aucun match trouvé.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZvukoGram Scraper pour Eva")
    parser.add_argument("--catalog",   action="store_true", help="Affiche le catalogue")
    parser.add_argument("--test-pick", metavar="TEXTE",     help="Simule un pick Eva")
    args = parser.parse_args()

    if args.catalog:
        show_catalog()
    elif args.test_pick:
        test_pick(args.test_pick)
    else:
        asyncio.run(run_scraper())
