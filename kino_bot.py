import asyncio
import logging
import sqlite3
import sys
import os
from os import getenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- SOZLAMALAR ---
TOKEN = getenv("BOT_TOKEN", "8946893622:AAH2bG6kGbHbAyzMrKjVJ0MDoGH-KHuSQdE")
ADMIN_ID = int(getenv("ADMIN_ID", "8738813282")) 

dp = Dispatcher()

# --- FSM HOLATLARI ---
class AddKinoState(StatesGroup):
    waiting_for_code = State()
    waiting_for_title = State()
    waiting_for_photo = State()
    waiting_for_ep_count = State()
    waiting_for_genre = State()
    waiting_for_quality = State()
    waiting_for_dubbing = State()
    is_vip = State()

class DeleteKinoState(StatesGroup):
    waiting_for_code = State()

class AddEpisodeVideoState(StatesGroup):
    waiting_for_code = State()
    waiting_for_ep_num = State()
    waiting_for_video = State()

class BulkEpisodesState(StatesGroup):
    waiting_for_code = State()
    waiting_for_start_num = State()
    waiting_for_videos = State()

class AddShortsState(StatesGroup):
    waiting_for_title = State()
    waiting_for_video = State()

class ChannelState(StatesGroup):
    waiting_for_channel = State()
    waiting_for_remove_channel = State()

class GiveVipState(StatesGroup):
    waiting_for_user_id = State()

class RemoveVipState(StatesGroup):
    waiting_for_user_id = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- MA'LUMOTLAR BAZASI ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    # Kinolar jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kinos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            title TEXT,
            photo_id TEXT,
            episodes_count INTEGER DEFAULT 1,
            genre TEXT DEFAULT 'Sarguzasht',
            quality TEXT DEFAULT '720p - 1080p',
            dubbing TEXT DEFAULT 'Oʻzbekcha',
            is_vip INTEGER DEFAULT 0
        )
    """)
    
    # Qismlar (Videolar) jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kino_code TEXT,
            episode_num INTEGER,
            video_id TEXT,
            UNIQUE(kino_code, episode_num)
        )
    """)
    
    # Shorts jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            file_id TEXT
        )
    """)
    
    # Foydalanuvchilar jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            is_vip INTEGER DEFAULT 0
        )
    """)

    # Ko'p kanalli obuna jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_username TEXT PRIMARY KEY
        )
    """)

    # Reytinglar jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kino_code TEXT,
            user_id INTEGER,
            stars INTEGER,
            UNIQUE(kino_code, user_id)
        )
    """)
    
    conn.commit()
    conn.close()

def add_user(user_id: int, full_name: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, full_name) VALUES (?, ?)", (user_id, full_name))
    conn.commit()
    conn.close()

def get_all_user_ids():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def set_user_vip(user_id: int, status: int) -> bool:
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        conn.close()
        return False
    cursor.execute("UPDATE users SET is_vip = ? WHERE user_id = ?", (status, user_id))
    conn.commit()
    conn.close()
    return True

def is_user_vip(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0] == 1)

def add_kino(code: str, title: str, photo_id: str, ep_count: int, genre: str, quality: str, dubbing: str, is_vip: int = 0) -> bool:
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO kinos (code, title, photo_id, episodes_count, genre, quality, dubbing, is_vip) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (code, title, photo_id, ep_count, genre, quality, dubbing, is_vip))
        conn.commit()
        res = True
    except sqlite3.IntegrityError:
        res = False
    conn.close()
    return res

def add_or_update_episode(kino_code: str, ep_num: int, video_id: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO episodes (kino_code, episode_num, video_id)
        VALUES (?, ?, ?)
        ON CONFLICT(kino_code, episode_num) DO UPDATE SET video_id = excluded.video_id
    """, (kino_code, ep_num, video_id))
    conn.commit()
    conn.close()

def get_episode_video(kino_code: str, ep_num: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT video_id FROM episodes WHERE kino_code = ? AND episode_num = ?", (kino_code, ep_num))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def delete_kino_by_code(code: str) -> bool:
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM kinos WHERE code = ?", (code,))
    cursor.execute("DELETE FROM episodes WHERE kino_code = ?", (code,))
    cursor.execute("DELETE FROM ratings WHERE kino_code = ?", (code,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_kino_by_code(code: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT title, photo_id, episodes_count, genre, quality, dubbing, is_vip 
        FROM kinos WHERE code = ?
    """, (code,))
    result = cursor.fetchone()
    conn.close()
    return result

def search_kino_by_text(query: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    pattern = f"%{query}%"
    cursor.execute("""
        SELECT code, title, is_vip 
        FROM kinos 
        WHERE title LIKE ? OR genre LIKE ? OR code = ?
        LIMIT 10
    """, (pattern, pattern, query))
    results = cursor.fetchall()
    conn.close()
    return results

def add_shorts(title: str, file_id: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO shorts (title, file_id) VALUES (?, ?)", (title, file_id))
    conn.commit()
    conn.close()

def get_random_shorts():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT title, file_id FROM shorts ORDER BY RANDOM() LIMIT 1")
    result = cursor.fetchone()
    conn.close()
    return result

# --- REYTING FUNKSIYALARI ---
def add_rating(kino_code: str, user_id: int, stars: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ratings (kino_code, user_id, stars)
        VALUES (?, ?, ?)
        ON CONFLICT(kino_code, user_id) DO UPDATE SET stars = excluded.stars
    """, (kino_code, user_id, stars))
    conn.commit()
    conn.close()

def get_kino_rating_info(kino_code: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT AVG(stars), COUNT(stars) FROM ratings WHERE kino_code = ?", (kino_code,))
    avg_stars, count = cursor.fetchone()
    conn.close()
    if not count:
        return 5.0, 0
    return round(avg_stars, 1), count

# --- KANALLAR BAZASI FUNKSIYALARI ---
def add_channel_db(channel: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO channels (channel_username) VALUES (?)", (channel,))
    conn.commit()
    conn.close()

def remove_channel_db(channel: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()