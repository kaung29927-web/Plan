import asyncio
import base64
import json
import os
import re
import time

import aiohttp
import cv2
import ddddocr
import numpy as np
from telebot import types
from telebot.async_telebot import AsyncTeleBot

# ========================= EDITABLE CONFIGURATION =========================
# Test configuration is kept in config_local.py so GitHub can keep this repo public.
# Edit config_local.py when switching the bot or repository.
try:
    from config_local import (
        BOT_TOKEN,
        GITHUB_TOKEN,
        GITHUB_OWNER,
        GITHUB_REPO,
        GITHUB_FILE,
        GITHUB_BRANCH,
    )
except ImportError:
    BOT_TOKEN = ""
    GITHUB_TOKEN = ""
    GITHUB_OWNER = "kaung29927-web"
    GITHUB_REPO = "Plan"
    GITHUB_FILE = "plan_check_saved_urls.json"
    GITHUB_BRANCH = "main"

PORTAL = "https://portal-as.ruijienetworks.com"
VOUCHER_URL = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
BALANCE_URL = "https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{session_id}"
CAPTCHA_IMAGE_URL = "https://portal-as.ruijienetworks.com/api/auth/captcha/image"
CAPTCHA_VERIFY_URL = "https://portal-as.ruijienetworks.com/api/auth/captcha/verify"

TEXT = {
    "start": "Plan Check Bot\n\n➕ URL အသစ်ထည့်ရန် Add URL ကိုနှိပ်ပါ။\n🔍 သိမ်းထားတဲ့ URL နဲ့ စစ်ရန် Check ကိုနှိပ်ပါ။",
    "add_url": "သိမ်းမယ့် Session URL ပို့ပေးပါ။\nရပ်ရန် /cancel ကိုသုံးပါ။",
    "add_name": "ဒီ URL အတွက် သိမ်းမယ့် name ပို့ပေးပါ။",
    "check_empty": "သိမ်းထားတဲ့ URL မရှိသေးပါ။ ပထမဆုံး Add URL ကိုနှိပ်ပါ။",
    "choose_url": "စစ်လိုတဲ့ saved URL ကို ရွေးပါ။",
    "voucher": "Voucher code ပို့ပေးပါ။",
    "checking": "စစ်ဆေးနေပါတယ်။ ခဏစောင့်ပါ...",
    "cancelled": "လုပ်ဆောင်မှုကို ရပ်လိုက်ပါပြီ။",
    "saved": "✅ သိမ်းပြီးပါပြီ။\nName: {name}",
    "invalid_url": "မှန်ကန်တဲ့ http/https Session URL ပို့ပေးပါ။",
    "no_session": "Session ID မရပါ။ Saved URL ကိုပြန်စစ်ပြီး ထပ်လုပ်ပါ။",
    "captcha_failed": "CAPTCHA verify မအောင်မြင်ပါ။ ထပ်စမ်းကြည့်ပါ။",
    "success": "✅ Success\n🎫 {code}\n{plan}",
    "limited": "⚠️ Limited code\n🎫 {code}",
    "invalid_code": "❌ Code မမှန်ပါ သို့မဟုတ် စစ်ဆေးမရပါ။",
    "error": "စစ်ဆေးနေစဉ် အမှားတစ်ခုဖြစ်ပါသည်။ နောက်မှ ထပ်စမ်းပါ။",
    "start_first": "/start ကိုနှိပ်ပြီး စတင်ပါ။",
}
BUTTON_ADD = "➕ Add URL"
BUTTON_CHECK = "🔍 Check"
BUTTON_ADD_AGAIN = "➕ Add URL"

if not BOT_TOKEN:
    raise RuntimeError("PLAN_CHECK_BOT_TOKEN is not set")
if not GITHUB_TOKEN:
    raise RuntimeError("PLAN_CHECK_GITHUB_TOKEN is not set")

bot = AsyncTeleBot(BOT_TOKEN)
user_state = {}
storage_lock = asyncio.Lock()
ocr = ddddocr.DdddOcr(show_ad=False)


# =============================== GITHUB DATA ===============================
def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_file_url():
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_FILE}"


async def github_read_urls(session):
    async with session.get(github_file_url(), headers=github_headers(), params={"ref": GITHUB_BRANCH}) as response:
        if response.status == 404:
            return {}, None
        response.raise_for_status()
        payload = await response.json()
    raw = base64.b64decode(payload.get("content", "")).decode("utf-8")
    data = json.loads(raw) if raw.strip() else {}
    return (data if isinstance(data, dict) else {}), payload.get("sha")


async def github_write_urls(session, data, sha=None):
    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("ascii")
    payload = {
        "message": "Update saved plan-check URLs",
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    async with session.put(github_file_url(), headers=github_headers(), json=payload) as response:
        if response.status not in (200, 201):
            body = await response.text()
            raise RuntimeError(f"GitHub save failed: HTTP {response.status} {body[:200]}")


async def add_saved_url(user_id, name, url):
    timeout = aiohttp.ClientTimeout(total=20)
    async with storage_lock:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data, sha = await github_read_urls(session)
            records = data.setdefault(str(user_id), [])
            records.append({"name": name, "url": url})
            await github_write_urls(session, data, sha)
            return len(records) - 1


async def get_user_urls(user_id):
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data, _ = await github_read_urls(session)
    records = data.get(str(user_id), [])
    return records if isinstance(records, list) else []


# ============================== PORTAL CHECK ===============================
def minutes_to_time(value):
    if value == "Unknown":
        return "Unknown"
    try:
        total = int(value)
    except (TypeError, ValueError):
        return "Unknown"
    if total == 0:
        return "Unlimited"
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def replace_mac(url, mac):
    return re.sub(r"(?<=mac=)[^&]+", mac, url)


def random_mac():
    return ":".join(["02"] + [f"{os.urandom(1)[0]:02x}" for _ in range(5)])


async def get_session_id(session, session_url):
    session_url = replace_mac(session_url, random_mac())
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36",
    }
    async with session.get(session_url, headers=headers, allow_redirects=True) as response:
        final_url = str(response.url)
    match = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", final_url)
    return match.group(1) if match else None


def preprocess_ocr(image_bytes):
    array = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return ""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresholded = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, encoded = cv2.imencode(".png", thresholded)
    return ocr.classification(encoded.tobytes()).upper()


async def verify_captcha(session, session_id):
    image_headers = {
        "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36",
    }
    for _ in range(8):
        async with session.get(CAPTCHA_IMAGE_URL, params={"sessionId": session_id, "_t": str(time.time())}, headers=image_headers) as response:
            image_bytes = await response.read()
        text = await asyncio.to_thread(preprocess_ocr, image_bytes)
        if not text:
            continue
        verify_headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": PORTAL,
            "user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36",
        }
        async with session.post(CAPTCHA_VERIFY_URL, headers=verify_headers, json={"sessionId": session_id, "authCode": text}) as response:
            data = await response.json()
        if data.get("success") is True:
            return text
    return None


async def get_plan(session, session_id, code, auth_code):
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": PORTAL,
        "user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36",
    }
    payload = {"accessCode": code, "sessionId": session_id, "apiVersion": 1, "authCode": auth_code}
    async with session.post(VOUCHER_URL, json=payload, headers=headers) as response:
        raw = await response.text()
    if "logonUrl" not in raw:
        return ("LIMITED", None) if "STA" in raw else ("INVALID", None)

    balance_headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/json;",
        "referer": f"{PORTAL}/download/static/maccauth/src/balance.html",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36",
    }
    async with session.get(BALANCE_URL.format(session_id=session_id), headers=balance_headers) as response:
        data = await response.json()
    result = data.get("result", {})
    profile = result.get("profileName", "Unknown")
    total_minutes = result.get("totalMinutes", "Unknown")
    return "SUCCESS", f"📋 Plan: {profile} | ⏳ Time: {minutes_to_time(total_minutes)}"


# ================================ BOT UI ===================================
def main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(BUTTON_ADD, callback_data="menu:add"),
        types.InlineKeyboardButton(BUTTON_CHECK, callback_data="menu:check"),
    )
    return markup


@bot.message_handler(commands=["start"])
async def start(message):
    user_state.pop(message.chat.id, None)
    await bot.send_message(message.chat.id, TEXT["start"], reply_markup=main_keyboard())


@bot.message_handler(commands=["add"])
async def add_command(message):
    user_state[message.chat.id] = {"step": "add_url"}
    await bot.send_message(message.chat.id, TEXT["add_url"])


@bot.message_handler(commands=["check"])
async def check_command(message):
    await show_saved_urls(message.chat.id)


async def show_saved_urls(chat_id):
    try:
        records = await get_user_urls(chat_id)
    except Exception as exc:
        print(f"GitHub read error: {type(exc).__name__}: {exc}")
        await bot.send_message(chat_id, "GitHub မှ saved URL များဖတ်မရပါ။ Config/token ကိုစစ်ပါ။")
        return
    if not records:
        await bot.send_message(chat_id, TEXT["check_empty"], reply_markup=main_keyboard())
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, record in enumerate(records):
        name = str(record.get("name", f"URL {index + 1}"))[:55]
        markup.add(types.InlineKeyboardButton(f"🔗 {name}", callback_data=f"saved:{index}"))
    markup.add(types.InlineKeyboardButton(BUTTON_ADD_AGAIN, callback_data="menu:add"))
    await bot.send_message(chat_id, TEXT["choose_url"], reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ("menu:add", "menu:check"))
async def menu_callback(call):
    await bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    if call.data == "menu:add":
        user_state[chat_id] = {"step": "add_url"}
        await bot.send_message(chat_id, TEXT["add_url"])
    else:
        await show_saved_urls(chat_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("saved:"))
async def saved_url_callback(call):
    await bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    try:
        index = int(call.data.split(":", 1)[1])
        records = await get_user_urls(chat_id)
        record = records[index]
    except (ValueError, IndexError, TypeError, KeyError):
        await bot.send_message(chat_id, "ဒီ saved URL ကို မတွေ့တော့ပါ။ /check နဲ့ ပြန်ဖွင့်ပါ။")
        return
    user_state[chat_id] = {"step": "voucher", "session_url": record["url"], "name": record["name"]}
    await bot.send_message(chat_id, f"ရွေးထားသော URL: {record['name']}\n{TEXT['voucher']}")


@bot.message_handler(commands=["cancel"])
async def cancel(message):
    user_state.pop(message.chat.id, None)
    await bot.send_message(message.chat.id, TEXT["cancelled"], reply_markup=main_keyboard())


@bot.message_handler(content_types=["text"])
async def text_handler(message):
    chat_id = message.chat.id
    state = user_state.get(chat_id)
    if not state:
        await bot.send_message(chat_id, TEXT["start_first"], reply_markup=main_keyboard())
        return

    text = message.text.strip()
    if state["step"] == "add_url":
        if not text.startswith(("http://", "https://")):
            await bot.send_message(chat_id, TEXT["invalid_url"])
            return
        state.update({"step": "add_name", "url": text})
        await bot.send_message(chat_id, TEXT["add_name"])
        return

    if state["step"] == "add_name":
        name = text[:80]
        try:
            await add_saved_url(chat_id, name, state["url"])
            user_state.pop(chat_id, None)
            await bot.send_message(chat_id, TEXT["saved"].format(name=name), reply_markup=main_keyboard())
        except Exception as exc:
            print(f"GitHub write error: {type(exc).__name__}: {exc}")
            await bot.send_message(chat_id, "URL သိမ်းမရပါ။ GitHub config/token/repository permission ကိုစစ်ပါ။")
        return

    code = text
    await bot.send_message(chat_id, TEXT["checking"])
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            session_id = await get_session_id(session, state["session_url"])
            if not session_id:
                await bot.send_message(chat_id, TEXT["no_session"])
                return
            auth_code = await verify_captcha(session, session_id)
            if not auth_code:
                await bot.send_message(chat_id, TEXT["captcha_failed"])
                return
            status, plan = await get_plan(session, session_id, code, auth_code)
            if status == "SUCCESS":
                await bot.send_message(chat_id, TEXT["success"].format(code=code, plan=plan))
            elif status == "LIMITED":
                await bot.send_message(chat_id, TEXT["limited"].format(code=code))
            else:
                await bot.send_message(chat_id, TEXT["invalid_code"])
    except Exception as exc:
        print(f"check error: {type(exc).__name__}: {exc}")
        await bot.send_message(chat_id, TEXT["error"])
    finally:
        user_state.pop(chat_id, None)


async def main():
    while True:
        try:
            await bot.polling(non_stop=True, timeout=60, request_timeout=60)
        except Exception as exc:
            print(f"polling error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
