import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# =====================================
# SOZLAMALAR
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

# DB yo'li: DB_PATH berilmasa, /data papkasi (persistent disk) bo'lsa o'shani ishlatadi
_env_db = os.getenv("DB_PATH", "").strip()
if _env_db:
    DB_PATH = _env_db
elif Path("/data").is_dir():
    DB_PATH = "/data/kino_bot.db"
else:
    DB_PATH = "kino_bot.db"

MAX_SQLITE_INT = 2**63 - 1
MAX_CAPTION = 1024
BROADCAST_DELAY = 0.05
BROADCAST_RETRIES = 3

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi! Environment Variables ga token kiriting."
    )

if not (ADMIN_ID_RAW.isascii() and ADMIN_ID_RAW.isdigit()):
    raise RuntimeError(
        "ADMIN_ID noto'g'ri! Telegram ID raqamingizni kiriting."
    )

ADMIN_ID = int(ADMIN_ID_RAW)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kino_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =====================================
# YORDAMCHI FUNKSIYALAR
# =====================================

def parse_int(text):
    """Faqat ASCII raqamlardan iborat va SQLite sig'imiga mos sonni qaytaradi.
    Aks holda None."""
    if not text:
        return None

    text = text.strip()

    if not text or len(text) > 19:
        return None

    if not (text.isascii() and text.isdigit()):
        return None

    number = int(text)

    if number > MAX_SQLITE_INT:
        return None

    return number


async def run_db(func, *args):
    """Sinxron SQLite funksiyani alohida threadda ishga tushiradi,
    shunda event loop bloklanmaydi."""
    return await asyncio.to_thread(func, *args)


# =====================================
# RENDER UCHUN PORT VA HEALTH-CHECK
# =====================================

async def handle_ping(request):
    """Render uchun oddiy javob qaytaruvchi endpoint."""
    return web.Response(text="Bot ishlamoqda!")


async def start_web_server():
    """Render talab qiladigan PORT-ni tinglaydigan aiohttp serverini ishga tushiradi."""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()

    # Render avtomatik $PORT beradi (agar bo'lmasa 10000 ishlatiladi)
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Render Web-server %s-portda ishga tushdi", port)


# =====================================
# DATABASE (sinxron funksiyalar)
# =====================================

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    parent = Path(DB_PATH).parent
    parent.mkdir(parents=True, exist_ok=True)

    with closing(get_connection()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                code INTEGER PRIMARY KEY,
                file_id TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                is_vip INTEGER NOT NULL DEFAULT 0
                    CHECK (is_vip IN (0, 1))
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_vip INTEGER NOT NULL DEFAULT 0
                    CHECK (is_vip IN (0, 1)),
                is_blocked INTEGER NOT NULL DEFAULT 0
                    CHECK (is_blocked IN (0, 1))
            )
        """)

        # Eski bazalar uchun migratsiya: is_blocked ustuni
        columns = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        ]
        if "is_blocked" not in columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0"
            )

        conn.commit()


def db_add_user(user_id: int):
    """Foydalanuvchini qo'shadi; agar oldin bloklagan bo'lsa, qayta faol qiladi."""
    with closing(get_connection()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )
        conn.execute(
            "UPDATE users SET is_blocked = 0 WHERE user_id = ? AND is_blocked = 1",
            (user_id,),
        )
        conn.commit()


def db_set_vip_status(user_id: int, status: int):
    status = 1 if status else 0

    with closing(get_connection()) as conn:
        conn.execute("""
            INSERT INTO users (user_id, is_vip)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET is_vip = excluded.is_vip
        """, (user_id, status))
        conn.commit()


def db_check_user_vip(user_id: int) -> bool:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT is_vip FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    return bool(row and row["is_vip"] == 1)


def db_get_active_users():
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE is_blocked = 0"
        ).fetchall()

    return [row["user_id"] for row in rows]


def db_mark_blocked(user_ids):
    if not user_ids:
        return

    with closing(get_connection()) as conn:
        conn.executemany(
            "UPDATE users SET is_blocked = 1 WHERE user_id = ?",
            [(uid,) for uid in user_ids],
        )
        conn.commit()


def db_add_movie(code: int, file_id: str, caption: str, is_vip: int = 0):
    caption = (caption or "")[:MAX_CAPTION]

    with closing(get_connection()) as conn:
        conn.execute("""
            INSERT INTO movies (code, file_id, caption, is_vip)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                file_id = excluded.file_id,
                caption = excluded.caption,
                is_vip = excluded.is_vip
        """, (code, file_id, caption, is_vip))
        conn.commit()


def db_get_movie(code: int):
    with closing(get_connection()) as conn:
        row = conn.execute("""
            SELECT file_id, caption, is_vip
            FROM movies
            WHERE code = ?
        """, (code,)).fetchone()

    if not row:
        return None

    return row["file_id"], row["caption"], row["is_vip"]


def db_delete_movie(code: int) -> bool:
    with closing(get_connection()) as conn:
        cursor = conn.execute(
            "DELETE FROM movies WHERE code = ?",
            (code,),
        )
        conn.commit()
        return cursor.rowcount > 0


def db_get_stats():
    with closing(get_connection()) as conn:
        total_users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        vip_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_vip = 1"
        ).fetchone()[0]

        blocked_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_blocked = 1"
        ).fetchone()[0]

        total_movies = conn.execute(
            "SELECT COUNT(*) FROM movies"
        ).fetchone()[0]

    return total_users, vip_users, blocked_users, total_movies


# =====================================
# ASYNC OBYOLKALAR (DB xatolarini ushlaydi)
# =====================================

async def add_user(user_id: int):
    if user_id == ADMIN_ID:
        return
    try:
        await run_db(db_add_user, user_id)
    except sqlite3.Error:
        logger.exception("Foydalanuvchini saqlashda xatolik: %s", user_id)


async def check_user_vip(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        return await run_db(db_check_user_vip, user_id)
    except sqlite3.Error:
        logger.exception("VIP tekshirishda xatolik: %s", user_id)
        return False


# =====================================
# FSM HOLATLARI
# =====================================

class AdminStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_vip_choice = State()
    waiting_for_video = State()
    waiting_for_del_code = State()
    waiting_for_broadcast = State()
    waiting_for_give_vip = State()
    waiting_for_remove_vip = State()


# =====================================
# KLAVIATURALAR
# =====================================

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Kino qo'shish"),
                KeyboardButton(text="🗑 Kino o'chirish"),
            ],
            [
                KeyboardButton(text="⭐ VIP berish"),
                KeyboardButton(text="❌ VIPni olib tashlash"),
            ],
            [
                KeyboardButton(text="📊 Statistika"),
                KeyboardButton(text="📢 Xabar yuborish"),
            ],
            [KeyboardButton(text="❌ Chiqish")],
        ],
        resize_keyboard=True,
    )


def get_vip_choice_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 Barcha uchun",
                    callback_data="movie_vip:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Faqat VIP uchun",
                    callback_data="movie_vip:1",
                )
            ],
        ]
    )


# =====================================
# START
# =====================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not message.from_user:
        return

    await add_user(message.from_user.id)

    is_vip = await check_user_vip(message.from_user.id)
    status = "⭐ VIP obunachi" if is_vip else "👤 Oddiy foydalanuvchi"

    await message.answer(
        f"👋 Salom, {message.from_user.first_name}!\n\n"
        f"Maqomingiz: {status}\n\n"
        "🎬 Kino ko'rish uchun kino kodini yuboring."
    )


# =====================================
# ADMIN PANEL
# =====================================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Sizda admin huquqi yo'q.")
        return

    await state.clear()

    await message.answer(
        "🔑 Admin panelga xush kelibsiz!",
        reply_markup=get_admin_keyboard(),
    )


@dp.message(Command("cancel"))
async def cancel_action(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return

    await state.clear()

    await message.answer(
        "✅ Joriy amal bekor qilindi.",
        reply_markup=get_admin_keyboard(),
    )


@dp.message(
    F.text == "❌ Chiqish",
    F.from_user.id == ADMIN_ID,
)
async def exit_admin(message: types.Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "Admin paneldan chiqdingiz.",
        reply_markup=ReplyKeyboardRemove(),
    )


# =====================================
# STATISTIKA
# =====================================

@dp.message(
    F.text == "📊 Statistika",
    F.from_user.id == ADMIN_ID,
)
async def admin_stats(message: types.Message):
    try:
        total_users, vip_users, blocked_users, total_movies = (
            await run_db(db_get_stats)
        )
    except sqlite3.Error:
        logger.exception("Statistikani olishda xatolik")
        await message.answer("❌ Statistikani olib bo'lmadi.")
        return

    await message.answer(
        "📊 BOT STATISTIKASI\n\n"
        f"👥 Foydalanuvchilar: {total_users}\n"
        f"⭐ VIP foydalanuvchilar: {vip_users}\n"
        f"🚫 Botni bloklaganlar: {blocked_users}\n"
        f"🎬 Kinolar: {total_movies}"
    )


# =====================================
# VIP BERISH
# =====================================

@dp.message(
    F.text == "⭐ VIP berish",
    F.from_user.id == ADMIN_ID,
)
async def start_give_vip(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_give_vip)

    await message.answer(
        "⭐ VIP beriladigan foydalanuvchining Telegram ID raqamini yuboring.\n\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_give_vip,
    F.from_user.id == ADMIN_ID,
)
async def process_give_vip(message: types.Message, state: FSMContext):
    target_id = parse_int(message.text)

    if target_id is None:
        await message.answer("❌ ID faqat raqamlardan iborat bo'lsin (to'g'ri ID kiriting).")
        return

    try:
        await run_db(db_set_vip_status, target_id, 1)
    except sqlite3.Error:
        logger.exception("VIP berishda xatolik")
        await message.answer("❌ Bazaga yozib bo'lmadi.")
        return

    await state.clear()

    try:
        await bot.send_message(
            target_id,
            "🎉 Tabriklaymiz! Sizga VIP maqomi berildi.",
        )
    except TelegramAPIError as exc:
        logger.warning("VIP xabari yuborilmadi, user=%s: %s", target_id, exc)

    await message.answer(
        f"✅ {target_id} foydalanuvchiga VIP berildi.",
        reply_markup=get_admin_keyboard(),
    )


# =====================================
# VIPNI OLIB TASHLASH
# =====================================

@dp.message(
    F.text == "❌ VIPni olib tashlash",
    F.from_user.id == ADMIN_ID,
)
async def start_remove_vip(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_remove_vip)

    await message.answer(
        "VIP maqomi olib tashlanadigan foydalanuvchining ID raqamini yuboring.\n\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_remove_vip,
    F.from_user.id == ADMIN_ID,
)
async def process_remove_vip(message: types.Message, state: FSMContext):
    target_id = parse_int(message.text)

    if target_id is None:
        await message.answer("❌ ID faqat raqamlardan iborat bo'lsin (to'g'ri ID kiriting).")
        return

    try:
        await run_db(db_set_vip_status, target_id, 0)
    except sqlite3.Error:
        logger.exception("VIP olib tashlashda xatolik")
        await message.answer("❌ Bazaga yozib bo'lmadi.")
        return

    await state.clear()

    try:
        await bot.send_message(
            target_id,
            "ℹ️ VIP maqomingiz olib tashlandi.",
        )
    except TelegramAPIError as exc:
        logger.warning("VIP xabari yuborilmadi, user=%s: %s", target_id, exc)

    await message.answer(
        f"✅ {target_id} foydalanuvchidan VIP olib tashlandi.",
        reply_markup=get_admin_keyboard(),
    )


# =====================================
# KINO QO'SHISH
# =====================================

@dp.message(
    F.text == "➕ Kino qo'shish",
    F.from_user.id == ADMIN_ID,
)
async def start_add_movie(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_code)

    await message.answer(
        "🔢 Kino uchun raqamli kod kiriting.\n"
        "Masalan: 101\n\n"
        "Mavjud kod kiritilsa, kino yangilanadi.\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_code,
    F.from_user.id == ADMIN_ID,
)
async def process_code(message: types.Message, state: FSMContext):
    code = parse_int(message.text)

    if code is None:
        await message.answer("❌ Kod faqat raqamlardan iborat bo'lsin (juda uzun bo'lmasin).")
        return

    await state.update_data(movie_code=code)
    await state.set_state(AdminStates.waiting_for_vip_choice)

    await message.answer(
        "🔒 Bu kino kimlar uchun?",
        reply_markup=get_vip_choice_keyboard(),
    )


@dp.callback_query(
    AdminStates.waiting_for_vip_choice,
    F.data.startswith("movie_vip:"),
    F.from_user.id == ADMIN_ID,
)
async def process_vip_choice(callback: types.CallbackQuery, state: FSMContext):
    try:
        is_vip = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError, IndexError):
        await callback.answer("Noto'g'ri tanlov!", show_alert=True)
        return

    if is_vip not in (0, 1):
        await callback.answer("Noto'g'ri tanlov!", show_alert=True)
        return

    await state.update_data(is_vip=is_vip)
    await state.set_state(AdminStates.waiting_for_video)

    if callback.message:
        try:
            await callback.message.edit_text(
                "📹 Endi kino videosini yuboring.\n"
                "Bekor qilish: /cancel"
            )
        except TelegramAPIError:
            await callback.message.answer(
                "📹 Endi kino videosini yuboring.\n"
                "Bekor qilish: /cancel"
            )

    await callback.answer()


@dp.message(
    AdminStates.waiting_for_vip_choice,
    F.from_user.id == ADMIN_ID,
)
async def vip_choice_wrong_input(message: types.Message):
    await message.answer(
        "👆 Iltimos, yuqoridagi tugmalardan birini bosing.\n"
        "Bekor qilish: /cancel",
        reply_markup=get_vip_choice_keyboard(),
    )


@dp.message(
    AdminStates.waiting_for_video,
    F.video,
    F.from_user.id == ADMIN_ID,
)
async def process_video(message: types.Message, state: FSMContext):
    data = await state.get_data()

    movie_code = data.get("movie_code")
    is_vip = data.get("is_vip")

    if movie_code is None or is_vip not in (0, 1):
        await state.clear()

        await message.answer(
            "❌ Saqlash ma'lumotlari topilmadi. Qaytadan boshlang.",
            reply_markup=get_admin_keyboard(),
        )
        return

    caption = message.caption or f"🎬 Kino kodi: {movie_code}"

    try:
        await run_db(
            db_add_movie,
            movie_code,
            message.video.file_id,
            caption,
            is_vip,
        )
    except sqlite3.Error:
        logger.exception("Kino saqlashda xatolik")
        await message.answer("❌ Kinoni bazaga saqlab bo'lmadi.")
        return

    await state.clear()

    movie_type = "⭐ VIP kino" if is_vip else "🌐 Oddiy kino"

    extra = ""
    if message.caption and len(message.caption) > MAX_CAPTION:
        extra = f"\n\n⚠️ Izoh {MAX_CAPTION} belgigacha qisqartirildi."

    await message.answer(
        "✅ Kino saqlandi!\n\n"
        f"Turi: {movie_type}\n"
        f"Kod: {movie_code}{extra}",
        reply_markup=get_admin_keyboard(),
    )


@dp.message(
    AdminStates.waiting_for_video,
    F.from_user.id == ADMIN_ID,
)
async def wrong_video_type(message: types.Message):
    await message.answer(
        "❌ Iltimos, videoni video fayl sifatida yuboring.\n"
        "Bekor qilish: /cancel"
    )


# =====================================
# KINO O'CHIRISH
# =====================================

@dp.message(
    F.text == "🗑 Kino o'chirish",
    F.from_user.id == ADMIN_ID,
)
async def start_delete_movie(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_del_code)

    await message.answer(
        "🗑 O'chiriladigan kino kodini yuboring.\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_del_code,
    F.from_user.id == ADMIN_ID,
)
async def process_delete_movie(message: types.Message, state: FSMContext):
    code = parse_int(message.text)

   if code is None:
        await message.answer("❌ Kod faqat raqam bo'lishi kerak (juda uzun bo'lmasin).")
        return

    try:
        deleted = await run_db(db_delete_movie, code)
    except sqlite3.Error:
        logger.exception("Kino o'chirishda xatolik")
        await message.answer("❌ Bazadan o'chirib bo'lmadi.")
        return

    await state.clear()

    if deleted:
        await message.answer(
            f"✅ {code} kodli kino o'chirildi.",
            reply_markup=get_admin_keyboard(),
        )
    else:
        await message.answer(
            "❌ Bu kodli kino topilmadi.",
            reply_markup=get_admin_keyboard(),
        )


# =====================================
# OMMAVIY XABAR YUBORISH
# =====================================

@dp.message(
    F.text == "📢 Xabar yuborish",
    F.from_user.id == ADMIN_ID,
)
async def start_broadcast(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_broadcast)

    await message.answer(
        "📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring.\n"
        "Matn, rasm yoki video bo'lishi mumkin.\n\n"
        "Bekor qilish: /cancel"
    )


async def copy_with_retry(user_id: int, message: types.Message) -> str:
    """Xabarni nusxalaydi. Natija: 'ok', 'blocked' yoki 'failed'.
    Telegram limiti (RetryAfter) bo'lsa, kutib, shu foydalanuvchiga qayta urinadi."""
    for attempt in range(1, BROADCAST_RETRIES + 1):
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            return "ok"

        except TelegramRetryAfter as exc:
            logger.warning(
                "Telegram limiti: user=%s, %s soniya kutiladi (urinish %s/%s)",
                user_id,
                exc.retry_after,
                attempt,
                BROADCAST_RETRIES,
            )
            await asyncio.sleep(exc.retry_after + 1)

        except TelegramForbiddenError:
            return "blocked"

        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "chat not found" in text or "user is deactivated" in text:
                return "blocked"
            logger.warning("Xabar yuborilmadi, user=%s: %s", user_id, exc)
            return "failed"

        except TelegramAPIError as exc:
            logger.warning("Xabar yuborilmadi, user=%s: %s", user_id, exc)
            return "failed"

        except Exception:
            logger.exception("Kutilmagan xatolik, user=%s", user_id)
            return "failed"

    return "failed"


@dp.message(
    AdminStates.waiting_for_broadcast,
    F.from_user.id == ADMIN_ID,
)
async def process_broadcast(message: types.Message, state: FSMContext):
    # Holatni darhol tozalaymiz, shunda qayta ishga tushib ketmaydi
    await state.clear()

    try:
        users = await run_db(db_get_active_users)
    except sqlite3.Error:
        logger.exception("Foydalanuvchilar ro'yxatini olishda xatolik")
        await message.answer(
            "❌ Foydalanuvchilar ro'yxatini olib bo'lmadi.",
            reply_markup=get_admin_keyboard(),
        )
        return

    await message.answer(
        "🚀 Xabar yuborish boshlandi.\n"
        f"Qabul qiluvchilar: {len(users)}"
    )

    sent = 0
    failed = 0
    blocked_ids = []

    for user_id in users:
        result = await copy_with_retry(user_id, message)

        if result == "ok":
            sent += 1
        elif result == "blocked":
            blocked_ids.append(user_id)
        else:
            failed += 1

        await asyncio.sleep(BROADCAST_DELAY)

    try:
        await run_db(db_mark_blocked, blocked_ids)
    except sqlite3.Error:
        logger.exception("Bloklaganlarni belgilashda xatolik")

    await message.answer(
        "📊 Xabar yuborish yakunlandi.\n\n"
        f"✅ Yetkazildi: {sent}\n"
        f"❌ Xatolar: {failed}\n"
        f"🚫 Botni bloklaganlar: {len(blocked_ids)}",
        reply_markup=get_admin_keyboard(),
    )


# =====================================
# KINO QIDIRISH
# =====================================

@dp.message(F.text.regexp(r"^\s*[0-9]{1,19}\s*$"))
async def search_movie(message: types.Message):
    if not message.from_user or not message.text:
        return

    await add_user(message.from_user.id)

    code = parse_int(message.text)

    if code is None:
        await message.answer("❌ Bu kod bo'yicha kino topilmadi.")
        return

    try:
        movie = await run_db(db_get_movie, code)
    except sqlite3.Error:
        logger.exception("Kino qidirishda xatolik: code=%s", code)
        await message.answer(
            "❌ Texnik xatolik yuz berdi. Keyinroq urinib ko'ring."
        )
        return

    if not movie:
        await message.answer("❌ Bu kod bo'yicha kino topilmadi.")
        return

    file_id, caption, is_vip = movie

    if is_vip and not await check_user_vip(message.from_user.id):
        await message.answer(
            "🔒 Bu kino faqat VIP foydalanuvchilar uchun.\n\n"
            "VIP maqomini olish uchun administratorga murojaat qiling."
        )
        return

    try:
        await message.answer_video(
            video=file_id,
            caption=(caption[:MAX_CAPTION] if caption else None),
        )

    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        await message.answer(
            "⏳ Bot band. Iltimos, bir necha soniyadan keyin qayta urinib ko'ring."
        )

    except TelegramAPIError:
        logger.exception("Kino yuborilmadi: code=%s", code)

        await message.answer(
            "❌ Videoni yuborishda xatolik yuz berdi. Keyinroq urinib ko'ring."
        )


# =====================================
# NOMA'LUM MATN (kod emas)
# =====================================

@dp.message(F.text, ~F.text.startswith("/"))
async def unknown_text(message: types.Message):
    if message.from_user and message.from_user.id == ADMIN_ID:
        return

    await message.answer(
        "🎬 Kino ko'rish uchun faqat raqamli kino kodini yuboring.\n"
        "Masalan: 101"
    )


# =====================================
# UMUMIY XATOLIKLAR
# =====================================

@dp.error()
async def global_error_handler(event: ErrorEvent):
    logger.exception(
        "Ushlanmagan xatolik: %s",
        event.exception,
        exc_info=event.exception,
    )
    return True


# =====================================
# ISHGA TUSHIRISH
# =====================================

async def main():
    init_db()
    logger.info("Baza yo'li: %s", os.path.abspath(DB_PATH))

    if not _env_db and not DB_PATH.startswith("/data"):
        logger.warning(
            "DIQQAT: DB_PATH berilmagan. Hostingda fayl tizimi vaqtinchalik "
            "bo'lsa, har deployda ma'lumotlar yo'qoladi. "
            "Persistent disk ulab, DB_PATH=/data/kino_bot.db qiling."
        )

    logger.info("Kino bot ishga tushmoqda...")

    try:
        await bot.delete_webhook(drop_pending_updates=True)

        # Render port talabini qondirish uchun veb-server ishga tushirilmoqda
        asyncio.create_task(start_web_server())

        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())         InlineKeyboardButton(
                    text="⭐ Faqat VIP uchun",
                    callback_data="movie_vip:1",
                )
            ],
        ]
    )


# =====================================
# START
# =====================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not message.from_user:
        return

    await add_user(message.from_user.id)

    is_vip = await check_user_vip(message.from_user.id)
    status = "⭐ VIP obunachi" if is_vip else "👤 Oddiy foydalanuvchi"

    await message.answer(
        f"👋 Salom, {message.from_user.first_name}!\n\n"
        f"Maqomingiz: {status}\n\n"
        "🎬 Kino ko'rish uchun kino kodini yuboring."
    )


# =====================================
# ADMIN PANEL
# =====================================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Sizda admin huquqi yo'q.")
        return

    await state.clear()

    await message.answer(
        "🔑 Admin panelga xush kelibsiz!",
        reply_markup=get_admin_keyboard(),
    )


@dp.message(Command("cancel"))
async def cancel_action(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return

    await state.clear()

    await message.answer(
        "✅ Joriy amal bekor qilindi.",
        reply_markup=get_admin_keyboard(),
    )


@dp.message(
    F.text == "❌ Chiqish",
    F.from_user.id == ADMIN_ID,
)
async def exit_admin(message: types.Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "Admin paneldan chiqdingiz.",
        reply_markup=ReplyKeyboardRemove(),
    )


# =====================================
# STATISTIKA
# =====================================

@dp.message(
    F.text == "📊 Statistika",
    F.from_user.id == ADMIN_ID,
)
async def admin_stats(message: types.Message):
    try:
        total_users, vip_users, blocked_users, total_movies = (
            await run_db(db_get_stats)
        )
    except sqlite3.Error:
        logger.exception("Statistikani olishda xatolik")
        await message.answer("❌ Statistikani olib bo'lmadi.")
        return

    await message.answer(
        "📊 BOT STATISTIKASI\n\n"
        f"👥 Foydalanuvchilar: {total_users}\n"
        f"⭐ VIP foydalanuvchilar: {vip_users}\n"
        f"🚫 Botni bloklaganlar: {blocked_users}\n"
        f"🎬 Kinolar: {total_movies}"
    )


# =====================================
# VIP BERISH
# =====================================

@dp.message(
    F.text == "⭐ VIP berish",
    F.from_user.id == ADMIN_ID,
)
async def start_give_vip(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_give_vip)

    await message.answer(
        "⭐ VIP beriladigan foydalanuvchining Telegram ID raqamini yuboring.\n\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_give_vip,
    F.from_user.id == ADMIN_ID,
)
async def process_give_vip(message: types.Message, state: FSMContext):
    target_id = parse_int(message.text)

    if target_id is None:
        await message.answer("❌ ID faqat raqamlardan iborat bo'lsin (to'g'ri ID kiriting).")
        return

    try:
        await run_db(db_set_vip_status, target_id, 1)
    except sqlite3.Error:
        logger.exception("VIP berishda xatolik")
        await message.answer("❌ Bazaga yozib bo'lmadi.")
        return

    await state.clear()

    try:
        await bot.send_message(
            target_id,
            "🎉 Tabriklaymiz! Sizga VIP maqomi berildi.",
        )
    except TelegramAPIError as exc:
        logger.warning("VIP xabari yuborilmadi, user=%s: %s", target_id, exc)

    await message.answer(
        f"✅ {target_id} foydalanuvchiga VIP berildi.",
        reply_markup=get_admin_keyboard(),
    )


# =====================================
# VIPNI OLIB TASHLASH
# =====================================

@dp.message(
    F.text == "❌ VIPni olib tashlash",
    F.from_user.id == ADMIN_ID,
)
async def start_remove_vip(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_remove_vip)

    await message.answer(
        "VIP maqomi olib tashlanadigan foydalanuvchining ID raqamini yuboring.\n\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_remove_vip,
    F.from_user.id == ADMIN_ID,
)
async def process_remove_vip(message: types.Message, state: FSMContext):
    target_id = parse_int(message.text)

    if target_id is None:
        await message.answer("❌ ID faqat raqamlardan iborat bo'lsin (to'g'ri ID kiriting).")
        return

    try:
        await run_db(db_set_vip_status, target_id, 0)
    except sqlite3.Error:
        logger.exception("VIP olib tashlashda xatolik")
        await message.answer("❌ Bazaga yozib bo'lmadi.")
        return

    await state.clear()

    try:
        await bot.send_message(
            target_id,
            "ℹ️ VIP maqomingiz olib tashlandi.",
        )
    except TelegramAPIError as exc:
        logger.warning("VIP xabari yuborilmadi, user=%s: %s", target_id, exc)

    await message.answer(
        f"✅ {target_id} foydalanuvchidan VIP olib tashlandi.",
        reply_markup=get_admin_keyboard(),
    )


# =====================================
# KINO QO'SHISH
# =====================================

@dp.message(
    F.text == "➕ Kino qo'shish",
    F.from_user.id == ADMIN_ID,
)
async def start_add_movie(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_code)

    await message.answer(
        "🔢 Kino uchun raqamli kod kiriting.\n"
        "Masalan: 101\n\n"
        "Mavjud kod kiritilsa, kino yangilanadi.\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_code,
    F.from_user.id == ADMIN_ID,
)
async def process_code(message: types.Message, state: FSMContext):
    code = parse_int(message.text)

    if code is None:
        await message.answer("❌ Kod faqat raqamlardan iborat bo'lsin (juda uzun bo'lmasin).")
        return

    await state.update_data(movie_code=code)
    await state.set_state(AdminStates.waiting_for_vip_choice)

    await message.answer(
        "🔒 Bu kino kimlar uchun?",
        reply_markup=get_vip_choice_keyboard(),
    )


@dp.callback_query(
    AdminStates.waiting_for_vip_choice,
    F.data.startswith("movie_vip:"),
    F.from_user.id == ADMIN_ID,
)
async def process_vip_choice(callback: types.CallbackQuery, state: FSMContext):
    try:
        is_vip = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError, IndexError):
        await callback.answer("Noto'g'ri tanlov!", show_alert=True)
        return

    if is_vip not in (0, 1):
        await callback.answer("Noto'g'ri tanlov!", show_alert=True)
        return

    await state.update_data(is_vip=is_vip)
    await state.set_state(AdminStates.waiting_for_video)

    if callback.message:
        try:
            await callback.message.edit_text(
                "📹 Endi kino videosini yuboring.\n"
                "Bekor qilish: /cancel"
            )
        except TelegramAPIError:
            await callback.message.answer(
                "📹 Endi kino videosini yuboring.\n"
                "Bekor qilish: /cancel"
            )

    await callback.answer()


@dp.message(
    AdminStates.waiting_for_vip_choice,
    F.from_user.id == ADMIN_ID,
)
async def vip_choice_wrong_input(message: types.Message):
    await message.answer(
        "👆 Iltimos, yuqoridagi tugmalardan birini bosing.\n"
        "Bekor qilish: /cancel",
        reply_markup=get_vip_choice_keyboard(),
    )


@dp.message(
    AdminStates.waiting_for_video,
    F.video,
    F.from_user.id == ADMIN_ID,
)
async def process_video(message: types.Message, state: FSMContext):
    data = await state.get_data()

    movie_code = data.get("movie_code")
    is_vip = data.get("is_vip")

    if movie_code is None or is_vip not in (0, 1):
        await state.clear()

        await message.answer(
            "❌ Saqlash ma'lumotlari topilmadi. Qaytadan boshlang.",
            reply_markup=get_admin_keyboard(),
        )
        return

    caption = message.caption or f"🎬 Kino kodi: {movie_code}"

    try:
        await run_db(
            db_add_movie,
            movie_code,
            message.video.file_id,
            caption,
            is_vip,
        )
    except sqlite3.Error:
        logger.exception("Kino saqlashda xatolik")
        await message.answer("❌ Kinoni bazaga saqlab bo'lmadi.")
        return

    await state.clear()

    movie_type = "⭐ VIP kino" if is_vip else "🌐 Oddiy kino"

    extra = ""
    if message.caption and len(message.caption) > MAX_CAPTION:
        extra = f"\n\n⚠️ Izoh {MAX_CAPTION} belgigacha qisqartirildi."

    await message.answer(
        "✅ Kino saqlandi!\n\n"
        f"Turi: {movie_type}\n"
        f"Kod: {movie_code}{extra}",
        reply_markup=get_admin_keyboard(),
    )


@dp.message(
    AdminStates.waiting_for_video,
    F.from_user.id == ADMIN_ID,
)
async def wrong_video_type(message: types.Message):
    await message.answer(
        "❌ Iltimos, videoni video fayl sifatida yuboring.\n"
        "Bekor qilish: /cancel"
    )


# =====================================
# KINO O'CHIRISH
# =====================================

@dp.message(
    F.text == "🗑 Kino o'chirish",
    F.from_user.id == ADMIN_ID,
)
async def start_delete_movie(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_del_code)

    await message.answer(
        "🗑 O'chiriladigan kino kodini yuboring.\n"
        "Bekor qilish: /cancel"
    )


@dp.message(
    AdminStates.waiting_for_del_code,
    F.from_user.id == ADMIN_ID,
)
async def process_delete_movie(message: types.Message, state: FSMContext):
    code = parse_int(message.text)

   
