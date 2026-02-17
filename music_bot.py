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

import asyncio, logging, httpx, hashlib, hmac, json, urllib.parse
from datetime import datetime
from collections import deque
from pathlib import Path
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


# ============================================================
#  👇 ЗАПОЛНИ ТОЛЬКО ЭТИ СТРОЧКИ
# ============================================================

BOT_TOKEN = "8566474882:AAHfufmlEeW0XmkX_y4IDL6Tcwj52D6Eaa8"

# Telegram ID всех модераторов — им будут приходить уведомления
# Узнать свой ID: напиши боту /id
MOD_IDS = [7628577301, 222222, 333333]

# URL где будет доступен этот сервер — ДОЛЖЕН быть HTTPS!
# Для теста можно использовать ngrok: ngrok http 8000
# Тогда здесь будет что-то типа: https://abc123.ngrok.io
APP_URL = "https://твой-домен.com"

# URL твоего сайта
SITE_URL = "https://твой-сайт.com"

# ============================================================
#  Это можно не трогать
# ============================================================

SECRET   = "bot_secret_key_2024"
PORT     = 8000
INTERVAL = 60

RULES_TEXT = """📋 <b>Правила модерации треков</b>

<b>✅ Принимаем:</b>
• Оригинальный трек (студийная запись)
• Существующая песня с правильным названием и исполнителем
• Длина: от 1 до 10 минут (обычно 1–6 минут)

<b>❌ Отклоняем:</b>
• Голосовые сообщения, вырезки из стримов, случайные звуки
• Треки длиннее 10–15 минут
• Политический подтекст — особенно Россия/Украина
• Стёбная и провокационная тематика
• Неприемлемый контент

<b>💡 Совет:</b>
Если трек без подписи — проверь его в интернете, затем заполни исполнителя и название через кнопку ✍️"""

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
#  Состояние (в памяти — перезапуск сбрасывает)
# ============================================================

handled: dict[str, str] = {}          # "song:42" → "Иван"
history: deque          = deque(maxlen=100)
pending_count           = {"songs": 0, "names": 0, "covers": 0}
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
        "time":     datetime.now().strftime("%d.%m %H:%M"),
    })


# ============================================================
#  Telegram WebApp — проверка подлинности запроса
#  (чтобы только настоящие модераторы через Telegram могли вызывать API)
# ============================================================

def verify_webapp_user(init_data: str) -> dict | None:
    """
    Проверяет подпись initData от Telegram WebApp.
    Возвращает данные пользователя если всё ок, иначе None.
    """
    try:
        parsed    = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        hash_val  = parsed.pop("hash", None)
        if not hash_val:
            return None

        # Формируем строку для проверки
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

        # Ключ = HMAC-SHA256("WebAppData", bot_token)
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
    """Достаёт и проверяет пользователя из заголовка X-Init-Data."""
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
#  Запрос к API сайта
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
#  Уведомления модераторам
# ============================================================

async def notify_moderators(text: str):
    """Шлёт личное сообщение каждому модератору с кнопкой открыть панель."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🎛 Открыть панель модерации",
            web_app=WebAppInfo(url=f"{APP_URL}/app")
        )
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
    await notify_moderators(
        f"🎵 <b>Новая песня на модерацию</b>{tag}\n\n"
        f"🎤 {artist}\n📝 {title}\n"
        f"👤 Загрузил: {data.get('uploader', '—')}"
    )

async def on_new_playlist_name(data: dict):
    await notify_moderators(
        f"📋 <b>Новое название плейлиста</b>\n\n"
        f"📝 <b>{data.get('name', '—')}</b>\n"
        f"👤 Создатель: {data.get('creator', '—')}"
    )

async def on_new_cover(data: dict):
    await notify_moderators(
        f"🖼 <b>Новая обложка плейлиста</b>\n\n"
        f"📋 {data.get('name', '—')}\n"
        f"👤 Создатель: {data.get('creator', '—')}"
    )


# ============================================================
#  Команды бота (в личке)
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in MOD_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        "👋 <b>Бот модерации музыкального сайта</b>\n\n"
        "Сюда будут приходить уведомления о новых песнях, "
        "названиях и обложках плейлистов.\n\n"
        "Нажми кнопку ниже чтобы открыть панель:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🎛 Открыть панель модерации",
                web_app=WebAppInfo(url=f"{APP_URL}/app")
            )
        ]])
    )

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👤 Твой Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML"
    )

async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MOD_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        "🎛 Открой панель модерации:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🎛 Панель модерации",
                web_app=WebAppInfo(url=f"{APP_URL}/app")
            )
        ]])
    )


# ============================================================
#  API для Mini App
# ============================================================

@app_web.get("/app")
async def serve_app():
    """Отдаёт HTML файл мини-приложения."""
    html_path = Path(__file__).parent / "app.html"
    return FileResponse(html_path, media_type="text/html")


@app_web.get("/api/pending")
async def get_pending(request: Request):
    """Возвращает все элементы в очереди модерации."""
    user = get_mod_from_request(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    try:
        songs  = (await api("GET", "/api/bot/pending/songs")).get("data", [])
        names  = (await api("GET", "/api/bot/pending/names")).get("data", [])
        covers = (await api("GET", "/api/bot/pending/covers")).get("data", [])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # Добавляем статус "кто обработал" для каждого элемента
    for s in songs:
        s["handled_by"] = who_handled(f"song:{s['id']}")
    for n in names:
        n["handled_by"] = who_handled(f"name:{n['id']}")
    for c in covers:
        c["handled_by"] = who_handled(f"cover:{c['id']}")

    return JSONResponse({"songs": songs, "names": names, "covers": covers})


@app_web.post("/api/action")
async def do_action(request: Request):
    """Обрабатывает действие модератора (approve/reject/sign)."""
    user = get_mod_from_request(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    data      = await request.json()
    item_type = data.get("type")    # song / name / cover
    item_id   = str(data.get("id"))
    action    = data.get("action")  # approve / reject / sign
    reason    = data.get("reason", "")
    mod_name  = user.get("first_name", "Модератор")
    key       = f"{item_type}:{item_id}"

    # Проверяем не обработано ли уже
    already = who_handled(key)
    if already and action != "sign":
        return JSONResponse({"error": "already_handled", "by": already}, status_code=409)

    try:
        if action == "approve":
            if item_type == "song":
                await api("POST", f"/api/bot/songs/{item_id}/approve")
            elif item_type == "name":
                await api("POST", f"/api/bot/playlists/{item_id}/name/approve")
            elif item_type == "cover":
                await api("POST", f"/api/bot/playlists/{item_id}/cover/approve")
            mark_handled(key, mod_name)
            add_history(item_type, item_id, "approve", mod_name)

        elif action == "reject":
            reason_text = REASON_TEXT.get(reason, reason or "другая причина")
            if item_type == "song":
                await api("DELETE", f"/api/bot/songs/{item_id}", {"reason": reason_text})
            elif item_type == "name":
                pl = await api("GET", f"/api/bot/playlists/{item_id}")
                new_name = f"Плейлист {pl.get('creator', 'пользователя')}"
                await api("PATCH", f"/api/bot/playlists/{item_id}/name/reject",
                          {"new_name": new_name, "reason": reason_text})
            elif item_type == "cover":
                await api("DELETE", f"/api/bot/playlists/{item_id}/cover", {"reason": reason_text})
            mark_handled(key, mod_name)
            add_history(item_type, item_id, "reject", mod_name, reason_text)

        elif action == "sign":
            artist = data.get("artist", "").strip()
            title  = data.get("title", "").strip()
            if not artist or not title:
                return JSONResponse({"error": "empty_fields"}, status_code=400)
            await api("PATCH", f"/api/bot/songs/{item_id}/sign",
                      {"artist": artist, "title": title})

        return JSONResponse({"ok": True})

    except Exception as e:
        log.error(f"action error {item_type} {item_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app_web.get("/api/history")
async def get_history(request: Request):
    user = get_mod_from_request(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    return JSONResponse({"history": list(history)[:50]})


@app_web.get("/api/rules")
async def get_rules(request: Request):
    # Правила открыты без авторизации — просто текст
    return JSONResponse({"rules": RULES_TEXT})


@app_web.get("/api/reasons/{item_type}")
async def get_reasons(item_type: str, request: Request):
    user = get_mod_from_request(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    return JSONResponse({"reasons": REJECT_REASONS.get(item_type, [])})


# ============================================================
#  Webhook от сайта — входящие события
# ============================================================

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
#  Периодическая проверка (резервный режим)
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
                        await bot.send_message(mod_id, "🎉 <b>Очередь пуста! Всё обработано.</b>",
                                               parse_mode="HTML")
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
#  Запуск
# ============================================================

app_tg.add_handler(CommandHandler("start", cmd_start))
app_tg.add_handler(CommandHandler("id",    cmd_id))
app_tg.add_handler(CommandHandler("panel", cmd_panel))


async def main():
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram бот запущен ✓")
    asyncio.create_task(check_pending())
    server = uvicorn.Server(uvicorn.Config(app_web, host="0.0.0.0", port=PORT, log_level="warning"))
    log.info(f"Сервер запущен на порту {PORT} ✓")
    log.info(f"Mini App доступен по адресу: {APP_URL}/app")
    await server.serve()
    await app_tg.updater.stop()
    await app_tg.stop()
    await app_tg.shutdown()


if __name__ == "__main__":
    asyncio.run(main())


# ============================================================
#  МАРШРУТЫ НА САЙТЕ:
#  POST   /api/bot/songs/{id}/approve
#  DELETE /api/bot/songs/{id}                  body: {"reason":"..."}
#  PATCH  /api/bot/songs/{id}/sign             body: {"artist":"...","title":"..."}
#  POST   /api/bot/playlists/{id}/name/approve
#  PATCH  /api/bot/playlists/{id}/name/reject  body: {"new_name":"...","reason":"..."}
#  POST   /api/bot/playlists/{id}/cover/approve
#  DELETE /api/bot/playlists/{id}/cover        body: {"reason":"..."}
#  GET    /api/bot/playlists/{id}              → {"creator":"Имя"}
#  GET    /api/bot/pending/songs               → {"data":[...]}
#  GET    /api/bot/pending/names               → {"data":[...]}
#  GET    /api/bot/pending/covers              → {"data":[...]}
#  Все запросы: Authorization: Bearer bot_secret_key_2024
#
#  СОБЫТИЯ ОТ САЙТА → POST {APP_URL}/site-webhook, X-Secret: bot_secret_key_2024
#  song:  {"type":"song",  "id":1,"title":"...","artist":"...","uploader":"...","audio_url":"..."}
#  name:  {"type":"name",  "id":1,"name":"...","creator":"..."}
#  cover: {"type":"cover", "id":1,"name":"...","creator":"...","cover_url":"..."}
# ============================================================
