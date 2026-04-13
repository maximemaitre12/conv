"""Génère les vocaux Eva via OpenAI TTS — reprend là où ça s'est arrêté."""
import json, os, re, sys, io, httpx
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

load_dotenv(Path(".env"))
client_ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), http_client=httpx.Client(verify=False))

BLACKLIST = re.compile(r"кри[кч]|вопль|истери|пение|Новый год|чиханье|мольба|невнятн", re.I)

TAG_RULES = [
    ("greeting", re.compile(r"привет|доброе утро|добрый (вечер|день)|доброго времени", re.I)),
    ("bye",      re.compile(r"пока|до завтра|спокойной ночи|сладких снов|пошлых снов", re.I)),
    ("confirm",  re.compile(r"^(да|хорошо|договорились|давай|буду ждать|ясно|понятно)", re.I)),
    ("deny",     re.compile(r"^нет|не могу|не поняла|не угадал|не доверяю", re.I)),
    ("flirt",    re.compile(r"хочешь меня|люблю|нравишься|пошалим|грудь|попа|хочу тебя|чмоки|пошлых", re.I)),
    ("question", re.compile(r"как дела|что (делаешь|сейчас)|как думаешь|поможешь|посоветуй|подскажи|фото", re.I)),
    ("reaction", re.compile(r"обидно|жаль|ой.?все|обиделась|интересно|новое|блин", re.I)),
    ("busy",     re.compile(r"занята|долго не отвечала|ногти|книгу|гулять|плохо|болит|смотрю|устала", re.I)),
    ("casual",   re.compile(r".", re.I)),
]

def auto_tag(t):
    tags = []
    for tag, pat in TAG_RULES:
        if pat.search(t):
            tags.append(tag)
            if len(tags) >= 4:
                break
    return tags or ["casual"]

def clean_tts(t):
    t = re.sub(r'\([^)]*\)', '', t).strip()
    t = re.sub(r'\[[^\]]*\]', '', t).strip()
    return t.strip('.,!? ') or t

Path("voices").mkdir(exist_ok=True)
raw    = json.loads(Path("tracks_raw.json").read_text(encoding="utf-8"))
tracks = [t for t in raw if not BLACKLIST.search(t["title"])]

print(f"Génération de {len(tracks)} vocaux (nova voice)...\n")

catalog = []
ok = err = cached = 0

for i, track in enumerate(tracks, 1):
    transcript = track["title"]
    tts_text   = clean_tts(transcript)
    slug       = track["track"].split("/")[-1].replace(".mp3", "")
    dest       = Path("voices") / f"{slug}.mp3"

    if dest.exists() and dest.stat().st_size > 1000:
        cached += 1
        print(f"[{i:>2}/{len(tracks)}] CACHE  {transcript[:50]}")
    else:
        try:
            resp = client_ai.audio.speech.create(
                model="tts-1",
                voice="nova",
                input=tts_text,
                response_format="mp3",
                speed=0.93,
            )
            dest.write_bytes(resp.content)
            ok += 1
            print(f"[{i:>2}/{len(tracks)}] OK {len(resp.content)//1024:>3}kb  {transcript[:50]}")
        except Exception as e:
            err += 1
            print(f"[{i:>2}/{len(tracks)}] ERR    {transcript[:40]} -> {e}")

    catalog.append({
        "id":         track["id"],
        "file":       str(dest),
        "filename":   dest.name,
        "transcript": transcript,
        "tts_text":   tts_text,
        "category":   "female_pack" if "1348" in track["track"] else "female_voices",
        "tags":       auto_tag(transcript),
    })
    sys.stdout.flush()

Path("voice_catalog.json").write_text(
    json.dumps(catalog, ensure_ascii=False, indent=2),
    encoding="utf-8"
)
print(f"\nTerminé : {ok} générés, {cached} en cache, {err} erreurs")
print(f"Catalogue : {len(catalog)} entrées  →  voice_catalog.json")
