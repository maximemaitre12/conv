import urllib.request
import json
import os
import time

VOICES_DIR = "voices"
os.makedirs(VOICES_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://zvukipro.com/",
}

TRACKS = [
    {"id": 45361, "transcript": "Приветик"},
    {"id": 45347, "transcript": "Доброго времени суток"},
    {"id": 45348, "transcript": "Доброе утро"},
    {"id": 45352, "transcript": "Ой, добрый вечер"},
    {"id": 45368, "transcript": "Че делаешь"},
    {"id": 45353, "transcript": "Как дела?"},
    {"id": 45354, "transcript": "Я книгу читаю, а ты?"},
    {"id": 45369, "transcript": "Ногти крашу, а ты чем занят?"},
    {"id": 45357, "transcript": "Не поняла"},
    {"id": 45356, "transcript": "Да (грустным голосом)"},
    {"id": 45387, "transcript": "Подожди"},
    {"id": 45367, "transcript": "Блин, я так устала, целый день на ногах"},
    {"id": 45381, "transcript": "Почему ты мне смайлики не шлешь?"},
    {"id": 45355, "transcript": "Может пошалим?"},
    {"id": 45382, "transcript": "Почему, я тебе не нравлюсь?"},
    {"id": 45338, "transcript": "В чем ты сейчас одет?"},
    {"id": 45341, "transcript": "Ясно"},
    {"id": 45371, "transcript": "Хочешь меня?"},
    {"id": 45350, "transcript": "Ой расскажи, мне это интересно"},
    {"id": 45343, "transcript": "М-м-м, какой большой, хочу тебя"},
    {"id": 45349, "transcript": "Для меня это что-то новое"},
    {"id": 45391, "transcript": "Может фоточку скинешь, я хочу его увидеть"},
    {"id": 45388, "transcript": "Как думаешь, что сейчас на мне?"},
    {"id": 45379, "transcript": "Не угадал"},
    {"id": 45342, "transcript": "Как тебе моя попа?"},
    {"id": 45339, "transcript": "А грудь нравится?"},
    {"id": 45358, "transcript": "Очень жаль"},
    {"id": 45364, "transcript": "Обидно"},
    {"id": 45363, "transcript": "Та нет (грустно)"},
    {"id": 45365, "transcript": "Я обиделась"},
    {"id": 45377, "transcript": "К сожалению не могу"},
    {"id": 45390, "transcript": "Нет"},
    {"id": 45380, "transcript": "Ой всё"},
    {"id": 45351, "transcript": "Ну, прости, я сейчас немного занята, я позже напишу"},
    {"id": 45394, "transcript": "Прости, что долго не отвечала"},
    {"id": 45337, "transcript": "А ты меня любишь?"},
    {"id": 45360, "transcript": "Сильно-сильно?"},
    {"id": 45340, "transcript": "Я тебе не доверяю"},
    {"id": 45366, "transcript": "И я тебя люблю"},
    {"id": 45372, "transcript": "Хорошо (с грустным вздохом)"},
    {"id": 45359, "transcript": "Подскажи, какой сериал можно посмотреть интересненький"},
    {"id": 45373, "transcript": "Ой щас фильм смотрю"},
    {"id": 45362, "transcript": "Можешь мне парнушку посоветовать интересную"},
    {"id": 45386, "transcript": "Можешь фильм посоветовать"},
    {"id": 45375, "transcript": "О скинь музыки"},
    {"id": 45374, "transcript": "Ну как скинешь, обязательно отпишись"},
    {"id": 45376, "transcript": "Хорошо?"},
    {"id": 45378, "transcript": "Буду ждать"},
    {"id": 45389, "transcript": "Давай буду ждать"},
    {"id": 45383, "transcript": "Договорились"},
    {"id": 45393, "transcript": "Хорошо"},
    {"id": 45384, "transcript": "Голова болит"},
    {"id": 45345, "transcript": "До завтра"},
    {"id": 45344, "transcript": "Пока-пока"},
    {"id": 45346, "transcript": "Пошлых снов"},
    {"id": 45370, "transcript": "Чмоки-чмоки"},
    {"id": 45392, "transcript": "Спокойной ночи"},
    {"id": 45385, "transcript": "Сладких снов"},
]

catalog = []
ok = 0
fail = 0

for track in TRACKS:
    url = f"https://zvukipro.com/index.php?do=download&id={track['id']}"
    filename = f"{track['id']}.mp3"
    dest = os.path.join(VOICES_DIR, filename)

    if os.path.exists(dest) and os.path.getsize(dest) > 500:
        print(f"[SKIP] {filename} — déjà présent")
        ok += 1
    else:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) > 500:
                with open(dest, "wb") as f:
                    f.write(data)
                ok += 1
                print(f"[OK  ] {filename} — {track['transcript']} ({len(data)} bytes)")
            else:
                fail += 1
                print(f"[ERR ] {filename} — trop petit ({len(data)} bytes)")
                dest = None
        except Exception as e:
            fail += 1
            print(f"[ERR ] {filename} — {e}")
            dest = None

    if dest and os.path.exists(dest):
        catalog.append({
            "id": track["id"],
            "filename": filename,
            "file": dest,
            "transcript": track["transcript"],
            "source": "zvukipro.com",
        })

    time.sleep(0.15)

with open("voice_catalog.json", "w", encoding="utf-8") as f:
    json.dump(catalog, f, ensure_ascii=False, indent=2)

print(f"\nTerminé : {ok} OK, {fail} échecs")
print(f"Catalogue sauvegardé : {len(catalog)} entrées")
