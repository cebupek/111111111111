"""
Бот модерации музыкального сайта — Telegram Mini App версия
Установка: pip install python-telegram-bot==21.3 fastapi uvicorn httpx
Запуск:    python music_bot.py

Как работает:
  1. Бот шлёт каждому модератору личное уведомление с кнопкой "Открыть панель"
  2. Кнопка открывает Mini App (app.html) прямо внутри Telegram
  3. Модератор видит очередь, слушает треки, принимает/отклоняет
  4. Все действия через Mini App → этот сервер → API твоего сайта
"""

import asyncio, logging, httpx, hashlib, hmac, json, urllib.parse, os, datetime
from collections import deque
from pathlib import Path
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ============================================================
#  👇 ЗАПОЛНИ ТОЛЬКО ЭТИ СТРОЧКИ
# ============================================================

BOT_TOKEN = "8566474882:AAHfufmlEeW0XmkX_y4IDL6Tcwj52D6Eaa8"
MOD_IDS = [7628577301, 222222, 333333]
APP_URL = "https://твой-домен.com"
SITE_URL = "https://твой-сайт.com"

# ============================================================
#  Это можно не трогать
# ============================================================

SECRET   = "bot_secret_key_2024"
PORT     = int(os.environ.get("PORT", 8000))
INTERVAL = 60

RULES_TEXT = """📋 <b>Правила модерации треков</b>
...
(оставляем без изменений)
"""

REJECT_REASONS = {
    "song": [
        {"label": "🔇 Не трек (голосовое/вырезка)", "code": "not_a_track"},
        {"label": "🤬 Неприемлемый контент",         "code": "bad_content"},
        {"label": "⚡ Политический подтекст",         "code": "political"},
        {"label": "📋 Авторские права",               "code": "copyright"},
        {"label": "⏱ Некорректная длина",             "code": "bad_length"},
        {"label": "🔁 Дубликат",                      "code": "duplicate"},
        {"label": "✏️ Другая причина",                "code": "other"},
    ],
    "name": [
        {"label": "🤬 Оскорбительное название", "code": "offensive"},
        {"label": "⚡ Политический подтекст",   "code": "political"},
        {"label": "📢 Спам / реклама",          "code": "spam"},
        {"label": "✏️ Другая причина",          "code": "other"},
    ],
    "cover": [
        {"label": "🔞 Неприемлемое изображение", "code": "nsfw"},
        {"label": "©️ Чужое изображение",        "code": "copyright"},
        {"label": "🖼 Плохое качество",          "code": "bad_quality"},
        {"label": "✏️ Другая причина",           "code": "other"},
    ],
}

REASON_TEXT = {
    "not_a_track": "не является треком",
    "bad_content": "неприемлемый контент",
    "political":   "политический подтекст",
    "copyright":   "нарушение авторских прав",
    "bad_length":  "некорректная длина",
    "duplicate":   "дубликат",
    "offensive":   "оскорбительное название",
    "spam":        "спам / реклама",
    "nsfw":        "неприемлемое изображение",
    "bad_quality": "плохое качество",
    "other":       "другая причина",
}

# ============================================================
#  Логи и приложение
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

app_tg  = Application.builder().token(BOT_TOKEN).build()
app_web = FastAPI()
app_web.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
bot: Bot = app_tg.bot

# ============================================================
#  Состояние (в памяти)
# ============================================================

handled: dict[str, str] = {}
history: deque = deque(maxlen=100)
pending_count = {"songs": 0, "names": 0, "covers": 0}
sent_songs, sent_names, sent_covers = set(), set(), set()

def mark_handled(key: str, name: str): handled[key] = name
def who_handled(key: str): return handled.get(key)

def add_history(item_type, item_id, action, mod_name, reason=""):
    history.appendleft({
        "type":     item_type,
        "id":       item_id,
        "action":   action,
        "mod":      mod_name,
        "reason":   reason,
        "time":     datetime.datetime.now().strftime("%d.%m %H:%M"),
    })

# ============================================================
#  Telegram WebApp проверка
# ============================================================

def verify_webapp_user(init_data: str) -> dict | None:
    try:
        parsed    = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        hash_val  = parsed.pop("hash", None)
        if not hash_val:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected   = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hash_val):
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user
    except Exception as e:
        log.warning(f"verify_webapp_user error: {e}")
        return None

def get_mod_from_request(request: Request) -> dict | None:
    init_data = request.headers.get("X-Init-Data", "")
    if not init_data:
        return None
    user = verify_webapp_user(init_data)
    if not user:
        return None
    if user.get("id") not in MOD_IDS:
        return None
    return user

# ============================================================
#  API сайта
# ============================================================

async def api(method: str, path: str, body: dict = None):
    url     = SITE_URL + path
    headers = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as c:
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        r = await c.request(method, url, **kwargs)
        r.raise_for_status()
        return r.json()

# ============================================================
#  Уведомления
# ============================================================

async def notify_moderators(text: str):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎛 Открыть панель модерации", web_app=WebAppInfo(url=f"{APP_URL}/app"))
    ]])
    for mod_id in MOD_IDS:
        try:
            await bot.send_message(mod_id, text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            log.warning(f"Не удалось уведомить модератора {mod_id}: {e}")

async def on_new_song(data: dict):
    artist  = data.get("artist") or "❓ Неизвестен"
    title   = data.get("title")  or "❓ Неизвестно"
    unknown = not data.get("artist") or not data.get("title")
    tag     = "\n⚠️ <b>Трек без подписи</b>" if unknown else ""
    await notify_moderators(f"🎵 <b>Новая песня на модерацию</b>{tag}\n\n🎤 {artist}\n📝 {title}\n👤 Загрузил: {data.get('uploader', '—')}")

async def on_new_playlist_name(data: dict):
    await notify_moderators(f"📋 <b>Новое название плейлиста</b>\n\n📝 <b>{data.get('name', '—')}</b>\n👤 Создатель: {data.get('creator', '—')}")

async def on_new_cover(data: dict):
    await notify_moderators(f"🖼 <b>Новая обложка плейлиста</b>\n\n📋 {data.get('name', '—')}\n👤 Создатель: {data.get('creator', '—')}")

# ============================================================
#  Команды бота
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in MOD_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        "👋 <b>Бот модерации музыкального сайта</b>\n\nСюда будут приходить уведомления о новых песнях, названиях и обложках плейлистов.\n\nНажми кнопку ниже чтобы открыть панель:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎛 Открыть панель модерации", web_app=WebAppInfo(url=f"{APP_URL}/app"))
        ]])
    )

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"👤 Твой Telegram ID: <code>{update.effective_user.id}</code>", parse_mode="HTML")

async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text("🎛 Открой панель модерации:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎛 Панель модерации", web_app=WebAppInfo(url=f"{APP_URL}/app"))
        ]])
    )

# ============================================================
#  API FastAPI (Mini App)
# ============================================================

@app_web.get("/app")
async def serve_app():
    html_path = Path(__file__).parent / "app.html"
    return FileResponse(html_path, media_type="text/html")

@app_web.get("/api/pending")
async def get_pending(request: Request):
    user = get_mod_from_request(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        songs  = (await api("GET", "/api/bot/pending/songs")).get("data", [])
        names  = (await api("GET", "/api/bot/pending/names")).get("data", [])
        covers = (await api("GET", "/api/bot/pending/covers")).get("data", [])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    for s in songs:  s["handled_by"] = who_handled(f"song:{s['id']}")
    for n in names:  n["handled_by"] = who_handled(f"name:{n['id']}")
    for c in covers: c["handled_by"] = who_handled(f"cover:{c['id']}")
    return JSONResponse({"songs": songs, "names": names, "covers": covers})

@app_web.post("/api/action")
async def do_action(request: Request):
    user = get_mod_from_request(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    data      = await request.json()
    item_type = data.get("type")
    item_id   = str(data.get("id"))
    action    = data.get("action")
    reason    = data.get("reason", "")
    mod_name  = user.get("first_name", "Модератор")
    key       = f"{item_type}:{item_id}"
    already = who_handled(key)
    if already and action != "sign":
        return JSONResponse({"error": "already_handled", "by": already}, status_code=409)
    try:
        if action == "approve":
            if item_type == "song": await api("POST", f"/api/bot/songs/{item_id}/approve")
            elif item_type == "name": await api("POST", f"/api/bot/playlists/{item_id}/name/approve")
            elif item_type == "cover": await api("POST", f"/api/bot/playlists/{item_id}/cover/approve")
            mark_handled(key, mod_name)
            add_history(item_type, item_id, "approve", mod_name)
        elif action == "reject":
            reason_text = REASON_TEXT.get(reason, reason or "другая причина")
            if item_type == "song": await api("DELETE", f"/api/bot/songs/{item_id}", {"reason": reason_text})
            elif item_type == "name":
                pl = await api("GET", f"/api/bot/playlists/{item_id}")
                new_name = f"Плейлист {pl.get('creator', 'пользователя')}"
                await api("PATCH", f"/api/bot/playlists/{item_id}/name/reject", {"new_name": new_name, "reason": reason_text})
            elif item_type == "cover": await api("DELETE", f"/api/bot/playlists/{item_id}/cover", {"reason": reason_text})
            mark_handled(key, mod_name)
            add_history(item_type, item_id, "reject", mod_name, reason_text)
        elif action == "sign":
            artist = data.get("artist", "").strip()
            title  = data.get("title", "").strip()
            if not artist or not title:
                return JSONResponse({"error": "empty_fields"}, status_code=400)
            await api("PATCH", f"/api/bot/songs/{item_id}/sign", {"artist": artist, "title": title})
        return JSONResponse({"ok": True})
    except Exception as e:
        log.error(f"action error {item_type} {item_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app_web.get("/api/history")
async def get_history(request: Request):
    user = get_mod_from_request(request)
    if not user: return JSONResponse({"error": "unauthorized"}, status_code=403)
    return JSONResponse({"history": list(history)[:50]})

@app_web.get("/api/rules")
async def get_rules(request: Request):
    return JSONResponse({"rules": RULES_TEXT})

@app_web.get("/api/reasons/{item_type}")
async def get_reasons(item_type: str, request: Request):
    user = get_mod_from_request(request)
    if not user: return JSONResponse({"error": "unauthorized"}, status_code=403)
    return JSONResponse({"reasons": REJECT_REASONS.get(item_type, [])})

@app_web.post("/site-webhook")
async def site_webhook(request: Request):
    if request.headers.get("X-Secret") != SECRET:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    data  = await request.json()
    event = data.get("type")
    try:
        if   event == "song":  await on_new_song(data)
        elif event == "name":  await on_new_playlist_name(data)
        elif event == "cover": await on_new_cover(data)
    except Exception as e:
        log.error(e)
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})

# ============================================================
#  Периодическая проверка
# ============================================================

async def check_pending():
    log.info(f"Polling каждые {INTERVAL} сек.")
    await asyncio.sleep(5)
    prev_total = -1
    while True:
        try:
            songs  = (await api("GET", "/api/bot/pending/songs")).get("data", [])
            names  = (await api("GET", "/api/bot/pending/names")).get("data", [])
            covers = (await api("GET", "/api/bot/pending/covers")).get("data", [])
            pending_count["songs"]  = len(songs)
            pending_count["names"]  = len(names)
            pending_count["covers"] = len(covers)
            total = pending_count["songs"] + pending_count["names"] + pending_count["covers"]
            if prev_total > 0 and total == 0:
                for mod_id in MOD_IDS:
                    try:
                        await bot.send_message(mod_id, "🎉 <b>Очередь пуста! Всё обработано.</b>", parse_mode="HTML")
                    except Exception: pass
            prev_total = total
            for item in songs:
                if item["id"] not in sent_songs:
                    sent_songs.add(item["id"]); await on_new_song(item)
            for item in names:
                if item["id"] not in sent_names:
                    sent_names.add(item["id"]); await on_new_playlist_name(item)
            for item in covers:
                if item["id"] not in sent_covers:
                    sent_covers.add(item["id"]); await on_new_cover(item)
        except Exception as e:
            log.error(f"Polling error: {e}")
        await asyncio.sleep(INTERVAL)

# ============================================================
#  Запуск бота + FastAPI
# ============================================================

app_tg.add_handler(CommandHandler("start", cmd_start))
app_tg.add_handler(CommandHandler("id",    cmd_id))
app_tg.add_handler(CommandHandler("panel", cmd_panel))

async def main():
    asyncio.create_task(check_pending())
    bot_task = asyncio.create_task(app_tg.run_polling())
    server = uvicorn.Server(uvicorn.Config(app_web, host="0.0.0.0", port=PORT, log_level="warning"))
    log.info(f"Сервер запущен на порту {PORT} ✓")
    log.info(f"Mini App доступен по адресу: {APP_URL}/app")
    server_task = asyncio.create_task(server.serve())
    await asyncio.gather(bot_task, server_task)

if __name__ == "__main__":
    asyncio.run(main())
