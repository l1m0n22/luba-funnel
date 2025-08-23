# main.py
import os
import asyncio
import time
import hmac
import hashlib
import base64
from datetime import timedelta
from io import BytesIO
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from dotenv import load_dotenv
from aiohttp import web

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue

# Совместимость с разными версиями PTB
try:
    from telegram import FSInputFile as _PTB_FSInput  # PTB 20+
except Exception:
    try:
        from telegram import InputFile as _PTB_FSInput  # PTB 13.x
    except Exception:
        _PTB_FSInput = None

# Pillow для безопасной перекодировки
from PIL import Image, ImageOps

load_dotenv()

# ===== ENV =====
BOT_TOKEN = os.getenv("FUNNEL_BOT_TOKEN")
SITE_URL  = os.getenv("SITE_URL", "https://next-level-form.onrender.com")
FUNNEL_BASE_URL = os.getenv("FUNNEL_BASE_URL")               # публичный URL этого сервиса (для /go)
FUNNEL_SIGNING_SECRET = os.getenv("FUNNEL_SIGNING_SECRET")   # общий секрет БОТ<>БЭК

# ФОТО к сообщениям 1..5 (положи файлы рядом с main.py или укажи абсолютные пути/URL)
PHOTO1 = os.getenv("FUNNEL_PHOTO1", "photo_2025-08-23_23-07-59.jpg")
PHOTO2 = os.getenv("FUNNEL_PHOTO2", "photo_2025-08-23_23-08-29.jpg")
PHOTO3 = os.getenv("FUNNEL_PHOTO3", "photo_2025-08-23_23-59-13.jpg")
PHOTO4 = os.getenv("FUNNEL_PHOTO4", "photo_2025-08-24_00-00-54.jpg")
PHOTO5 = os.getenv("FUNNEL_PHOTO5", "photo_2025-08-24_00-01-43.jpg")

if not BOT_TOKEN:
    raise RuntimeError("В .env нет FUNNEL_BOT_TOKEN")
if not FUNNEL_SIGNING_SECRET:
    raise RuntimeError("В .env нет FUNNEL_SIGNING_SECRET")

# Только «submitted» влияет на отмену напоминаний (клик по ссылке — нет)
submitted: set[int] = set()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ===== ТЕКСТЫ =====
MSG1 = "Salom! Biz xursandmiz, Siz deyarli jamoamizga qo‘shildingi 🚀"
def msg2(link: str) -> str:
    return (
        "Jietti brendining ambassadori bo‘lish uchun qisqa anketa to‘ldiring. "
        "Biz 2 soat ichida Siz bilan bog‘lanamiz."
        f"\n\nHavola: {link}"
    )
MSG3 = "Tabriklaymiz! 🎉 Arizangiz qabul qilindi. Tez orada menejerimiz Siz bilan bog‘lanadi."
MSG4 = "Eslatma: anketani hali to‘ldirmadingiz. ⏳ Joylar soni cheklangan."
MSG5 = "Oxirgi eslatma! 🚨 Faqat bir nechta joy qoldi. Qulay paytda to‘ldiring."

# ===== УТИЛИТЫ: подпись и ссылки =====
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def _sign_payload(chat_id: int, ts: int) -> str:
    key = FUNNEL_SIGNING_SECRET.encode()
    msg = f"{chat_id}:{ts}".encode()
    return _b64url(hmac.new(key, msg, hashlib.sha256).digest())

def _verify(chat_id: int, ts: int, sig: str, ttl_sec: int = 172800) -> bool:
    # подпись действительна 48 часов по умолчанию
    if abs(int(time.time()) - int(ts)) > ttl_sec:
        return False
    try:
        return hmac.compare_digest(sig, _sign_payload(int(chat_id), int(ts)))
    except Exception:
        return False

def make_signed_params(chat_id: int) -> dict[str, str]:
    ts = int(time.time())
    sig = _sign_payload(chat_id, ts)
    return {"c": str(chat_id), "ts": str(ts), "sig": sig}

def _add_query(url: str, extra: dict[str, str]) -> str:
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    q.update(extra)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), u.fragment))

def make_track_link(chat_id: int) -> str:
    params = make_signed_params(chat_id)
    if FUNNEL_BASE_URL:
        return _add_query(f"{FUNNEL_BASE_URL.rstrip('/')}/go", params)  # через наш редирект
    return _add_query(SITE_URL, params)                                 # прямой на сайт (с параметрами)

# ===== УТИЛИТЫ: фото-отправка (с перекодировкой при ошибке) =====
def _resolve_path(p: str) -> str:
    if not p:
        return ""
    p = p.strip()
    if p.startswith(("http://", "https://")):
        return p
    if os.path.isabs(p) and os.path.exists(p):
        return p
    return os.path.join(SCRIPT_DIR, p)

def _build_photo_arg(path_or_url: str):
    """Готовит аргумент для send_photo(): URL-строка, FSInputFile/InputFile, ('_FILEPATH_', abs) или None."""
    if not path_or_url:
        return None
    p = _resolve_path(path_or_url)
    if p.startswith(("http://", "https://")):
        return p
    if os.path.exists(p):
        if _PTB_FSInput is not None:
            try:
                return _PTB_FSInput(p)
            except Exception as e:
                print(f"[PHOTO] FSInputFile/InputFile не сработал для {p}: {e}")
        return ("_FILEPATH_", p)
    print(f"[PHOTO] Файл не найден: {p}")
    return None

def _local_path_if_any(path_or_url: str) -> str | None:
    if not path_or_url:
        return None
    p = _resolve_path(path_or_url)
    if p.startswith(("http://", "https://")):
        return None
    return p if os.path.exists(p) else None

def _reencode_to_jpeg_bytes(path: str) -> BytesIO:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im).convert("RGB")
    max_dim = 4096
    if max(im.size) > max_dim:
        im.thumbnail((max_dim, max_dim))
    bio = BytesIO()
    im.save(bio, format="JPEG", quality=85, optimize=True, progressive=False)
    bio.seek(0)
    return bio

async def send_with_photo(bot, chat_id: int, text: str,
                          kb: InlineKeyboardMarkup | None = None,
                          photo_path: str | None = None):
    """1) пробуем как есть; 2) при ошибке перекодируем в JPEG; 3) фолбэк — текст."""
    ph = _build_photo_arg(photo_path) if photo_path else None
    local_path = _local_path_if_any(photo_path) if photo_path else None

    # Первая попытка
    try:
        if isinstance(ph, tuple) and ph[0] == "_FILEPATH_":
            with open(ph[1], "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f, caption=text, reply_markup=kb)
        elif ph is not None:
            await bot.send_photo(chat_id=chat_id, photo=ph, caption=text, reply_markup=kb)
        else:
            raise RuntimeError("Нет фото (ph=None)")
        return
    except Exception as e:
        print(f"[PHOTO] Первая попытка не удалась ({photo_path}): {e}")

    # Вторая попытка — перекодировка в JPEG
    if local_path:
        try:
            bio = _reencode_to_jpeg_bytes(local_path)
            await bot.send_photo(chat_id=chat_id, photo=bio, caption=text, reply_markup=kb)
            print("[PHOTO] Перекодировали и отправили JPEG ✓")
            return
        except Exception as e2:
            print(f"[PHOTO] Перекодировка тоже не удалась: {e2}")

    # Фолбэк — текст
    print("[PHOTO] Падаем в текстовый фолбэк")
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)

# ===== КЛАВИАТУРА =====
def one_button_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Анketani to‘ldirish", url=make_track_link(chat_id))]])

# ===== JobQueue =====
def cancel_user_jobs(jq: JobQueue, chat_id: int):
    for name in (f"f60:{chat_id}", f"f24h:{chat_id}"):
        for j in jq.get_jobs_by_name(name):
            j.schedule_removal()

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Шлём напоминание (4/5) только если пользователь НЕ отправил форму."""
    chat_id = context.job.chat_id
    payload = context.job.data or {}
    text  = payload.get("text", "")
    photo = payload.get("photo")
    if chat_id not in submitted:
        await send_with_photo(context.bot, chat_id, text, one_button_kb(chat_id), photo)

# ===== Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    submitted.discard(chat_id)  # новая сессия — считаем, что пока не отправил

    # Диагностика путей к фото
    for i, ph in enumerate([PHOTO1, PHOTO2, PHOTO3, PHOTO4, PHOTO5], start=1):
        rp = _resolve_path(ph)
        exists = (os.path.exists(rp) if not rp.startswith("http") else "url")
        print(f"[PHOTO] PHOTO{i} -> {rp} | exists={exists}")

    link = make_track_link(chat_id)

    # 1 и 2 — сразу
    await send_with_photo(context.bot, chat_id, MSG1, None, PHOTO1)
    await send_with_photo(context.bot, chat_id, msg2(link), one_button_kb(chat_id), PHOTO2)

    # Планируем только 1 час и 24 часа (4 и 5)
    jq: JobQueue = context.job_queue
    cancel_user_jobs(jq, chat_id)
    jq.run_once(reminder_job, when=timedelta(minutes=5),   chat_id=chat_id,
                data={"text": MSG4, "photo": PHOTO4}, name=f"f60:{chat_id}")
    jq.run_once(reminder_job, when=timedelta(minutes=5),    chat_id=chat_id,
                data={"text": MSG5, "photo": PHOTO5}, name=f"f24h:{chat_id}")

async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_with_photo(context.bot, update.effective_chat.id,
                          make_track_link(update.effective_chat.id), None, PHOTO2)

# ===== HTTP: /go — только проксируем параметры, НИЧЕГО НЕ ОТМЕНЯЕМ =====
async def go_handler(request: web.Request):
    chat_id = request.query.get("c", "")
    ts  = request.query.get("ts", "")
    sig = request.query.get("sig", "")
    # Просто пробрасываем на лендинг с теми же параметрами
    dest = _add_query(SITE_URL, {"c": chat_id, "ts": ts, "sig": sig})
    raise web.HTTPFound(dest)

# ===== HTTP: /mark-submitted — бэкенд зовёт после удачной отправки формы =====
async def mark_submitted(request: web.Request):
    from telegram.ext import Application as PTBApp
    ptb_app: PTBApp = request.app["ptb_app"]

    try:
        payload = await request.json()
        chat_id = int(str(payload.get("c", "")))
        ts  = int(str(payload.get("ts", "")))
        sig = str(payload.get("sig", ""))
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    if not _verify(chat_id, ts, sig):
        return web.json_response({"ok": False, "error": "bad signature"}, status=403)

    # Помечаем как отправившего форму и отменяем напоминания
    submitted.add(chat_id)
    cancel_user_jobs(ptb_app.job_queue, chat_id)

    # Сообщение 3 — поздравление с фото
    try:
        await send_with_photo(ptb_app.bot, chat_id, MSG3, None, PHOTO3)
    except Exception as e:
        print(f"[PHOTO] Ошибка отправки поздравления: {e}")

    return web.json_response({"ok": True})

# ===== Web server =====
async def start_web(ptb_app: Application) -> web.AppRunner:
    app = web.Application()
    app["ptb_app"] = ptb_app
    app.add_routes([
        web.get("/go", go_handler),
        web.post("/mark-submitted", mark_submitted),
    ])
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    return runner

# ===== Entrypoint =====
async def main_async():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("site", site_cmd))

    # Запускаем PTB без .run_polling (чтобы не конфликтовать с текущим loop)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message"])

    runner = await start_web(app)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main_async())
