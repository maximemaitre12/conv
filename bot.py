"""
Bot Léa — Happn → WhatsApp Web
Connexion à Chrome via CDP (remote debugging)
"""

import asyncio
import re
import httpx
import os
from playwright.async_api import async_playwright
from openai import OpenAI
from dotenv import load_dotenv
from soul import SOUL

load_dotenv()

CDP_URL = "http://localhost:9222"
POLL_INTERVAL = 2  # secondes entre chaque vérification

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(verify=False),
)

conversation_history = []
last_seen_message = None
whatsapp_number = None


# ─── Utilitaires ──────────────────────────────────────────────

def extract_phone_number(text):
    match = re.search(r'(\+?[\d][\d\s\-\(\)]{6,14}[\d])', text)
    if match:
        num = re.sub(r'[\s\-\(\)]', '', match.group(1))
        if len(num) >= 8:
            return num
    return None


async def get_lea_response(user_message):
    global conversation_history, whatsapp_number

    number = extract_phone_number(user_message)
    if number:
        whatsapp_number = number
        print(f"[BOT] Numéro détecté : {number}")

    conversation_history.append({"role": "user", "content": user_message})
    messages = [{"role": "system", "content": SOUL}] + conversation_history

    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=400,
        messages=messages,
    )

    reply = response.choices[0].message.content
    conversation_history.append({"role": "assistant", "content": reply})
    return reply


async def find_page(context, url_fragment):
    for page in context.pages:
        if url_fragment in page.url:
            return page
    return None


# ─── Happn ────────────────────────────────────────────────────

async def get_last_happn_message(page):
    """Extrait le dernier message reçu sur Happn via JS injection"""
    return await page.evaluate("""
        () => {
            const candidates = [
                '[class*="received"] [class*="content"]',
                '[class*="MessageReceived"]',
                '[class*="message-received"]',
                '[class*="incoming"] [class*="text"]',
                '[class*="other"] [class*="message"]',
            ];

            for (const sel of candidates) {
                const els = document.querySelectorAll(sel);
                if (els.length > 0) {
                    const last = els[els.length - 1];
                    const text = (last.innerText || last.textContent || '').trim();
                    if (text) return { found: true, text, selector: sel };
                }
            }

            // Fallback large
            const all = [...document.querySelectorAll('[class*="message"], [class*="Message"]')];
            const withText = all.filter(el => {
                const t = (el.innerText || '').trim();
                return t.length > 0 && t.length < 500;
            });
            if (withText.length > 0) {
                const last = withText[withText.length - 1];
                return { found: true, text: (last.innerText || '').trim(), selector: 'fallback' };
            }

            return { found: false };
        }
    """)


async def send_happn_message(page, text):
    """Tape et envoie un message dans le chat Happn ouvert"""
    input_selectors = [
        'textarea[name="message"]',
        'textarea[placeholder*="essage" i]',
        'div[contenteditable="true"][class*="input"]',
        'div[contenteditable="true"][class*="message"]',
        'div[contenteditable="true"][class*="editor"]',
        'div[contenteditable="true"]',
        'textarea',
    ]

    for sel in input_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=600):
                await el.click()
                await asyncio.sleep(0.4)
                await page.keyboard.type(text, delay=28)
                await asyncio.sleep(0.6)
                await page.keyboard.press("Enter")
                return True
        except Exception:
            continue

    print("[HAPPN] ⚠ Input non trouvé — lance python bot.py --debug pour identifier les sélecteurs")
    return False


# ─── WhatsApp Web ─────────────────────────────────────────────

async def get_last_whatsapp_message(page):
    return await page.evaluate("""
        () => {
            const msgs = document.querySelectorAll(
                'div[class*="message-in"] span[class*="selectable-text"], ' +
                'div[data-id] [class*="copyable-text"] span[class*="selectable-text"]'
            );
            if (!msgs.length) return null;
            return (msgs[msgs.length - 1].innerText || '').trim();
        }
    """)


async def send_whatsapp_message(page, text):
    input_selectors = [
        'div[contenteditable="true"][data-tab="10"]',
        'div[title="Type a message"]',
        'div[title="Message"]',
        'footer div[contenteditable="true"]',
    ]

    for sel in input_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.click()
                await asyncio.sleep(0.3)
                await page.keyboard.type(text, delay=28)
                await asyncio.sleep(0.5)
                await page.keyboard.press("Enter")
                return True
        except Exception:
            continue

    print("[WHATSAPP] ⚠ Input non trouvé")
    return False


async def open_whatsapp_chat(page, number):
    """Ouvre une conversation WhatsApp avec un numéro"""
    url = f"https://web.whatsapp.com/send?phone={number}"
    await page.goto(url)
    await asyncio.sleep(5)  # Attendre le chargement


# ─── Boucles principales ──────────────────────────────────────

async def happn_loop(happn_page):
    global last_seen_message, whatsapp_number

    print("[BOT] Surveillance Happn démarrée...\n")

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        if whatsapp_number:
            return  # Passer à WhatsApp

        try:
            result = await get_last_happn_message(happn_page)

            if not result or not result.get("found"):
                continue

            msg_text = (result.get("text") or "").strip()

            if not msg_text or msg_text == last_seen_message:
                continue

            last_seen_message = msg_text
            print(f"[HAPPN ← ] {msg_text}")

            # Délai lecture réaliste
            await asyncio.sleep(1.2 + len(msg_text) * 0.025)

            reply = await get_lea_response(msg_text)
            print(f"[HAPPN → ] {reply}\n")

            await send_happn_message(happn_page, reply)

        except Exception as e:
            print(f"[BOT] Erreur Happn : {e}")
            await asyncio.sleep(3)


async def whatsapp_loop(wa_page, number):
    global conversation_history

    print(f"\n[BOT] Passage sur WhatsApp → {number}")
    await open_whatsapp_chat(wa_page, number)

    # Premier message — Léa se présente comme la même personne
    first_msg = await get_lea_response(
        "[SYSTÈME: L'utilisateur vient de te donner son numéro. "
        "Tu l'as ajouté sur WhatsApp. Envoie un tout premier message "
        "ultra court et naturel — fais le lien que c'est toi, la même Léa. "
        "Pas de grande explication, juste une phrase simple.]"
    )
    print(f"[WHATSAPP → ] {first_msg}\n")
    await send_whatsapp_message(wa_page, first_msg)

    last_wa_message = None

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        try:
            msg = await get_last_whatsapp_message(wa_page)

            if not msg or msg == last_wa_message:
                continue

            last_wa_message = msg
            print(f"[WHATSAPP ← ] {msg}")

            await asyncio.sleep(1.2 + len(msg) * 0.025)

            reply = await get_lea_response(msg)
            print(f"[WHATSAPP → ] {reply}\n")

            await send_whatsapp_message(wa_page, reply)

        except Exception as e:
            print(f"[BOT] Erreur WhatsApp : {e}")
            await asyncio.sleep(3)


# ─── Entrée ───────────────────────────────────────────────────

async def main():
    async with async_playwright() as p:
        print("[BOT] Connexion à Chrome...")

        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception:
            print("\n[ERREUR] Chrome inaccessible.")
            print("Lance Chrome avec cette commande d'abord :\n")
            print('  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\\chrome-debug\n')
            return

        context = browser.contexts[0]
        pages = context.pages
        print(f"[BOT] {len(pages)} onglet(s) ouvert(s) : {[p.url[:50] for p in pages]}\n")

        happn_page = await find_page(context, "happn")
        wa_page = await find_page(context, "whatsapp")

        if not happn_page:
            print("[ERREUR] Onglet Happn introuvable. Ouvre happn.com d'abord.")
            return
        if not wa_page:
            print("[ERREUR] Onglet WhatsApp Web introuvable. Ouvre web.whatsapp.com d'abord.")
            return

        print("[BOT] Happn ✓")
        print("[BOT] WhatsApp Web ✓\n")

        # Phase 1 : Happn
        await happn_loop(happn_page)

        # Phase 2 : WhatsApp (déclenché quand numéro détecté)
        if whatsapp_number:
            await whatsapp_loop(wa_page, whatsapp_number)


if __name__ == "__main__":
    asyncio.run(main())
