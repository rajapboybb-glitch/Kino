import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

DB_PATH = "kino_bot.db"
if Path("/data").is_dir():
    DB_PATH = "/data/kino_bot.db"

MAX_SQLITE_INT = 2**63 - 1

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi!")

ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kino_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def parse_int(text):
    if not text:
        return None
    text = text.strip()
    if not text or len(text) > 19 or not (text.isascii() and text.isdigit()):
        return None
    number = int(text)
    if number > MAX_SQLITE_INT:
        return None
    return number

async def run_db(func, *args):
    return await asyncio.to_thread(func, *args)

async def handle_ping(request):
    return web.Response(text="Bot ishlamoqda!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    parent = Path(DB_PATH).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                code INTEGER PRIMARY KEY,
                file_id TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        conn.commit()
    finally:
        conn.close()

def db_add_user(user_id: int):
    conn = get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
    finally:
        conn.close()

def db_get_movie(code: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT file_id, caption FROM movies WHERE code = ?", (code,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return row["file_id"], row["caption"]

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not message.from_user:
        return
    await run_db(db_add_user, message.from_user.id)
    await message.answer(f"👋 Salom, {message.from_user.first_name}!\n🎬 Kino kodini yuboring:")

@dp.message(F.text.regexp(r"^\s*[0-9]{1,19}\s*$"))
async def search_movie(message: types.Message):
    if not message.from_user or not message.text:
        return
    await run_db(db_add_user, message.from_user.id)
    code = parse_int(message.text)
    if code is None:
        await message.answer("❌ Kino topilmadi.")
        return
    try:
        movie = await run_db(db_get_movie, code)
    except sqlite3.Error:
        await message.answer("❌ Xatolik yuz berdi.")
        return
    if not movie:
        await message.answer("❌ Bu kod bo'yicha kino topilmadi.")
        return
    file_id, caption = movie
    try:
        await message.answer_video(video=file_id, caption=(caption if caption else None))
    except TelegramAPIError:
        await message.answer("❌ Videoni yuborishda xatolik.")

async def main():
    init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(start_web_server())
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
    
