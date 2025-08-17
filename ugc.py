import asyncio
import csv
import os
import sqlite3
import time
from typing import Optional, Tuple, List
import html  # для безопасного экранирования имён в HTML-сообщениях
import random
from collections import defaultdict

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.markdown import apply_html_entities  # Добавлен импорт для обработки caption


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

def _env_int_set(name: str, default_csv: str) -> set[int]:
    raw = os.getenv(name, default_csv)
    ids = []
    for s in str(raw).split(","):
        s = s.strip()
        if not s:
            continue
        try:
            ids.append(int(s))
        except ValueError:
            pass
    return set(ids)

# ---- secrets из переменных окружения Render ----
API_TOKEN = os.getenv("API_TOKEN")         # обязательно
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN")   # если используешь @CryptoBot

if not API_TOKEN:
    raise RuntimeError("Не задана переменная окружения API_TOKEN (Settings → Environment → Add Variable).")

# ---- гибкие настройки (можно менять в Render без правки кода) ----
ADMIN_IDS = _env_int_set("ADMIN_IDS", "5194736461")
REF_BONUS = _env_int("REF_BONUS", 1)
BROADCAST_COOLDOWN_SEC = _env_int("BROADCAST_COOLDOWN_SEC", 60)
LEADERS_LIMIT = _env_int("LEADERS_LIMIT", 20)
SHOP_PAGE_SIZE = _env_int("SHOP_PAGE_SIZE", 6)
USERS_PAGE_SIZE = _env_int("USERS_PAGE_SIZE", 20)
SKIP_HATCH_COST = _env_int("SKIP_HATCH_COST", 25)
CLICK_POINTS = _env_float("CLICK_POINTS", 1.0)
CLICK_COOLDOWN = _env_int("CLICK_COOLDOWN", 86400)

CRYPTO_ASSET = os.getenv("CRYPTO_ASSET", "USDT")
CRYPTO_API = os.getenv("CRYPTO_API", "https://pay.crypt.bot/api")

# ---- Telegram bot ----
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ---- База данных ----
DB_PATH = os.getenv("DB_PATH", "rasika_shop.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()


# Создание таблиц, если они не существуют
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    referrer_id INTEGER,
    points REAL DEFAULT 0.0,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    username TEXT,
    full_name TEXT,
    click_ts INTEGER DEFAULT 0
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    user_id INTEGER PRIMARY KEY,
    referrer_id INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price INTEGER NOT NULL
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    item_id INTEGER,
    item_name TEXT NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending / processing / done / canceled
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS crypto_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price_usdt REAL NOT NULL
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS crypto_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    amount_usdt REAL NOT NULL,
    invoice_id INTEGER,
    pay_url TEXT,
    status TEXT NOT NULL DEFAULT 'created',   -- created / paid / delivered / canceled
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS rarities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS egg_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    hatch_time_sec INTEGER NOT NULL DEFAULT 0
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS egg_chances (
    random_egg_id INTEGER,
    egg_id INTEGER,
    chance REAL NOT NULL,
    PRIMARY KEY (random_egg_id, egg_id)
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS pets_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rarity_id INTEGER NOT NULL,
    daily_points REAL NOT NULL,
    photo TEXT,
    chance REAL DEFAULT 1.0
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS pet_evolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pet_id INTEGER NOT NULL,
    required_days INTEGER NOT NULL,
    name TEXT NOT NULL,
    photo TEXT,
    daily_points REAL NOT NULL
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS user_pets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    pet_id INTEGER NOT NULL,
    egg_id INTEGER NOT NULL,
    acquired_at INTEGER DEFAULT (strftime('%s','now')),
    lives INTEGER DEFAULT 10,
    last_feed_day INTEGER DEFAULT 0,
    feed_streak INTEGER DEFAULT 0
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS hatching_eggs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    egg_id INTEGER NOT NULL,
    acquired_at INTEGER DEFAULT (strftime('%s','now'))
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS channels (
    channel TEXT PRIMARY KEY
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS chat_users (
    chat_id INTEGER,
    user_id INTEGER,
    PRIMARY KEY (chat_id, user_id)
);
""")
# Новая таблица для шансов выпадения питомцев из яиц
cur.execute("""
CREATE TABLE IF NOT EXISTS pet_egg_chances (
    pet_id INTEGER,
    egg_id INTEGER,
    chance REAL NOT NULL,
    PRIMARY KEY (pet_id, egg_id)
);
""")
conn.commit()

# ========= УТИЛЫ =========
def db_one(q: str, p: Tuple = ()) -> Optional[Tuple]:
    cur.execute(q, p)
    return cur.fetchone()

def db_all(q: str, p: Tuple = ()) -> List[Tuple]:
    cur.execute(q, p)
    return cur.fetchall()

def db_exec(q: str, p: Tuple = ()) -> None:
    cur.execute(q, p)
    conn.commit()

# Автоматическое добавление недостающих столбцов
def ensure_columns():
    # Проверка и добавление столбцов в таблицу users
    cur.execute("PRAGMA table_info(users)")
    user_columns = {c[1] for c in cur.fetchall()}
    if "click_ts" not in user_columns:
        cur.execute("ALTER TABLE users ADD COLUMN click_ts INTEGER DEFAULT 0")
        print("[DB] Колонка click_ts добавлена в таблицу users")
    # Для user_pets: lives, last_feed_day, feed_streak
    cur.execute("PRAGMA table_info(user_pets)")
    up_columns = {c[1] for c in cur.fetchall()}
    if "lives" not in up_columns:
        cur.execute("ALTER TABLE user_pets ADD COLUMN lives INTEGER DEFAULT 10")
        print("[DB] Добавлена lives в user_pets")
    if "last_feed_day" not in up_columns:
        cur.execute("ALTER TABLE user_pets ADD COLUMN last_feed_day INTEGER DEFAULT 0")
        print("[DB] Добавлена last_feed_day в user_pets")
    if "feed_streak" not in up_columns:
        cur.execute("ALTER TABLE user_pets ADD COLUMN feed_streak INTEGER DEFAULT 0")
        print("[DB] Добавлена feed_streak в user_pets")
    # Для egg_types: hatch_time_sec
    cur.execute("PRAGMA table_info(egg_types)")
    egg_columns = {c[1] for c in cur.fetchall()}
    if "hatch_time_sec" not in egg_columns:
        cur.execute("ALTER TABLE egg_types ADD COLUMN hatch_time_sec INTEGER NOT NULL DEFAULT 0")
        print("[DB] Добавлена hatch_time_sec в egg_types")
    conn.commit()

# Вызываем функцию для проверки и добавления столбцов
ensure_columns()

# Автоматическое создание "Random Egg", если не существует
random_egg = db_one("SELECT id FROM egg_types WHERE name = 'Random Egg'")
if not random_egg:
    db_exec("INSERT INTO egg_types (name, price, hatch_time_sec) VALUES ('Random Egg', 5, 0)")
    print("[DB] Создано яйцо 'Random Egg' с ценой 5 очков")

def display_name(uid: int) -> str:
    row = db_one("SELECT username, full_name FROM users WHERE user_id=?", (uid,))
    if not row:
        return str(uid)
    uname, fname = row
    if uname:
        return f"@{uname}" + (f" ({fname})" if fname else "")
    if fname:
        return f"{fname} ({uid})"
    return str(uid)

def get_channels() -> List[str]:
    return [row[0] for row in db_all("SELECT channel FROM channels")]

def is_require_sub() -> bool:
    row = db_one("SELECT value FROM meta WHERE key='require_sub'")
    return row[0] != '0' if row else True

async def is_subscribed_all(user_id: int) -> bool:
    channels = get_channels()
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if str(member.status) not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True

def not_subscribed_list(user_id: int) -> List[str]:
    return get_channels()

def format_orders_list(orders: List[Tuple]) -> str:
    if not orders:
        return "Пока заказов нет."
    lines = []
    for oid, item_name, price, status, created_at in orders:
        t = time.strftime("%d.%m %H:%M", time.localtime(created_at))
        human = {"pending": "в ожидании", "processing": "в обработке", "done": "выполнен", "canceled": "отменён"}.get(status, status)
        lines.append(f"#{oid} • {item_name} — {price} оч. • <i>{human}</i> • {t}")
    return "\n".join(lines)

def format_orders_list_with_names(orders: List[Tuple]) -> str:
    if not orders:
        return "Пока заказов нет."
    lines = []
    for oid, uid, item_name, price, status, created_at in orders:
        disp = display_name(uid)
        t = time.strftime("%d.%m %H:%M", time.localtime(created_at))
        human = {"pending": "в ожидания", "processing": "в обработке", "done": "выполнен", "canceled": "отменён"}.get(status, status)
        lines.append(f"#{oid} • {disp} • {item_name} — {price} оч. • <i>{human}</i> • {t}")
    return "\n".join(lines)

def format_crypto_orders_with_names(rows: List[Tuple]) -> str:
    if not rows:
        return "Пока крипто‑заказов нет."
    lines = []
    for oid, uid, invid, item, amt, status, ts in rows:
        disp = display_name(uid)
        t = time.strftime("%d.%m %H:%M", time.localtime(ts))
        human = {"created": "создан", "paid": "оплачен", "delivered": "выдан", "canceled": "отменён"}.get(status, status)
        inv = f" (invoice {invid})" if invid else ""
        lines.append(f"#{oid}{inv} • {disp} • {item} — {amt} USDT • <i>{human}</i> • {t}")
    return "\n".join(lines)

async def notify_user(user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass

async def notify_admins(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=reply_markup)
        except Exception:
            pass

def get_pet_current_stage(pet_id: int, acquired_at: int, lives: int) -> Tuple[str, str, float]:
    if lives <= 0:
        return "Мертвый питомец", "", 0.0
    now = time.time()
    days = int((now - acquired_at) // 86400)
    base = db_one("SELECT name, photo, daily_points FROM pets_config WHERE id=?", (pet_id,))
    if not base:
        return "Unknown", "", 0.0
    stages = db_all("SELECT required_days, name, photo, daily_points FROM pet_evolutions WHERE pet_id=? ORDER BY required_days", (pet_id,))
    all_stages = [(0, base[0], base[1], base[2])] + stages
    current = max((stage for stage in all_stages if stage[0] <= days), key=lambda s: s[0])
    return current[1], current[2] or "", current[3]

# ========= КНОПКИ =========
def kb_main(user_id: int) -> InlineKeyboardMarkup:
    bot_username = "stealabrainrot_sbot"  # Updated bot username
    rows = [
        [
            InlineKeyboardButton(text="📊 Мои очки", callback_data="my_points"),
            InlineKeyboardButton(text="🖱 Клик", callback_data="click"),
        ],
        [
            InlineKeyboardButton(text="🛒 Магазин Расика", callback_data="shop:0"),
            InlineKeyboardButton(text="💱 Крипто‑магазин (USDT)", callback_data="cshop:0"),
        ],
        [
            InlineKeyboardButton(text="🧾 Мои заказы", callback_data="my_orders"),
            InlineKeyboardButton(text="🏆 Лидеры", callback_data="leaders"),
        ],
        [
            InlineKeyboardButton(text="👥 Пригласить друзей", callback_data="invite"),
            InlineKeyboardButton(text="🏅 Топ покупателей (Crypto)", callback_data="crypto_top"),
        ],
        [
            InlineKeyboardButton(text="🐾 Мои питомцы", callback_data="my_pets"),
        ],
        [
            InlineKeyboardButton(text="🥚 Магазин яиц", callback_data="egg_shop:0"),
            InlineKeyboardButton(text="🥚 Мои яйца", callback_data="my_eggs"),
        ],
        [
            InlineKeyboardButton(text="➕ Добавить в чат", url=f"https://t.me/{bot_username}?startgroup=true")
        ],
    ]
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton(text="⚙ Управление", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅ На главную", callback_data="main_menu")]
    ])

def kb_subscription_multi() -> InlineKeyboardMarkup:
    channels = get_channels()
    buttons = [[InlineKeyboardButton(text=f"📢 {ch}", url=f"https://t.me/{ch.replace('@','')}")] for ch in channels]
    buttons.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Товары", callback_data="admin_items")],
        [InlineKeyboardButton(text="💱 Крипто‑магазин", callback_data="admin_cshop")],
        [InlineKeyboardButton(text="📦 Заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="➕ Выдать очки", callback_data="admin_give_points")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users:0")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⬇ Экспорт заказов (CSV)", callback_data="admin_export")],
        [InlineKeyboardButton(text="🐶 Управление питомцами", callback_data="admin_pets")],
        [InlineKeyboardButton(text="📢 Каналы подписки", callback_data="admin_channels")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="main_menu")]
    ])

def kb_admin_channels() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_channel")],
        [InlineKeyboardButton(text="✏ Изменить", callback_data="admin_edit_channel")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_del_channel")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_channels")],
        [InlineKeyboardButton(text="🔄 Переключить обязательность", callback_data="admin_toggle_sub")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_menu")]
    ])

def kb_admin_items() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add")],
        [InlineKeyboardButton(text="✏ Изменить", callback_data="admin_edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_del")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_menu")]
    ])

def kb_admin_cshop() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить (USDT)", callback_data="cadmin_add")],
        [InlineKeyboardButton(text="✏ Изменить (USDT)", callback_data="cadmin_edit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="cadmin_del")],
        [InlineKeyboardButton(text="📋 Список", callback_data="cadmin_list")],
        [InlineKeyboardButton(text="🧾 Заказы (последние)", callback_data="cadmin_orders")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_menu")]
    ])

def kb_admin_pets() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡️ Редкости", callback_data="admin_rarities")],
        [InlineKeyboardButton(text="🥚 Яйца", callback_data="admin_eggs")],
        [InlineKeyboardButton(text="🐶 Питомцы", callback_data="admin_pets_config")],
        [InlineKeyboardButton(text="🎁 Выдать питомца", callback_data="admin_give_pet")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_menu")]
    ])

def kb_admin_rarities() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_rarity")],
        [InlineKeyboardButton(text="✏ Изменить", callback_data="admin_edit_rarity")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_del_rarity")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_rarities")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_pets")]
    ])

def kb_admin_eggs() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_egg")],
        [InlineKeyboardButton(text="✏ Изменить", callback_data="admin_edit_egg")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_del_egg")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_eggs")],
        [InlineKeyboardButton(text="✏ Настроить Random Egg", callback_data="admin_random_egg")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_pets")]
    ])

def kb_admin_random_egg() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить шанс", callback_data="admin_add_egg_chance")],
        [InlineKeyboardButton(text="✏ Изменить шанс", callback_data="admin_edit_egg_chance")],
        [InlineKeyboardButton(text="🗑 Удалить шанс", callback_data="admin_del_egg_chance")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_egg_chances")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_eggs")]
    ])

def kb_admin_pets_config() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_pet_config")],
        [InlineKeyboardButton(text="✏ Изменить", callback_data="admin_edit_pet_config")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_del_pet_config")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_pets_config")],
        [InlineKeyboardButton(text="🔄 Эволюции", callback_data="admin_evolutions")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_pets")]
    ])

def kb_admin_evolutions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_evolution")],
        [InlineKeyboardButton(text="✏ Изменить", callback_data="admin_edit_evolution")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_del_evolution")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_evolutions")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="admin_pets_config")]
    ])

def kb_order_status(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟡 В обработке", callback_data=f"order:{order_id}:processing"),
            InlineKeyboardButton(text="✅ Выполнен", callback_data=f"order:{order_id}:done"),
            InlineKeyboardButton(text="❌ Отменён", callback_data=f"order:{order_id}:canceled"),
        ]
    ])

def kb_shop_pagination(page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏮ Назад", callback_data=f"shop:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Далее ⏭", callback_data=f"shop:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅ На главную", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cshop_pagination(page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏮ Назад", callback_data=f"cshop:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Далее ⏭", callback_data=f"cshop:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅ На главную", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_egg_pagination(page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏮ Назад", callback_data=f"egg_shop:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Далее ⏭", callback_data=f"egg_shop:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅ На главную", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_users_pagination(page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏮ Назад", callback_data=f"admin_users:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Далее ⏭", callback_data=f"admin_users:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="📥 Скачать CSV", callback_data="admin_export_users")])
    rows.append([InlineKeyboardButton(text="⬅ В админ‑панель", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_crypto_pay(pay_url: str, order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить USDT", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"chkpay:{order_id}")],
        [InlineKeyboardButton(text="⬅ На главную", callback_data="main_menu")]
    ])

def kb_crypto_admin_delivered(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отметить как выдано", callback_data=f"cdeliver:{order_id}")]
    ])

def kb_my_eggs(user_id: int) -> InlineKeyboardMarkup:
    now = time.time()
    eggs = db_all("""
        SELECT he.id, et.name, et.hatch_time_sec, he.acquired_at
        FROM hatching_eggs he
        JOIN egg_types et ON he.egg_id = et.id
        WHERE he.user_id = ?
    """, (user_id,))
    rows = []
    for he_id, name, hatch_time, acquired_at in eggs:
        ready = (now - acquired_at) >= hatch_time
        if ready:
            rows.append([InlineKeyboardButton(text=f"{name} (готово)", callback_data=f"hatch:{he_id}")])
        else:
            rows.append([InlineKeyboardButton(text=f"{name} (не готово)", callback_data=f"skip_hatch:{he_id}")])
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_my_pets(user_id: int) -> InlineKeyboardMarkup:
    rows = db_all("""
        SELECT up.id, p.name
        FROM user_pets up
        JOIN pets_config p ON up.pet_id = p.id
        WHERE up.user_id = ?
        ORDER BY up.id
    """, (user_id,))
    buttons = []
    for up_id, name in rows:
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"pet_profile:{up_id}")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_pet_profile(up_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍖 Кормить", callback_data=f"feed:{up_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_pet:{up_id}")],
        [InlineKeyboardButton(text="🔄 Передать", callback_data=f"transfer_pet:{up_id}")],
        [InlineKeyboardButton(text="⬅ Назад к питомцам", callback_data="my_pets")]
    ])

# ========= FSM =========
class AddItem(StatesGroup):
    name = State()
    price = State()

class EditItem(StatesGroup):
    id = State()
    name = State()
    price = State()

class DelItem(StatesGroup):
    id = State()

class GivePoints(StatesGroup):
    user_id = State()
    amount = State()

class Broadcast(StatesGroup):
    content = State()
    btn_choice = State()
    btn_text = State()
    btn_url = State()
    confirm = State()

# Крипто FSM
class CAdd(StatesGroup):
    name = State()
    price = State()

class CEdit(StatesGroup):
    id = State()
    name = State()
    price = State()

class CDel(StatesGroup):
    id = State()

class AddRarity(StatesGroup):
    name = State()

class EditRarity(StatesGroup):
    id = State()
    name = State()

class DelRarity(StatesGroup):
    id = State()

class AddEgg(StatesGroup):
    name = State()
    price = State()
    hatch_time_sec = State()

class EditEgg(StatesGroup):
    id = State()
    name = State()
    price = State()
    hatch_time_sec = State()

class DelEgg(StatesGroup):
    id = State()

class AddEggChance(StatesGroup):
    random_egg_id = State()
    egg_id = State()
    chance = State()

class EditEggChance(StatesGroup):
    random_egg_id = State()
    egg_id = State()
    chance = State()

class DelEggChance(StatesGroup):
    random_egg_id = State()
    egg_id = State()

class AddPetConfig(StatesGroup):
    name = State()
    rarity_id = State()
    daily_points = State()
    photo = State()
    chance = State()
    egg_chances = State()  # Новый шаг для шансов по яйцам

class EditPetConfig(StatesGroup):
    id = State()
    name = State()
    rarity_id = State()
    daily_points = State()
    photo = State()
    chance = State()
    egg_chances = State()  # Новый шаг для шансов по яйцам

class DelPetConfig(StatesGroup):
    id = State()

class AddEvolution(StatesGroup):
    pet_id = State()
    required_days = State()
    name = State()
    photo = State()
    daily_points = State()

class EditEvolution(StatesGroup):
    id = State()
    required_days = State()
    name = State()
    photo = State()
    daily_points = State()

class DelEvolution(StatesGroup):
    id = State()

class AddChannel(StatesGroup):
    channel = State()

class EditChannel(StatesGroup):
    old_channel = State()
    new_channel = State()

class DelChannel(StatesGroup):
    channel = State()

class TransferPet(StatesGroup):
    target = State()

class GivePet(StatesGroup):
    user_id = State()
    pet_id = State()

# ========= CRYPTO BOT API =========
async def crypto_create_invoice(amount: float, description: str) -> Optional[dict]:
    if not CRYPTO_TOKEN or CRYPTO_TOKEN.startswith("PUT_"):
        return None
    url = f"{CRYPTO_API}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    data = {
        "asset": CRYPTO_ASSET,
        "amount": str(amount),
        "description": description,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, data=data) as r:
            js = await r.json()
            if js.get("ok") and js.get("result"):
                return js["result"]
            return None

async def crypto_get_invoice(invoice_id: int) -> Optional[dict]:
    if not CRYPTO_TOKEN or CRYPTO_TOKEN.startswith("PUT_"):
        return None
    url = f"{CRYPTO_API}/getInvoices?invoice_ids={invoice_id}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            js = await r.json()
            if not js.get("ok"):
                return None
            res = js.get("result", {}).get("items", [])
            return res[0] if res else None

async def auto_check_crypto_orders():
    while True:
        await asyncio.sleep(60)  # Проверять каждую минуту
        orders = db_all("SELECT id, invoice_id, user_id, item_name, amount_usdt FROM crypto_orders WHERE status = 'created'")
        for order_id, invoice_id, uid, item_name, amount in orders:
            inv = await crypto_get_invoice(invoice_id)
            if inv and inv['status'] == 'paid':
                db_exec("UPDATE crypto_orders SET status = 'paid' WHERE id = ?", (order_id,))
                await notify_user(uid, f"✅ Оплата подтверждена автоматически для заказа #{order_id} — {item_name}")
                await notify_admins(
                    f"✅ Авто-проверка: Оплата получена по крипто‑заказу #{order_id} (invoice {invoice_id})\n"
                    f"Пользователь: {display_name(uid)} / {uid}\n"
                    f"Товар: {item_name} — {amount:.2f} USDT",
                    reply_markup=kb_crypto_admin_delivered(order_id)
                )

# ========= START / SUB =========
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    uid = user.id
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id == uid:
            referrer_id = None

    db_exec("INSERT OR IGNORE INTO users (user_id, referrer_id, username, full_name) VALUES (?, ?, ?, ?)",
            (uid, referrer_id, user.username, user.full_name))
    db_exec("UPDATE users SET username=?, full_name=? WHERE user_id=?", (user.username, user.full_name, uid))

    if is_require_sub() and not await is_subscribed_all(uid):
        channels_text = "\n".join([f"• {ch}" for ch in not_subscribed_list(uid)])
        await message.answer(
            "❗ Чтобы пользоваться <b>Магазином Расика</b>, подпишитесь на каналы:\n"
            f"{channels_text}\n\n"
            "После подписки нажмите «✅ Я подписался».",
            reply_markup=kb_subscription_multi()
        )
        return

    if referrer_id and db_one("SELECT 1 FROM referrals WHERE user_id = ?", (uid,)) is None:
        db_exec("INSERT INTO referrals (user_id, referrer_id) VALUES (?, ?)", (uid, referrer_id))
        db_exec("UPDATE users SET points = points + ? WHERE user_id = ?", (REF_BONUS, referrer_id))
        await notify_user(referrer_id, f"🎉 По вашей ссылке пришёл {display_name(uid)}! +{REF_BONUS} очко")

    await message.answer("🛍 Добро пожаловать в <b>Магазин Расика</b>!", reply_markup=kb_main(uid))

@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: types.CallbackQuery):
    uid = callback.from_user.id
    db_exec("UPDATE users SET username=?, full_name=? WHERE user_id=?", (callback.from_user.username, callback.from_user.full_name, uid))

    if is_require_sub() and not await is_subscribed_all(uid):
        await callback.answer("❗ Вы ещё не подписались на все каналы.", show_alert=True)
        return

    ref_row = db_one("SELECT referrer_id FROM users WHERE user_id = ?", (uid,))
    referrer_id = ref_row[0] if ref_row else None
    if referrer_id and db_one("SELECT 1 FROM referrals WHERE user_id = ?", (uid,)) is None:
        db_exec("INSERT INTO referrals (user_id, referrer_id) VALUES (?, ?)", (uid, referrer_id))
        db_exec("UPDATE users SET points = points + ? WHERE user_id = ?", (REF_BONUS, referrer_id))
        await notify_user(referrer_id, f"🎉 По вашей ссылке подписался {display_name(uid)}! +{REF_BONUS} очко")

    await callback.message.edit_text("✅ Подписка подтверждена! Добро пожаловать в <b>Магазин Расика</b>.",
                                     reply_markup=kb_main(uid))

# ========= ОБЩЕЕ МЕНЮ =========
@dp.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    db_exec("UPDATE users SET username=?, full_name=? WHERE user_id=?", (callback.from_user.username, callback.from_user.full_name, callback.from_user.id))
    await state.clear()
    await callback.message.edit_text("🛍 Добро пожаловать в <b>Магазин Расика</b>!",
                                     reply_markup=kb_main(callback.from_user.id))

# аккуратный вывод очков
@dp.callback_query(F.data == "my_points")
async def my_points(callback: types.CallbackQuery):
    row = db_one("SELECT COALESCE(points, 0) FROM users WHERE user_id = ?", (callback.from_user.id,))
    points = row[0] if row else 0.0
    pts_str = f"{points:.2f}".rstrip("0").rstrip(".")
    await callback.message.edit_text(f"📊 У вас: <b>{pts_str}</b> очков.", reply_markup=kb_back_main())

@dp.callback_query(F.data == "invite")
async def invite(callback: types.CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    await callback.message.edit_text(
        f"👥 Приглашайте друзей (также можно приглашать людей из любых чатов!) и получайте <b>{REF_BONUS}</b> очко за каждого, кто подпишется на все каналы.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>",
        reply_markup=kb_back_main()
    )

@dp.callback_query(F.data == "crypto_top")
async def crypto_top(callback: types.CallbackQuery):
    rows = db_all("""
        SELECT user_id, SUM(amount_usdt) as total
        FROM crypto_orders
        WHERE status IN ('paid', 'delivered')
        GROUP BY user_id
        ORDER BY total DESC
        LIMIT 20
    """)
    if not rows:
        await callback.message.edit_text("🏅 Пока нет оплаченных крипто-заказов.", reply_markup=kb_back_main())
        return
    lines = ["🏅 <b>Топ покупателей (Crypto)</b>:"]
    for i, (uid, total) in enumerate(rows, start=1):
        lines.append(f"{i}. {display_name(uid)} / {uid} — <b>{total:.2f}</b> USDT")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb_back_main())

# ========= ЛИДЕРЫ =========
@dp.callback_query(F.data == "leaders")
async def leaders_cb(callback: types.CallbackQuery):
    rows = db_all("""
        SELECT user_id, points
        FROM users
        ORDER BY points DESC, user_id ASC
        LIMIT ?
    """, (LEADERS_LIMIT,))
    if not rows:
        await callback.message.edit_text("🏆 Пока некому соревноваться.", reply_markup=kb_back_main())
        return
    lines = ["🏆 <b>Таблица лидеров</b>:"]
    for i, (uid, pts) in enumerate(rows, start=1):
        lines.append(f"{i}. {display_name(uid)} — <b>{pts:.2f}</b> оч.")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb_back_main())

# ========= КЛИК =========
@dp.callback_query(F.data == "click")
async def click_cb(callback: types.CallbackQuery):
    uid = callback.from_user.id
    row = db_one("SELECT click_ts FROM users WHERE user_id=?", (uid,))
    last_click = row[0] if row else 0
    now = time.time()
    current_day = int(now / 86400)
    last_day = int(last_click / 86400)
    if current_day == last_day:
        await callback.answer("Вы уже кликнули сегодня. Попробуйте завтра.")
        return
    db_exec("UPDATE users SET points = points + ?, click_ts = ? WHERE user_id=?", (CLICK_POINTS, now, uid))
    await callback.answer(f"Клик! +{CLICK_POINTS} очко")
    await callback.message.edit_text("🖱 Вы кликнули и получили очки!", reply_markup=kb_back_main())

# ========= МОИ ЗАКАЗЫ =========
@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    uid = callback.from_user.id
    orders = db_all("""
        SELECT id, item_name, price, status, created_at
        FROM orders
        WHERE user_id = ?
        ORDER BY id DESC
    """, (uid,))
    text = "🧾 <b>Мои заказы</b>:\n" + format_orders_list(orders) if orders else "Пока заказов нет."
    await callback.message.edit_text(text, reply_markup=kb_back_main())

# ========= МАГА ЗИН (очки, пагинация) =========
def get_items_page(page: int) -> Tuple[List[Tuple[int, str, int]], int]:
    total = db_one("SELECT COUNT(*) FROM items")[0]
    total_pages = max(1, (total + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * SHOP_PAGE_SIZE
    items = db_all("SELECT id, name, price FROM items ORDER BY id LIMIT ? OFFSET ?",
                   (SHOP_PAGE_SIZE, offset))
    return items, total_pages

@dp.callback_query(F.data.startswith("shop:"))
async def shop(callback: types.CallbackQuery):
    if is_require_sub() and not await is_subscribed_all(callback.from_user.id):
        channels_text = "\n".join([f"• {ch}" for ch in not_subscribed_list(callback.from_user.id)])
        await callback.message.edit_text(
            "❗ Чтобы пользоваться <b>Магазином Расика</b>, подпишитесь на каналы:\n"
            f"{channels_text}\n\nПосле подписки нажмите «✅ Я подписался».",
            reply_markup=kb_subscription_multi()
        )
        return
    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    total = db_one("SELECT COUNT(*) FROM items")[0]
    if total == 0:
        await callback.message.edit_text("🛒 Пока товаров нет.", reply_markup=kb_back_main())
        return

    items, total_pages = get_items_page(page)
    rows = [[InlineKeyboardButton(text=f"{name} — {price} оч.", callback_data=f"buy:{iid}")]
            for (iid, name, price) in items]
    kb = InlineKeyboardMarkup(inline_keyboard=rows + kb_shop_pagination(page, total_pages).inline_keyboard)
    await callback.message.edit_text("🛒 <b>Магазин Расика</b> (очки):", reply_markup=kb)

@dp.callback_query(F.data.startswith("buy:"))
async def buy(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    item_id = int(parts[1])
    item_row = db_one("SELECT name, price FROM items WHERE id=?", (item_id,))
    if not item_row:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    item_name, price = item_row
    user_points_row = db_one("SELECT points FROM users WHERE user_id=?", (uid,))
    points = user_points_row[0] if user_points_row else 0.0
    if points < price:
        await callback.answer("Недостаточно очков!", show_alert=True)
        return
    db_exec("UPDATE users SET points = points - ? WHERE user_id=?", (price, uid))
    db_exec("INSERT INTO orders (user_id, item_id, item_name, price) VALUES (?, ?, ?, ?)",
            (uid, item_id, item_name, price))
    order_id = cur.lastrowid
    text = f"🧾 Заказ #{order_id} создан: <b>{item_name}</b> за {price} очков.\n" \
           f"Ожидайте обработки администратором."
    kb_notify = kb_order_status(order_id)
    await callback.message.edit_text(text, reply_markup=kb_back_main())
    await notify_admins(f"📦 Новый заказ #{order_id} от {display_name(uid)}: {item_name} ({price} очков)", reply_markup=kb_notify)

# ========= МАГАЗИН ЯИЦ =========
def get_egg_page(page: int) -> Tuple[List[Tuple[int, str, int]], int]:
    total = db_one("SELECT COUNT(*) FROM egg_types")[0]
    total_pages = max(1, (total + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * SHOP_PAGE_SIZE
    eggs = db_all("SELECT id, name, price FROM egg_types ORDER BY id LIMIT ? OFFSET ?", (SHOP_PAGE_SIZE, offset))
    return eggs, total_pages

@dp.callback_query(F.data.startswith("egg_shop:"))
async def egg_shop(callback: types.CallbackQuery):
    if is_require_sub() and not await is_subscribed_all(callback.from_user.id):
        channels_text = "\n".join([f"• {ch}" for ch in not_subscribed_list(callback.from_user.id)])
        await callback.message.edit_text(
            "❗ Чтобы пользоваться <b>Магазином Расика</b>, подпишитесь на каналы:\n"
            f"{channels_text}\n\nПосле подписки нажмите «✅ Я подписался».",
            reply_markup=kb_subscription_multi()
        )
        return
    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    total = db_one("SELECT COUNT(*) FROM egg_types")[0]
    if total == 0:
        await callback.message.edit_text("🥚 Пока яиц нет.", reply_markup=kb_back_main())
        return

    eggs, total_pages = get_egg_page(page)
    rows = [[InlineKeyboardButton(text=f"{name} — {price} оч.", callback_data=f"buy_egg:{iid}")]
            for (iid, name, price) in eggs]
    kb = InlineKeyboardMarkup(inline_keyboard=rows + kb_egg_pagination(page, total_pages).inline_keyboard)
    await callback.message.edit_text("🥚 <b>Магазин яиц</b> (очки):", reply_markup=kb)

@dp.callback_query(F.data.startswith("buy_egg:"))
async def buy_egg(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    egg_id = int(parts[1])
    egg_row = db_one("SELECT name, price FROM egg_types WHERE id=?", (egg_id,))
    if not egg_row:
        await callback.answer("Яйцо не найдено.", show_alert=True)
        return
    egg_name, price = egg_row
    user_points_row = db_one("SELECT points FROM users WHERE user_id=?", (uid,))
    points = user_points_row[0] if user_points_row else 0.0
    if points < price:
        await callback.answer("Недостаточно очков!", show_alert=True)
        return
    db_exec("UPDATE users SET points = points - ? WHERE user_id=?", (price, uid))
    db_exec("INSERT INTO hatching_eggs (user_id, egg_id) VALUES (?, ?)", (uid, egg_id))
    text = f"🥚 Яйцо '{egg_name}' куплено!"
    await callback.message.edit_text(text, reply_markup=kb_back_main())

# ========= МОИ ЯЙЦА =========
@dp.callback_query(F.data == "my_eggs")
async def my_eggs(callback: types.CallbackQuery):
    uid = callback.from_user.id
    eggs = db_all("""
        SELECT he.id, et.name, et.hatch_time_sec, he.acquired_at
        FROM hatching_eggs he
        JOIN egg_types et ON he.egg_id = et.id
        WHERE he.user_id = ?
    """, (uid,))
    if not eggs:
        await callback.message.edit_text("🥚 У вас нет яиц.", reply_markup=kb_back_main())
        return
    lines = ["🥚 <b>Мои яйца</b>:"]
    for he_id, name, hatch_time, acquired_at in eggs:
        remain = max(0, hatch_time - (time.time() - acquired_at))
        if remain > 0:
            lines.append(f"{name} (вылупится через {int(remain)} сек)")
        else:
            lines.append(f"{name} (готово к открытию)")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb_my_eggs(uid))

@dp.callback_query(F.data.startswith("hatch:"))
async def hatch_egg_cb(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    he_id = int(parts[1])
    row = db_one("SELECT egg_id, acquired_at FROM hatching_eggs WHERE id=? AND user_id=?", (he_id, uid))
    if not row:
        await callback.answer("Яйцо не найдено или не ваше.", show_alert=True)
        return
    egg_id, acquired_at = row
    hatch_time = db_one("SELECT hatch_time_sec FROM egg_types WHERE id=?", (egg_id,))[0]
    if (time.time() - acquired_at) < hatch_time:
        await callback.answer("Яйцо ещё не готово.", show_alert=True)
        return
    await hatch_egg(he_id, uid)
    await callback.message.edit_text("🥚 Яйцо вылуплено!", reply_markup=kb_back_main())

@dp.callback_query(F.data.startswith("skip_hatch:"))
async def skip_hatch_cb(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    he_id = int(parts[1])
    row = db_one("SELECT egg_id, acquired_at FROM hatching_eggs WHERE id=? AND user_id=?", (he_id, uid))
    if not row:
        await callback.answer("Яйцо не найдено или не ваше.", show_alert=True)
        return
    user_points = db_one("SELECT points FROM users WHERE user_id=?", (uid,))[0] or 0.0
    if user_points < SKIP_HATCH_COST:
        await callback.answer("Недостаточно очков для пропуска.", show_alert=True)
        return
    db_exec("UPDATE users SET points = points - ? WHERE user_id=?", (SKIP_HATCH_COST, uid))
    await hatch_egg(he_id, uid)
    await callback.message.edit_text("🥚 Яйцо вылуплено (пропуск за 25 очков)!", reply_markup=kb_back_main())

async def hatch_egg(he_id: int, uid: int):
    egg_id = db_one("SELECT egg_id FROM hatching_eggs WHERE id=?", (he_id,))[0]
    # Проверка на Random Egg
    random_egg_id = db_one("SELECT id FROM egg_types WHERE name='Random Egg'")[0]
    if egg_id == random_egg_id:
        chances = db_all("SELECT egg_id, chance FROM egg_chances WHERE random_egg_id=?", (egg_id,))
        if chances:
            egg_ids = [row[0] for row in chances]
            egg_chances = [row[1] for row in chances]
            sum_ch = sum(egg_chances)
            if sum_ch > 0:
                rolled_egg_id = random.choices(egg_ids, weights=egg_chances)[0]
                rolled_name = db_one("SELECT name FROM egg_types WHERE id=?", (rolled_egg_id,))[0]
                await notify_user(uid, f"🥚 Ваше Random Egg вылупилось в {rolled_name}!")
                egg_id = rolled_egg_id  # Теперь вылупляем из rolled_egg_id

    # Выбор питомца по pet_egg_chances
    pet_rows = db_all("SELECT pet_id, chance FROM pet_egg_chances WHERE egg_id=?", (egg_id,))
    if not pet_rows:
        # Fallback: использовать все питомцы из pets_config с их chance
        pet_rows = db_all("SELECT id, chance FROM pets_config")
        if not pet_rows:
            await notify_user(uid, "❗ Нет настроенных питомцев.")
            return

    pet_ids = [row[0] for row in pet_rows]
    pet_chances = [row[1] for row in pet_rows]
    sum_pch = sum(pet_chances)
    if sum_pch <= 0:
        pet_id = random.choice(pet_ids)
    else:
        pet_id = random.choices(pet_ids, weights=pet_chances)[0]
    acquired_at = db_one("SELECT acquired_at FROM hatching_eggs WHERE id=?", (he_id,))[0]
    db_exec("INSERT INTO user_pets (user_id, pet_id, egg_id, acquired_at) VALUES (?, ?, ?, ?)", (uid, pet_id, egg_id, acquired_at))
    db_exec("DELETE FROM hatching_eggs WHERE id=?", (he_id,))
    pet_row = db_one("SELECT name, daily_points FROM pets_config WHERE id=?", (pet_id,))
    rar_row = db_one("SELECT r.name FROM pets_config p JOIN rarities r ON p.rarity_id = r.id WHERE p.id=?", (pet_id,))
    rar_name = rar_row[0] if rar_row else "Unknown"
    pet_name, daily_points = pet_row
    await notify_user(uid, f"🎉 Из яйца вылупилось: {pet_name} ({rar_name}), +{daily_points:.2f} оч./день!")

# ========= МОИ ПИТОМЦЫ =========
@dp.callback_query(F.data == "my_pets")
async def my_pets(callback: types.CallbackQuery):
    uid = callback.from_user.id
    pets = db_all("SELECT id, pet_id, acquired_at, lives FROM user_pets WHERE user_id=?", (uid,))
    if not pets:
        await callback.message.edit_text("🐾 У вас нет питомцев.", reply_markup=kb_back_main())
        return
    lines = ["🐾 <b>Мои питомцы</b>:"]
    for up_id, pet_id, acquired_at, lives in pets:
        name, photo, daily_points = get_pet_current_stage(pet_id, acquired_at, lives)
        lines.append(f"#{up_id}: {name} — {daily_points:.2f} оч./день, жизней: {lives}")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb_my_pets(uid))

@dp.callback_query(F.data.startswith("pet_profile:"))
async def pet_profile(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    up_id = int(parts[1])
    row = db_one("SELECT user_id, pet_id, acquired_at, lives, last_feed_day, feed_streak FROM user_pets WHERE id=?", (up_id,))
    if not row:
        await callback.answer("Питомец не найден.", show_alert=True)
        return
    uid, pet_id, acquired_at, lives, last_day, streak = row
    if uid != callback.from_user.id:
        await callback.answer("Это не ваш питомец.", show_alert=True)
        return
    name, photo, daily_points = get_pet_current_stage(pet_id, acquired_at, lives)
    now_day = int(time.time() / 86400)
    fed_today = last_day == now_day
    text = f"🐶 <b>{name}</b>\n" \
           f"Очки/день: {daily_points:.2f}\n" \
           f"Жизней: {lives}/10\n" \
           f"Стрик кормления: {streak}\n" \
           f"Кормлен сегодня: {'да' if fed_today else 'нет'}"
    if photo:
        await callback.message.delete()
        await callback.message.answer_photo(photo, caption=text, reply_markup=kb_pet_profile(up_id))
    else:
        await callback.message.edit_text(text, reply_markup=kb_pet_profile(up_id))

@dp.callback_query(F.data.startswith("feed:"))
async def feed_pet_cb(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    up_id = int(parts[1])
    row = db_one("SELECT user_id, lives, last_feed_day, feed_streak FROM user_pets WHERE id=?", (up_id,))
    if not row:
        await callback.answer("Питомец не найден.", show_alert=True)
        return
    uid, lives, last_day, streak = row
    if uid != callback.from_user.id:
        await callback.answer("Это не ваш питомец.", show_alert=True)
        return
    if lives <= 0:
        await callback.answer("Питомец мёртв.", show_alert=True)
        return
    now_day = int(time.time() / 86400)
    if last_day == now_day:
        await callback.answer("Уже кормили сегодня.", show_alert=True)
        return
    new_streak = streak + 1 if last_day == now_day - 1 else 1
    db_exec("UPDATE user_pets SET last_feed_day=?, feed_streak=? WHERE id=?", (now_day, new_streak, up_id))
    await callback.answer("🍖 Питомец накормлен!")
    await pet_profile(callback)

@dp.callback_query(F.data.startswith("delete_pet:"))
async def delete_pet_cb(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    up_id = int(parts[1])
    row = db_one("SELECT user_id FROM user_pets WHERE id=?", (up_id,))
    if not row:
        await callback.answer("Питомец не найден.", show_alert=True)
        return
    uid = row[0]
    if uid != callback.from_user.id:
        await callback.answer("Это не ваш питомец.", show_alert=True)
        return
    db_exec("DELETE FROM user_pets WHERE id=?", (up_id,))
    await callback.answer("🗑 Питомец удалён.")
    await my_pets(callback)

@dp.callback_query(F.data.startswith("transfer_pet:"))
async def transfer_pet_start(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    up_id = int(parts[1])
    row = db_one("SELECT user_id FROM user_pets WHERE id=?", (up_id,))
    if not row:
        await callback.answer("Питомец не найден.", show_alert=True)
        return
    uid = row[0]
    if uid != callback.from_user.id:
        await callback.answer("Это не ваш питомец.", show_alert=True)
        return
    await state.update_data(up_id=up_id)
    await state.set_state(TransferPet.target)
    await callback.message.edit_text("🔄 Введите user_id получателя:", reply_markup=kb_back_main())

@dp.message(TransferPet.target)
async def transfer_pet_target(msg: types.Message, state: FSMContext):
    try:
        target_uid = int(msg.text.strip())
    except:
        await msg.answer("User ID - число.")
        return
    if not db_one("SELECT 1 FROM users WHERE user_id=?", (target_uid,)):
        await msg.answer("Получатель не найден.")
        return
    data = await state.get_data()
    up_id = data["up_id"]
    db_exec("UPDATE user_pets SET user_id=? WHERE id=?", (target_uid, up_id))
    await notify_user(target_uid, "🔄 Вам передали питомца!")
    await state.clear()
    await msg.answer("✅ Питомец передан.", reply_markup=kb_back_main())

# ========= CRYPTO SHOP =========
def get_crypto_items_page(page: int) -> Tuple[List[Tuple[int, str, float]], int]:
    total = db_one("SELECT COUNT(*) FROM crypto_items")[0]
    total_pages = max(1, (total + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * SHOP_PAGE_SIZE
    items = db_all("SELECT id, name, price_usdt FROM crypto_items ORDER BY id LIMIT ? OFFSET ?", (SHOP_PAGE_SIZE, offset))
    return items, total_pages

@dp.callback_query(F.data.startswith("cshop:"))
async def cshop(callback: types.CallbackQuery):
    if is_require_sub() and not await is_subscribed_all(callback.from_user.id):
        channels_text = "\n".join([f"• {ch}" for ch in not_subscribed_list(callback.from_user.id)])
        await callback.message.edit_text(
            "❗ Чтобы пользоваться <b>Магазином Расика</b>, подпишитесь на каналы:\n"
            f"{channels_text}\n\nПосле подписки нажмите «✅ Я подписался».",
            reply_markup=kb_subscription_multi()
        )
        return
    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    total = db_one("SELECT COUNT(*) FROM crypto_items")[0]
    if total == 0:
        await callback.message.edit_text("💱 Пока товаров в крипто-магазине нет.", reply_markup=kb_back_main())
        return

    items, total_pages = get_crypto_items_page(page)
    rows = [[InlineKeyboardButton(text=f"{name} — {price:.2f} USDT", callback_data=f"buy_c:{iid}")]
            for (iid, name, price) in items]
    kb = InlineKeyboardMarkup(inline_keyboard=rows + kb_cshop_pagination(page, total_pages).inline_keyboard)
    await callback.message.edit_text("💱 <b>Крипто-магазин</b> (USDT):", reply_markup=kb)

@dp.callback_query(F.data.startswith("buy_c:"))
async def buy_crypto(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    item_id = int(parts[1])
    item_row = db_one("SELECT name, price_usdt FROM crypto_items WHERE id=?", (item_id,))
    if not item_row:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    item_name, price = item_row
    description = f"Заказ в Магазине Расика: {item_name}"
    inv = await crypto_create_invoice(price, description)
    if not inv:
        await callback.answer("Ошибка создания инвойса. Попробуйте позже.", show_alert=True)
        return
    invoice_id = inv["invoice_id"]
    pay_url = inv["pay_url"]
    db_exec("INSERT INTO crypto_orders (user_id, item_id, item_name, amount_usdt, invoice_id, pay_url) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, item_id, item_name, price, invoice_id, pay_url))
    order_id = cur.lastrowid
    text = f"💱 Заказ #{order_id} создан: <b>{item_name}</b> за {price:.2f} USDT.\nОплатите по ссылке."
    await callback.message.edit_text(text, reply_markup=kb_crypto_pay(pay_url, order_id))
    await notify_admins(f"💱 Новый крипто-заказ #{order_id} от {display_name(uid)}: {item_name} ({price:.2f} USDT)\nInvoice {invoice_id}")

@dp.callback_query(F.data.startswith("chkpay:"))
async def check_pay_cb(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    order_id = int(parts[1])
    row = db_one("SELECT invoice_id, status FROM crypto_orders WHERE id=?", (order_id,))
    if not row:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    invoice_id, status = row
    if status != 'created':
        await callback.answer(f"Статус: {status}", show_alert=True)
        return
    inv = await crypto_get_invoice(invoice_id)
    if inv and inv['status'] == 'paid':
        db_exec("UPDATE crypto_orders SET status = 'paid' WHERE id = ?", (order_id,))
        await callback.message.edit_text("✅ Оплата подтверждена! Ожидайте выдачи.")
        await notify_admins(f"✅ Оплата по крипто-заказу #{order_id} (invoice {invoice_id})\nПользователь: {display_name(callback.from_user.id)}",
                            reply_markup=kb_crypto_admin_delivered(order_id))
    else:
        await callback.answer("Оплата ещё не подтверждена.")

@dp.callback_query(F.data.startswith("cdeliver:"))
async def deliver_crypto_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только админ.", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    db_exec("UPDATE crypto_orders SET status = 'delivered' WHERE id = ?", (order_id,))
    row = db_one("SELECT user_id, item_name FROM crypto_orders WHERE id=?", (order_id,))
    if row:
        uid, item_name = row
        await notify_user(uid, f"✅ Ваш крипто-заказ #{order_id} ({item_name}) выдан!")
    await callback.message.edit_text("✅ Отмечено как выдано.")

# ========= АДМИН: ТОВАРЫ =========
@dp.callback_query(F.data == "admin_items")
async def admin_items(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    await cb.message.edit_text("🛍 Управление товарами", reply_markup=kb_admin_items())

@dp.callback_query(F.data == "admin_list")
async def admin_list_items(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    rows = db_all("SELECT id, name, price FROM items ORDER BY id")
    if not rows:
        await cb.message.edit_text("📋 Товаров нет.", reply_markup=kb_admin_items())
        return
    lines = ["📋 <b>Товары</b>:"]
    for iid, name, price in rows:
        lines.append(f"#{iid}: {name} — {price} оч.")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_items())

@dp.callback_query(F.data == "admin_add")
async def admin_add_item_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(AddItem.name)
    await cb.message.edit_text("➕ Введите название товара:", reply_markup=kb_back_main())

@dp.message(AddItem.name)
async def admin_add_item_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым.")
        return
    await state.update_data(name=name)
    await state.set_state(AddItem.price)
    await msg.answer("Цена (int >0):", reply_markup=kb_back_main())

@dp.message(AddItem.price)
async def admin_add_item_price(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        price = int(msg.text.strip())
        if price <= 0:
            raise ValueError
    except:
        await msg.answer("Цена - целое >0.")
        return
    data = await state.get_data()
    db_exec("INSERT INTO items (name, price) VALUES (?, ?)", (data["name"], price))
    await state.clear()
    await msg.answer("✅ Товар добавлен.", reply_markup=kb_admin_items())

@dp.callback_query(F.data == "admin_edit")
async def admin_edit_item_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(EditItem.id)
    await cb.message.edit_text("✏ Введите ID товара:", reply_markup=kb_back_main())

@dp.message(EditItem.id)
async def admin_edit_item_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        iid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM items WHERE id=?", (iid,)):
        await msg.answer("Товар не найден.")
        return
    await state.update_data(id=iid)
    await state.set_state(EditItem.name)
    await msg.answer("Новое название (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditItem.name)
async def admin_edit_item_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    await state.update_data(name=None if name == '.' else name)
    await state.set_state(EditItem.price)
    await msg.answer("Новая цена (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditItem.price)
async def admin_edit_item_price(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    price = None
    if txt != '.':
        try:
            price = int(txt)
            if price <= 0:
                raise ValueError
        except:
            await msg.answer("Цена - целое >0.")
            return
    data = await state.get_data()
    iid = data["id"]
    updates = []
    params = []
    if data["name"] is not None:
        updates.append("name=?")
        params.append(data["name"])
    if price is not None:
        updates.append("price=?")
        params.append(price)
    if not updates:
        await state.clear()
        await msg.answer("Ничего не изменено.", reply_markup=kb_admin_items())
        return
    q = "UPDATE items SET " + ", ".join(updates) + " WHERE id=?"
    params.append(iid)
    db_exec(q, tuple(params))
    await state.clear()
    await msg.answer("✅ Товар обновлён.", reply_markup=kb_admin_items())

@dp.callback_query(F.data == "admin_del")
async def admin_del_item_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(DelItem.id)
    await cb.message.edit_text("🗑 Введите ID товара для удаления:", reply_markup=kb_back_main())

@dp.message(DelItem.id)
async def admin_del_item_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        iid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    row = db_one("SELECT name FROM items WHERE id=?", (iid,))
    if not row:
        await msg.answer("Товар не найден.")
        return
    db_exec("DELETE FROM items WHERE id=?", (iid,))
    await state.clear()
    await msg.answer(f"🗑 Удалён товар '{row[0]}'.", reply_markup=kb_admin_items())

# --- Админ: Крипто-магазин
@dp.callback_query(F.data == "admin_cshop")
async def admin_cshop(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    await cb.message.edit_text("💱 Управление крипто-магазином", reply_markup=kb_admin_cshop())

@dp.callback_query(F.data == "cadmin_list")
async def cadmin_list_items(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    rows = db_all("SELECT id, name, price_usdt FROM crypto_items ORDER BY id")
    if not rows:
        await cb.message.edit_text("📋 Товаров нет.", reply_markup=kb_admin_cshop())
        return
    lines = ["📋 <b>Крипто-товары</b>:"]
    for iid, name, price in rows:
        lines.append(f"#{iid}: {name} — {price:.2f} USDT")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_cshop())

@dp.callback_query(F.data == "cadmin_add")
async def cadmin_add_item_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(CAdd.name)
    await cb.message.edit_text("➕ Введите название крипто-товара:", reply_markup=kb_back_main())

@dp.message(CAdd.name)
async def cadmin_add_item_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым.")
        return
    await state.update_data(name=name)
    await state.set_state(CAdd.price)
    await msg.answer("Цена в USDT (float >0):", reply_markup=kb_back_main())

@dp.message(CAdd.price)
async def cadmin_add_item_price(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        price = float(msg.text.strip())
        if price <= 0:
            raise ValueError
    except:
        await msg.answer("Цена - число >0.")
        return
    data = await state.get_data()
    db_exec("INSERT INTO crypto_items (name, price_usdt) VALUES (?, ?)", (data["name"], price))
    await state.clear()
    await msg.answer("✅ Крипто-товар добавлен.", reply_markup=kb_admin_cshop())

@dp.callback_query(F.data == "cadmin_edit")
async def cadmin_edit_item_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(CEdit.id)
    await cb.message.edit_text("✏ Введите ID крипто-товара:", reply_markup=kb_back_main())

@dp.message(CEdit.id)
async def cadmin_edit_item_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        iid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM crypto_items WHERE id=?", (iid,)):
        await msg.answer("Товар не найден.")
        return
    await state.update_data(id=iid)
    await state.set_state(CEdit.name)
    await msg.answer("Новое название (или . пропуск):", reply_markup=kb_back_main())

@dp.message(CEdit.name)
async def cadmin_edit_item_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    await state.update_data(name=None if name == '.' else name)
    await state.set_state(CEdit.price)
    await msg.answer("Новая цена (или . пропуск):", reply_markup=kb_back_main())

@dp.message(CEdit.price)
async def cadmin_edit_item_price(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    price = None
    if txt != '.':
        try:
            price = float(txt)
            if price <= 0:
                raise ValueError
        except:
            await msg.answer("Цена - число >0.")
            return
    data = await state.get_data()
    iid = data["id"]
    updates = []
    params = []
    if data["name"] is not None:
        updates.append("name=?")
        params.append(data["name"])
    if price is not None:
        updates.append("price_usdt=?")
        params.append(price)
    if not updates:
        await state.clear()
        await msg.answer("Ничего не изменено.", reply_markup=kb_admin_cshop())
        return
    q = "UPDATE crypto_items SET " + ", ".join(updates) + " WHERE id=?"
    params.append(iid)
    db_exec(q, tuple(params))
    await state.clear()
    await msg.answer("✅ Крипто-товар обновлён.", reply_markup=kb_admin_cshop())

@dp.callback_query(F.data == "cadmin_del")
async def cadmin_del_item_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(CDel.id)
    await cb.message.edit_text("🗑 Введите ID крипто-товара для удаления:", reply_markup=kb_back_main())

@dp.message(CDel.id)
async def cadmin_del_item_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        iid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    row = db_one("SELECT name FROM crypto_items WHERE id=?", (iid,))
    if not row:
        await msg.answer("Товар не найден.")
        return
    db_exec("DELETE FROM crypto_items WHERE id=?", (iid,))
    await state.clear()
    await msg.answer(f"🗑 Удалён крипто-товар '{row[0]}'.", reply_markup=kb_admin_cshop())

@dp.callback_query(F.data == "cadmin_orders")
async def cadmin_orders(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    rows = db_all("""
        SELECT id, user_id, invoice_id, item_name, amount_usdt, status, created_at
        FROM crypto_orders
        ORDER BY id DESC
        LIMIT 50
    """)
    text = "🧾 <b>Последние крипто-заказы</b>:\n" + format_crypto_orders_with_names(rows)
    await cb.message.edit_text(text, reply_markup=kb_admin_cshop())

# --- Админ: Выдать очки
@dp.callback_query(F.data == "admin_give_points")
async def admin_give_points_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(GivePoints.user_id)
    await cb.message.edit_text("➕ Введите user_id:", reply_markup=kb_back_main())

@dp.message(GivePoints.user_id)
async def admin_give_points_uid(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        uid = int(msg.text.strip())
    except:
        await msg.answer("User ID - число.")
        return
    if not db_one("SELECT 1 FROM users WHERE user_id=?", (uid,)):
        await msg.answer("Пользователь не найден.")
        return
    await state.update_data(user_id=uid)
    await state.set_state(GivePoints.amount)
    await msg.answer("Количество очков (float >0):", reply_markup=kb_back_main())

@dp.message(GivePoints.amount)
async def admin_give_points_amount(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        amt = float(msg.text.strip())
        if amt <= 0:
            raise ValueError
    except:
        await msg.answer("Количество - число >0.")
        return
    data = await state.get_data()
    uid = data["user_id"]
    db_exec("UPDATE users SET points = points + ? WHERE user_id=?", (amt, uid))
    await notify_user(uid, f"➕ Админ выдал вам {amt:.2f} очков!")
    await state.clear()
    await msg.answer("✅ Очки выданы.", reply_markup=kb_admin())

# --- Админ: Заказы
@dp.callback_query(F.data == "admin_orders")
async def admin_orders(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    orders = db_all("""
        SELECT id, user_id, item_name, price, status, created_at
        FROM orders
        ORDER BY id DESC
        LIMIT 50
    """)
    text = "📦 <b>Последние заказы</b>:\n" + format_orders_list_with_names(orders)
    await cb.message.edit_text(text, reply_markup=kb_admin())

@dp.callback_query(F.data.startswith("order:"))
async def order_status_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только админ.", show_alert=True)
        return
    parts = callback.data.split(":")
    order_id = int(parts[1])
    new_status = parts[2]
    db_exec("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
    row = db_one("SELECT user_id, item_name FROM orders WHERE id=?", (order_id,))
    if row:
        uid, item_name = row
        human = {"processing": "в обработке", "done": "выполнен", "canceled": "отменён"}.get(new_status, new_status)
        await notify_user(uid, f"🧾 Ваш заказ #{order_id} ({item_name}) теперь <i>{human}</i>.")
    await callback.answer(f"Статус изменён на {new_status}")
    await callback.message.edit_text(callback.message.text + f"\n\n✅ Статус изменён на {new_status}.")

# --- Админ: Пользователи
@dp.callback_query(F.data.startswith("admin_users:"))
async def admin_users(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    parts = cb.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    total = db_one("SELECT COUNT(*) FROM users")[0]
    total_pages = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * USERS_PAGE_SIZE
    rows = db_all("""
        SELECT user_id, username, full_name, points, created_at
        FROM users
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (USERS_PAGE_SIZE, offset))
    lines = [f"👥 <b>Пользователи (страница {page+1}/{total_pages}, всего {total})</b>:"]
    for uid, uname, fname, pts, ts in rows:
        disp = display_name(uid)
        t = time.strftime("%d.%m %H:%M", time.localtime(ts))
        lines.append(f"{disp} — {pts:.2f} оч. — {t}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_users_pagination(page, total_pages))

@dp.callback_query(F.data == "admin_export_users")
async def admin_export_users(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    rows = db_all("""
        SELECT user_id, username, full_name, points, created_at
        FROM users
        ORDER BY created_at DESC
    """)
    path = "users.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "username", "full_name", "points", "created_at"])
        w.writerows(rows)
    await cb.message.answer_document(FSInputFile(path))
    os.remove(path)

# --- Админ: Рассылка
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(Broadcast.content)
    await cb.message.edit_text("📢 Отправьте контент для рассылки (текст, фото, видео и т.д.):", reply_markup=kb_back_main())

@dp.message(Broadcast.content)
async def broadcast_content(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    content = msg.html_text or ""  # Используем msg.html_text для текста или caption
    if msg.photo or msg.video or msg.document or msg.audio or msg.voice:
        # Если есть медиа, сохраняем file_id
        if msg.photo:
            file_id = msg.photo[-1].file_id
            media_type = 'photo'
        elif msg.video:
            file_id = msg.video.file_id
            media_type = 'video'
        elif msg.document:
            file_id = msg.document.file_id
            media_type = 'document'
        elif msg.audio:
            file_id = msg.audio.file_id
            media_type = 'audio'
        elif msg.voice:
            file_id = msg.voice.file_id
            media_type = 'voice'
        await state.update_data(content=content, media_type=media_type, file_id=file_id)
    else:
        await state.update_data(content=content)
    await state.set_state(Broadcast.btn_choice)
    await msg.answer("Добавить кнопку? (да/нет):", reply_markup=kb_back_main())

@dp.message(Broadcast.btn_choice)
async def broadcast_btn_choice(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    choice = msg.text.strip().lower()
    if choice not in ('да', 'нет'):
        await msg.answer("Да или нет?")
        return
    if choice == 'нет':
        await state.update_data(btn_text=None, btn_url=None)
        await state.set_state(Broadcast.confirm)
        await msg.answer("Подтвердите рассылку (да/нет):", reply_markup=kb_back_main())
        return
    await state.set_state(Broadcast.btn_text)
    await msg.answer("Текст кнопки:", reply_markup=kb_back_main())

@dp.message(Broadcast.btn_text)
async def broadcast_btn_text(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    btn_text = msg.text.strip()
    if not btn_text:
        await msg.answer("Текст не может быть пустым.")
        return
    await state.update_data(btn_text=btn_text)
    await state.set_state(Broadcast.btn_url)
    await msg.answer("URL кнопки:", reply_markup=kb_back_main())

@dp.message(Broadcast.btn_url)
async def broadcast_btn_url(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    btn_url = msg.text.strip()
    if not btn_url.startswith("http"):
        await msg.answer("URL должен начинаться с http(s).")
        return
    await state.update_data(btn_url=btn_url)
    await state.set_state(Broadcast.confirm)
    await msg.answer("Подтвердите рассылку (да/нет):", reply_markup=kb_back_main())

@dp.message(Broadcast.confirm)
async def broadcast_confirm(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    choice = msg.text.strip().lower()
    if choice != 'да':
        await state.clear()
        await msg.answer("Рассылка отменена.", reply_markup=kb_admin())
        return
    data = await state.get_data()
    content = data["content"]
    btn_text = data.get("btn_text")
    btn_url = data.get("btn_url")
    media_type = data.get("media_type")
    file_id = data.get("file_id")
    kb = None
    if btn_text and btn_url:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=btn_url)]])
    users = [row[0] for row in db_all("SELECT user_id FROM users")]
    sent = 0
    last_ts = 0
    for uid in users:
        now = time.time()
        if now - last_ts < BROADCAST_COOLDOWN_SEC / len(users):  # Чтобы не спамить API
            await asyncio.sleep((BROADCAST_COOLDOWN_SEC / len(users)) - (now - last_ts))
        last_ts = time.time()
        try:
            if media_type:
                if media_type == 'photo':
                    await bot.send_photo(uid, file_id, caption=content, reply_markup=kb)
                elif media_type == 'video':
                    await bot.send_video(uid, file_id, caption=content, reply_markup=kb)
                elif media_type == 'document':
                    await bot.send_document(uid, file_id, caption=content, reply_markup=kb)
                elif media_type == 'audio':
                    await bot.send_audio(uid, file_id, caption=content, reply_markup=kb)
                elif media_type == 'voice':
                    await bot.send_voice(uid, file_id, caption=content, reply_markup=kb)
            else:
                await bot.send_message(uid, content, reply_markup=kb)
            sent += 1
        except Exception:
            pass
    await state.clear()
    await msg.answer(f"✅ Рассылка завершена. Отправлено {sent} пользователям.", reply_markup=kb_admin())

# --- Админ: Экспорт заказов
@dp.callback_query(F.data == "admin_export")
async def admin_export(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    rows = db_all("""
        SELECT id, user_id, item_name, price, status, created_at
        FROM orders
        ORDER BY created_at DESC
    """)
    path = "orders.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "user_id", "item_name", "price", "status", "created_at"])
        w.writerows(rows)
    await cb.message.answer_document(FSInputFile(path))
    os.remove(path)

# --- Админ: Каналы подписки
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    await cb.message.edit_text("📢 Управление каналами подписки", reply_markup=kb_admin_channels())

@dp.callback_query(F.data == "admin_list_channels")
async def admin_list_channels(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    channels = get_channels()
    if not channels:
        await cb.message.edit_text("📋 Каналов нет.", reply_markup=kb_admin_channels())
        return
    lines = ["📋 <b>Каналы</b>:"]
    for ch in channels:
        lines.append(f"• {ch}")
    lines.append(f"\nОбязательность: {'да' if is_require_sub() else 'нет'}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_channels())

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(AddChannel.channel)
    await cb.message.edit_text("➕ Введите @канал:", reply_markup=kb_back_main())

@dp.message(AddChannel.channel)
async def admin_add_channel_ch(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    ch = msg.text.strip()
    if not ch.startswith("@"):
        await msg.answer("@канал должен начинаться с @.")
        return
    db_exec("INSERT OR IGNORE INTO channels (channel) VALUES (?)", (ch,))
    await state.clear()
    await msg.answer("✅ Канал добавлен.", reply_markup=kb_admin_channels())

@dp.callback_query(F.data == "admin_edit_channel")
async def admin_edit_channel_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(EditChannel.old_channel)
    await cb.message.edit_text("✏ Введите старый @канал:", reply_markup=kb_back_main())

@dp.message(EditChannel.old_channel)
async def admin_edit_channel_old(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    old_ch = msg.text.strip()
    if not db_one("SELECT 1 FROM channels WHERE channel=?", (old_ch,)):
        await msg.answer("Канал не найден.")
        return
    await state.update_data(old_channel=old_ch)
    await state.set_state(EditChannel.new_channel)
    await msg.answer("Новый @канал:", reply_markup=kb_back_main())

@dp.message(EditChannel.new_channel)
async def admin_edit_channel_new(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    new_ch = msg.text.strip()
    if not new_ch.startswith("@"):
        await msg.answer("@канал должен начинаться с @.")
        return
    data = await state.get_data()
    db_exec("UPDATE channels SET channel=? WHERE channel=?", (new_ch, data["old_channel"]))
    await state.clear()
    await msg.answer("✅ Канал изменён.", reply_markup=kb_admin_channels())

@dp.callback_query(F.data == "admin_del_channel")
async def admin_del_channel_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(DelChannel.channel)
    await cb.message.edit_text("🗑 Введите @канал для удаления:", reply_markup=kb_back_main())

@dp.message(DelChannel.channel)
async def admin_del_channel_ch(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    ch = msg.text.strip()
    if not db_one("SELECT 1 FROM channels WHERE channel=?", (ch,)):
        await msg.answer("Канал не найден.")
        return
    db_exec("DELETE FROM channels WHERE channel=?", (ch,))
    await state.clear()
    await msg.answer("🗑 Канал удалён.", reply_markup=kb_admin_channels())

@dp.callback_query(F.data == "admin_toggle_sub")
async def admin_toggle_sub(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        return
    current = is_require_sub()
    new = '0' if current else '1'
    db_exec("INSERT OR REPLACE INTO meta (key, value) VALUES ('require_sub', ?)", (new,))
    await cb.answer(f"Обязательность подписки: {'отключена' if new == '0' else 'включена'}")
    await admin_list_channels(cb)

# --- Админ: Редкости
@dp.callback_query(F.data == "admin_rarities")
async def admin_rarities(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("🛡️ Управление редкостями", reply_markup=kb_admin_rarities())

@dp.callback_query(F.data == "admin_list_rarities")
async def admin_list_rarities(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    rows = db_all("SELECT id, name FROM rarities ORDER BY id")
    if not rows:
        await cb.message.edit_text("📋 Редкостей нет.", reply_markup=kb_admin_rarities())
        return
    lines = ["📋 <b>Редкости</b>:"]
    for rid, name in rows:
        lines.append(f"#{rid}: {name}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_rarities())

@dp.callback_query(F.data == "admin_add_rarity")
async def admin_add_rarity_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(AddRarity.name)
    await cb.message.edit_text("➕ Введите название редкости:", reply_markup=kb_back_main())

@dp.message(AddRarity.name)
async def admin_add_rarity_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым.")
        return
    db_exec("INSERT INTO rarities (name) VALUES (?)", (name,))
    await state.clear()
    await msg.answer("✅ Редкость добавлена.", reply_markup=kb_admin_rarities())

@dp.callback_query(F.data == "admin_edit_rarity")
async def admin_edit_rarity_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(EditRarity.id)
    await cb.message.edit_text("✏ Введите ID редкости:", reply_markup=kb_back_main())

@dp.message(EditRarity.id)
async def admin_edit_rarity_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        rid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM rarities WHERE id=?", (rid,)):
        await msg.answer("Редкость не найдена.")
        return
    await state.update_data(id=rid)
    await state.set_state(EditRarity.name)
    await msg.answer("Новое название:", reply_markup=kb_back_main())

@dp.message(EditRarity.name)
async def admin_edit_rarity_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым.")
        return
    data = await state.get_data()
    db_exec("UPDATE rarities SET name=? WHERE id=?", (name, data["id"]))
    await state.clear()
    await msg.answer("✅ Редкость обновлена.", reply_markup=kb_admin_rarities())

@dp.callback_query(F.data == "admin_del_rarity")
async def admin_del_rarity_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(DelRarity.id)
    await cb.message.edit_text("🗑 Введите ID редкости для удаления:", reply_markup=kb_back_main())

@dp.message(DelRarity.id)
async def admin_del_rarity_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        rid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    row = db_one("SELECT name FROM rarities WHERE id=?", (rid,))
    if not row:
        await msg.answer("Редкость не найдена.")
        return
    db_exec("DELETE FROM rarities WHERE id=?", (rid,))
    await state.clear()
    await msg.answer(f"🗑 Удалена редкость '{row[0]}'.", reply_markup=kb_admin_rarities())

# --- Админ: Яйца
@dp.callback_query(F.data == "admin_eggs")
async def admin_eggs(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("🥚 Управление яйцами", reply_markup=kb_admin_eggs())

@dp.callback_query(F.data == "admin_list_eggs")
async def admin_list_eggs(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    rows = db_all("SELECT id, name, price, hatch_time_sec FROM egg_types ORDER BY id")
    if not rows:
        await cb.message.edit_text("📋 Яиц нет.", reply_markup=kb_admin_eggs())
        return
    lines = ["📋 <b>Яйца</b>:"]
    for eid, name, price, hts in rows:
        lines.append(f"#{eid}: {name} — {price} оч., вылупление {hts} сек")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_eggs())

@dp.callback_query(F.data == "admin_add_egg")
async def admin_add_egg_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(AddEgg.name)
    await cb.message.edit_text("➕ Введите название яйца:", reply_markup=kb_back_main())

@dp.message(AddEgg.name)
async def admin_add_egg_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым.")
        return
    await state.update_data(name=name)
    await state.set_state(AddEgg.price)
    await msg.answer("Цена (int >0):", reply_markup=kb_back_main())

@dp.message(AddEgg.price)
async def admin_add_egg_price(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        price = int(msg.text.strip())
        if price <= 0:
            raise ValueError
    except:
        await msg.answer("Цена - целое >0.")
        return
    await state.update_data(price=price)
    await state.set_state(AddEgg.hatch_time_sec)
    await msg.answer("Время вылупления в секундах (int >=0):", reply_markup=kb_back_main())

@dp.message(AddEgg.hatch_time_sec)
async def admin_add_egg_hts(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        hts = int(msg.text.strip())
        if hts < 0:
            raise ValueError
    except:
        await msg.answer("Время - целое >=0.")
        return
    data = await state.get_data()
    db_exec("INSERT INTO egg_types (name, price, hatch_time_sec) VALUES (?, ?, ?)", (data["name"], data["price"], hts))
    await state.clear()
    await msg.answer("✅ Яйцо добавлено.", reply_markup=kb_admin_eggs())

@dp.callback_query(F.data == "admin_edit_egg")
async def admin_edit_egg_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(EditEgg.id)
    await cb.message.edit_text("✏ Введите ID яйца:", reply_markup=kb_back_main())

@dp.message(EditEgg.id)
async def admin_edit_egg_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        eid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM egg_types WHERE id=?", (eid,)):
        await msg.answer("Яйцо не найдено.")
        return
    await state.update_data(id=eid)
    await state.set_state(EditEgg.name)
    await msg.answer("Новое название (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditEgg.name)
async def admin_edit_egg_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    await state.update_data(name=None if name == '.' else name)
    await state.set_state(EditEgg.price)
    await msg.answer("Новая цена (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditEgg.price)
async def admin_edit_egg_price(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    price = None
    if txt != '.':
        try:
            price = int(txt)
            if price <= 0:
                raise ValueError
        except:
            await msg.answer("Цена - целое >0.")
            return
    await state.update_data(price=price)
    await state.set_state(EditEgg.hatch_time_sec)
    await msg.answer("Новое время вылупления (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditEgg.hatch_time_sec)
async def admin_edit_egg_hts(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    hts = None
    if txt != '.':
        try:
            hts = int(txt)
            if hts < 0:
                raise ValueError
        except:
            await msg.answer("Время - целое >=0.")
            return
    data = await state.get_data()
    eid = data["id"]
    updates = []
    params = []
    if data["name"] is not None:
        updates.append("name=?")
        params.append(data["name"])
    if data["price"] is not None:
        updates.append("price=?")
        params.append(data["price"])
    if hts is not None:
        updates.append("hatch_time_sec=?")
        params.append(hts)
    if not updates:
        await state.clear()
        await msg.answer("Ничего не изменено.", reply_markup=kb_admin_eggs())
        return
    q = "UPDATE egg_types SET " + ", ".join(updates) + " WHERE id=?"
    params.append(eid)
    db_exec(q, tuple(params))
    await state.clear()
    await msg.answer("✅ Яйцо обновлено.", reply_markup=kb_admin_eggs())

@dp.callback_query(F.data == "admin_del_egg")
async def admin_del_egg_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(DelEgg.id)
    await cb.message.edit_text("🗑 Введите ID яйца для удаления:", reply_markup=kb_back_main())

@dp.message(DelEgg.id)
async def admin_del_egg_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        eid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    row = db_one("SELECT name FROM egg_types WHERE id=?", (eid,))
    if not row:
        await msg.answer("Яйцо не найдено.")
        return
    db_exec("DELETE FROM egg_types WHERE id=?", (eid,))
    db_exec("DELETE FROM egg_chances WHERE egg_id=? OR random_egg_id=?", (eid, eid))
    db_exec("DELETE FROM pet_egg_chances WHERE egg_id=?", (eid,))
    await state.clear()
    await msg.answer(f"🗑 Удалено яйцо '{row[0]}'.", reply_markup=kb_admin_eggs())

@dp.callback_query(F.data == "admin_random_egg")
async def admin_random_egg(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("✏ Настройка Random Egg", reply_markup=kb_admin_random_egg())

@dp.callback_query(F.data == "admin_list_egg_chances")
async def admin_list_egg_chances(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    random_egg_id = db_one("SELECT id FROM egg_types WHERE name='Random Egg'")[0]
    rows = db_all("""
        SELECT et.name, ec.chance
        FROM egg_chances ec
        JOIN egg_types et ON ec.egg_id = et.id
        WHERE ec.random_egg_id = ?
    """, (random_egg_id,))
    if not rows:
        await cb.message.edit_text("📋 Шансов нет.", reply_markup=kb_admin_random_egg())
        return
    lines = ["📋 <b>Шансы для Random Egg</b>:"]
    for name, chance in rows:
        lines.append(f"{name}: {chance:.2f}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_random_egg())

@dp.callback_query(F.data == "admin_add_egg_chance")
async def admin_add_egg_chance_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(AddEggChance.egg_id)
    await cb.message.edit_text("➕ Введите ID яйца:", reply_markup=kb_back_main())

@dp.message(AddEggChance.egg_id)
async def admin_add_egg_chance_eid(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        eid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM egg_types WHERE id=?", (eid,)):
        await msg.answer("Яйцо не найдено.")
        return
    await state.update_data(egg_id=eid)
    await state.set_state(AddEggChance.chance)
    await msg.answer("Шанс (float >0):", reply_markup=kb_back_main())

@dp.message(AddEggChance.chance)
async def admin_add_egg_chance_chance(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        chance = float(msg.text.strip())
        if chance <= 0:
            raise ValueError
    except:
        await msg.answer("Шанс - число >0.")
        return
    data = await state.get_data()
    random_egg_id = db_one("SELECT id FROM egg_types WHERE name='Random Egg'")[0]
    db_exec("INSERT OR REPLACE INTO egg_chances (random_egg_id, egg_id, chance) VALUES (?, ?, ?)", (random_egg_id, data["egg_id"], chance))
    await state.clear()
    await msg.answer("✅ Шанс добавлен/обновлён.", reply_markup=kb_admin_random_egg())

@dp.callback_query(F.data == "admin_edit_egg_chance")
async def admin_edit_egg_chance_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(EditEggChance.egg_id)
    await cb.message.edit_text("✏ Введите ID яйца:", reply_markup=kb_back_main())

@dp.message(EditEggChance.egg_id)
async def admin_edit_egg_chance_eid(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        eid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    random_egg_id = db_one("SELECT id FROM egg_types WHERE name='Random Egg'")[0]
    if not db_one("SELECT 1 FROM egg_chances WHERE random_egg_id=? AND egg_id=?", (random_egg_id, eid)):
        await msg.answer("Шанс для этого яйца не найден.")
        return
    await state.update_data(egg_id=eid)
    await state.set_state(EditEggChance.chance)
    await msg.answer("Новый шанс (float >0):", reply_markup=kb_back_main())

@dp.message(EditEggChance.chance)
async def admin_edit_egg_chance_chance(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        chance = float(msg.text.strip())
        if chance <= 0:
            raise ValueError
    except:
        await msg.answer("Шанс - число >0.")
        return
    data = await state.get_data()
    random_egg_id = db_one("SELECT id FROM egg_types WHERE name='Random Egg'")[0]
    db_exec("UPDATE egg_chances SET chance=? WHERE random_egg_id=? AND egg_id=?", (chance, random_egg_id, data["egg_id"]))
    await state.clear()
    await msg.answer("✅ Шанс обновлён.", reply_markup=kb_admin_random_egg())

@dp.callback_query(F.data == "admin_del_egg_chance")
async def admin_del_egg_chance_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(DelEggChance.egg_id)
    await cb.message.edit_text("🗑 Введите ID яйца для удаления шанса:", reply_markup=kb_back_main())

@dp.message(DelEggChance.egg_id)
async def admin_del_egg_chance_eid(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        eid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    random_egg_id = db_one("SELECT id FROM egg_types WHERE name='Random Egg'")[0]
    if not db_one("SELECT 1 FROM egg_chances WHERE random_egg_id=? AND egg_id=?", (random_egg_id, eid)):
        await msg.answer("Шанс не найден.")
        return
    db_exec("DELETE FROM egg_chances WHERE random_egg_id=? AND egg_id=?", (random_egg_id, eid))
    await state.clear()
    await msg.answer("🗑 Шанс удалён.", reply_markup=kb_admin_random_egg())

# --- Админ: Питомцы конфиг
@dp.callback_query(F.data == "admin_pets_config")
async def admin_pets_config(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("🐶 Управление конфигом питомцев", reply_markup=kb_admin_pets_config())

@dp.callback_query(F.data == "admin_list_pets_config")
async def admin_list_pets_config(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    rows = db_all("""
        SELECT p.id, p.name, r.name, p.daily_points, p.photo, p.chance
        FROM pets_config p
        JOIN rarities r ON p.rarity_id = r.id
        ORDER BY p.id
    """)
    if not rows:
        await cb.message.edit_text("📋 Питомцев нет.", reply_markup=kb_admin_pets_config())
        return
    lines = ["📋 <b>Питомцы</b>:"]
    for pid, name, rarity, dp, photo, chance in rows:
        ph = f" (photo: {photo})" if photo else ""
        lines.append(f"#{pid}: {name} ({rarity}) — {dp:.2f} оч./день, шанс {chance:.2f}{ph}")
    # Добавим шансы по яйцам
    for pid, _, _, _, _, _ in rows:
        egg_ch = db_all("""
            SELECT et.name, pec.chance
            FROM pet_egg_chances pec
            JOIN egg_types et ON pec.egg_id = et.id
            WHERE pec.pet_id = ?
        """, (pid,))
        if egg_ch:
            ech_lines = [f"  - {ename}: {ech:.2f}" for ename, ech in egg_ch]
            lines.append("\n".join(ech_lines))
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_pets_config())

@dp.callback_query(F.data == "admin_add_pet_config")
async def admin_add_pet_config_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(AddPetConfig.name)
    await cb.message.edit_text("➕ Введите название питомца:", reply_markup=kb_back_main())

@dp.message(AddPetConfig.name)
async def admin_add_pet_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым.")
        return
    await state.update_data(name=name)
    await state.set_state(AddPetConfig.rarity_id)
    await msg.answer("ID редкости:", reply_markup=kb_back_main())

@dp.message(AddPetConfig.rarity_id)
async def admin_add_pet_rarity(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        rid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM rarities WHERE id=?", (rid,)):
        await msg.answer("Редкость не найдена.")
        return
    await state.update_data(rarity_id=rid)
    await state.set_state(AddPetConfig.daily_points)
    await msg.answer("Daily points (float >0):", reply_markup=kb_back_main())

@dp.message(AddPetConfig.daily_points)
async def admin_add_pet_dp(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        dp = float(msg.text.strip())
        if dp <= 0:
            raise ValueError
    except:
        await msg.answer("Daily points - число >0.")
        return
    await state.update_data(daily_points=dp)
    await state.set_state(AddPetConfig.photo)
    await msg.answer("Фото (отправьте фото, введите URL (http/https) или . пропуск):", reply_markup=kb_back_main())

@dp.message(AddPetConfig.photo)
async def admin_add_pet_photo(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    if msg.photo:
        photo = msg.photo[-1].file_id
    elif msg.text and (msg.text.strip().startswith("http://") or msg.text.strip().startswith("https://")):
        photo = msg.text.strip()
    elif msg.text.strip() == '.':
        photo = None
    else:
        await msg.answer("Пожалуйста, загрузите фото, введите URL (http/https) или . для пропуска.")
        return
    await state.update_data(photo=photo)
    await state.set_state(AddPetConfig.chance)
    await msg.answer("Глобальный шанс (float >=0, 1.0 по умолчанию, или . пропуск):", reply_markup=kb_back_main())

@dp.message(AddPetConfig.chance)
async def admin_add_pet_chance(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    chance = 1.0
    if txt != '.':
        try:
            chance = float(txt)
            if chance < 0:
                raise ValueError
        except:
            await msg.answer("Шанс - число >=0.")
            return
    await state.update_data(chance=chance)
    await state.set_state(AddPetConfig.egg_chances)
    await msg.answer("Шансы по яйцам (egg_id1:chance1,egg_id2:chance2,... или . пропуск):", reply_markup=kb_back_main())

@dp.message(AddPetConfig.egg_chances)
async def admin_add_pet_egg_chances(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    data = await state.get_data()
    db_exec("INSERT INTO pets_config (name, rarity_id, daily_points, photo, chance) VALUES (?, ?, ?, ?, ?)",
            (data["name"], data["rarity_id"], data["daily_points"], data["photo"], data["chance"]))
    pid = cur.lastrowid
    txt = msg.text.strip()
    if txt != '.':
        pairs = txt.split(',')
        for pair in pairs:
            try:
                egg_id, ch = pair.split(':')
                egg_id = int(egg_id.strip())
                ch = float(ch.strip())
                if not db_one("SELECT 1 FROM egg_types WHERE id=?", (egg_id,)):
                    await msg.answer(f"Яйцо #{egg_id} не найдено. Пропускаю.")
                    continue
                db_exec("INSERT INTO pet_egg_chances (pet_id, egg_id, chance) VALUES (?, ?, ?)", (pid, egg_id, ch))
            except:
                await msg.answer(f"Некорректный формат для {pair}. Пропускаю.")
    await state.clear()
    await msg.answer("✅ Питомец добавлен.", reply_markup=kb_admin_pets_config())

@dp.callback_query(F.data == "admin_edit_pet_config")
async def admin_edit_pet_config_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(EditPetConfig.id)
    await cb.message.edit_text("✏ Введите ID питомца:", reply_markup=kb_back_main())

@dp.message(EditPetConfig.id)
async def admin_edit_pet_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        pid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM pets_config WHERE id=?", (pid,)):
        await msg.answer("Питомец не найден.")
        return
    await state.update_data(id=pid)
    await state.set_state(EditPetConfig.name)
    await msg.answer("Новое название (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditPetConfig.name)
async def admin_edit_pet_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    await state.update_data(name=None if name == '.' else name)
    await state.set_state(EditPetConfig.rarity_id)
    await msg.answer("Новый ID редкости (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditPetConfig.rarity_id)
async def admin_edit_pet_rarity(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    rid = None
    if txt != '.':
        try:
            rid = int(txt)
            if not db_one("SELECT 1 FROM rarities WHERE id=?", (rid,)):
                await msg.answer("Редкость не найдена.")
                return
        except:
            await msg.answer("ID - число.")
            return
    await state.update_data(rarity_id=rid)
    await state.set_state(EditPetConfig.daily_points)
    await msg.answer("Новые daily points (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditPetConfig.daily_points)
async def admin_edit_pet_dp(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    dp = None
    if txt != '.':
        try:
            dp = float(txt)
            if dp <= 0:
                raise ValueError
        except:
            await msg.answer("Daily points - число >0.")
            return
    await state.update_data(daily_points=dp)
    await state.set_state(EditPetConfig.photo)
    await msg.answer("Новое фото (отправьте фото, введите URL, . для пропуска или none для очистки):", reply_markup=kb_back_main())

@dp.message(EditPetConfig.photo)
async def admin_edit_pet_photo(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip() if msg.text else None
    if msg.photo:
        photo = msg.photo[-1].file_id
    elif txt and (txt.startswith("http://") or txt.startswith("https://")):
        photo = txt
    elif txt == '.':
        photo = "skip"  # Do not change existing
    elif txt == 'none':
        photo = None  # Clear to NULL
    else:
        await msg.answer("Пожалуйста, загрузите фото, введите URL, . для пропуска или none для очистки.")
        return
    if photo != "skip":
        await state.update_data(photo=photo)
    await state.set_state(EditPetConfig.chance)
    await msg.answer("Новый глобальный шанс (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditPetConfig.chance)
async def admin_edit_pet_chance(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    chance = None
    if txt != '.':
        try:
            chance = float(txt)
            if chance < 0:
                raise ValueError
        except:
            await msg.answer("Шанс - число >=0.")
            return
    await state.update_data(chance=chance)
    await state.set_state(EditPetConfig.egg_chances)
    await msg.answer("Новые шансы по яйцам (egg_id1:chance1,... или . пропуск, пусто для очистки):", reply_markup=kb_back_main())

@dp.message(EditPetConfig.egg_chances)
async def admin_edit_pet_egg_chances(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    data = await state.get_data()
    pid = data["id"]
    updates = []
    params = []
    if data["name"] is not None:
        updates.append("name=?")
        params.append(data["name"])
    if data["rarity_id"] is not None:
        updates.append("rarity_id=?")
        params.append(data["rarity_id"])
    if data["daily_points"] is not None:
        updates.append("daily_points=?")
        params.append(data["daily_points"])
    if "photo" in data and data["photo"] is not None:
        updates.append("photo=?")
        params.append(data["photo"])
    elif "photo" in data and data["photo"] is None:
        updates.append("photo=NULL")
    if data["chance"] is not None:
        updates.append("chance=?")
        params.append(data["chance"])
    if updates:
        q = "UPDATE pets_config SET " + ", ".join(updates) + " WHERE id=?"
        params.append(pid)
        db_exec(q, tuple(params))
    txt = msg.text.strip()
    if txt != '.':
        db_exec("DELETE FROM pet_egg_chances WHERE pet_id=?", (pid,))
        if txt:
            pairs = txt.split(',')
            for pair in pairs:
                try:
                    egg_id, ch = pair.split(':')
                    egg_id = int(egg_id.strip())
                    ch = float(ch.strip())
                    if not db_one("SELECT 1 FROM egg_types WHERE id=?", (egg_id,)):
                        await msg.answer(f"Яйцо #{egg_id} не найдено. Пропускаю.")
                        continue
                    db_exec("INSERT INTO pet_egg_chances (pet_id, egg_id, chance) VALUES (?, ?, ?)", (pid, egg_id, ch))
                except:
                    await msg.answer(f"Некорректный формат для {pair}. Пропускаю.")
    await state.clear()
    await msg.answer("✅ Питомец обновлён.", reply_markup=kb_admin_pets_config())

@dp.callback_query(F.data == "admin_del_pet_config")
async def admin_del_pet_config_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(DelPetConfig.id)
    await cb.message.edit_text("🗑 Введите ID питомца для удаления:", reply_markup=kb_back_main())

@dp.message(DelPetConfig.id)
async def admin_del_pet_config_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        pid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    row = db_one("SELECT name FROM pets_config WHERE id=?", (pid,))
    if not row:
        await msg.answer("Питомец не найден.")
        return
    db_exec("DELETE FROM pets_config WHERE id=?", (pid,))
    db_exec("DELETE FROM pet_evolutions WHERE pet_id=?", (pid,))
    db_exec("DELETE FROM pet_egg_chances WHERE pet_id=?", (pid,))
    await state.clear()
    await msg.answer(f"🗑 Удалён питомец '{row[0]}'.", reply_markup=kb_admin_pets_config())

# --- Админ: Эволюции
@dp.callback_query(F.data == "admin_evolutions")
async def admin_evolutions(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("🔄 Управление эволюциями", reply_markup=kb_admin_evolutions())

@dp.callback_query(F.data == "admin_list_evolutions")
async def admin_list_evolutions(cb: types.CallbackQuery):
    if not ensure_admin(cb):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    rows = db_all("""
        SELECT pe.id, p.name, pe.required_days, pe.name, pe.daily_points, pe.photo
        FROM pet_evolutions pe
        JOIN pets_config p ON pe.pet_id = p.id
        ORDER BY pe.pet_id, pe.required_days
    """)
    if not rows:
        await cb.message.edit_text("📋 Эволюций нет.", reply_markup=kb_admin_evolutions())
        return
    lines = ["📋 <b>Эволюции</b>:"]
    for eid, pet_name, days, name, dp, photo in rows:
        ph = f" (photo: {photo})" if photo else ""
        lines.append(f"#{eid}: {pet_name} ({days} дней) → {name} — {dp:.2f} оч./день{ph}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_evolutions())

@dp.callback_query(F.data == "admin_add_evolution")
async def admin_add_evolution_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(AddEvolution.pet_id)
    await cb.message.edit_text("➕ Введите ID питомца:", reply_markup=kb_back_main())

@dp.message(AddEvolution.pet_id)
async def admin_add_evo_pet_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        pid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM pets_config WHERE id=?", (pid,)):
        await msg.answer("Питомец не найден.")
        return
    await state.update_data(pet_id=pid)
    await state.set_state(AddEvolution.required_days)
    await msg.answer("Требуемые дни (int >0):", reply_markup=kb_back_main())

@dp.message(AddEvolution.required_days)
async def admin_add_evo_days(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        days = int(msg.text.strip())
        if days <= 0:
            raise ValueError
    except:
        await msg.answer("Дни - целое >0.")
        return
    await state.update_data(required_days=days)
    await state.set_state(AddEvolution.name)
    await msg.answer("Название эволюции:", reply_markup=kb_back_main())

@dp.message(AddEvolution.name)
async def admin_add_evo_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым.")
        return
    await state.update_data(name=name)
    await state.set_state(AddEvolution.photo)
    await msg.answer("Фото (отправьте фото, введите URL (http/https) или . пропуск):", reply_markup=kb_back_main())

@dp.message(AddEvolution.photo)
async def admin_add_evo_photo(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    if msg.photo:
        photo = msg.photo[-1].file_id
    elif msg.text and (msg.text.strip().startswith("http://") or msg.text.strip().startswith("https://")):
        photo = msg.text.strip()
    elif msg.text.strip() == '.':
        photo = None
    else:
        await msg.answer("Пожалуйста, загрузите фото, введите URL (http/https) или . для пропуска.")
        return
    await state.update_data(photo=photo)
    await state.set_state(AddEvolution.daily_points)
    await msg.answer("Daily points эволюции (float >0):", reply_markup=kb_back_main())

@dp.message(AddEvolution.daily_points)
async def admin_add_evo_dp(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        dp = float(msg.text.strip())
        if dp <= 0:
            raise ValueError
    except:
        await msg.answer("Daily points - число >0.")
        return
    data = await state.get_data()
    db_exec("INSERT INTO pet_evolutions (pet_id, required_days, name, photo, daily_points) VALUES (?, ?, ?, ?, ?)",
            (data["pet_id"], data["required_days"], data["name"], data["photo"], dp))
    await state.clear()
    await msg.answer("✅ Эволюция добавлена.", reply_markup=kb_admin_evolutions())

@dp.callback_query(F.data == "admin_edit_evolution")
async def admin_edit_evolution_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(EditEvolution.id)
    await cb.message.edit_text("✏ Введите ID эволюции:", reply_markup=kb_back_main())

@dp.message(EditEvolution.id)
async def admin_edit_evo_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        eid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM pet_evolutions WHERE id=?", (eid,)):
        await msg.answer("Эволюция не найдена.")
        return
    await state.update_data(id=eid)
    await state.set_state(EditEvolution.required_days)
    await msg.answer("Новые дни (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditEvolution.required_days)
async def admin_edit_evo_days(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    days = None
    if txt != '.':
        try:
            days = int(txt)
            if days <= 0:
                raise ValueError
        except:
            await msg.answer("Дни - целое >0.")
            return
    await state.update_data(required_days=days)
    await state.set_state(EditEvolution.name)
    await msg.answer("Новое название (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditEvolution.name)
async def admin_edit_evo_name(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    name = msg.text.strip()
    await state.update_data(name=None if name == '.' else name)
    await state.set_state(EditEvolution.photo)
    await msg.answer("Новое фото (отправьте фото, введите URL, . для пропуска или none для очистки):", reply_markup=kb_back_main())

@dp.message(EditEvolution.photo)
async def admin_edit_evo_photo(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip() if msg.text else None
    if msg.photo:
        photo = msg.photo[-1].file_id
    elif txt and (txt.startswith("http://") or txt.startswith("https://")):
        photo = txt
    elif txt == '.':
        photo = "skip"
    elif txt == 'none':
        photo = None
    else:
        await msg.answer("Пожалуйста, загрузите фото, введите URL, . для пропуска или none для очистки.")
        return
    if photo != "skip":
        await state.update_data(photo=photo)
    await state.set_state(EditEvolution.daily_points)
    await msg.answer("Новые daily points (или . пропуск):", reply_markup=kb_back_main())

@dp.message(EditEvolution.daily_points)
async def admin_edit_evo_dp(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    txt = msg.text.strip()
    dp = None
    if txt != '.':
        try:
            dp = float(txt)
            if dp <= 0:
                raise ValueError
        except:
            await msg.answer("Daily points - число >0.")
            return
    data = await state.get_data()
    eid = data["id"]
    updates = []
    params = []
    if data["required_days"] is not None:
        updates.append("required_days=?")
        params.append(data["required_days"])
    if data["name"] is not None:
        updates.append("name=?")
        params.append(data["name"])
    if "photo" in data and data["photo"] is not None:
        updates.append("photo=?")
        params.append(data["photo"])
    elif "photo" in data and data["photo"] is None:
        updates.append("photo=NULL")
    if dp is not None:
        updates.append("daily_points=?")
        params.append(dp)
    if not updates:
        await state.clear()
        await msg.answer("Ничего не изменено.", reply_markup=kb_admin_evolutions())
        return
    q = "UPDATE pet_evolutions SET " + ", ".join(updates) + " WHERE id=?"
    params.append(eid)
    db_exec(q, tuple(params))
    await state.clear()
    await msg.answer("✅ Эволюция обновлена.", reply_markup=kb_admin_evolutions())

@dp.callback_query(F.data == "admin_del_evolution")
async def admin_del_evolution_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(DelEvolution.id)
    await cb.message.edit_text("🗑 Введите ID эволюции для удаления:", reply_markup=kb_back_main())

@dp.message(DelEvolution.id)
async def admin_del_evolution_id(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        eid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    row = db_one("SELECT name FROM pet_evolutions WHERE id=?", (eid,))
    if not row:
        await msg.answer("Эволюция не найдена.")
        return
    db_exec("DELETE FROM pet_evolutions WHERE id=?", (eid,))
    await state.clear()
    await msg.answer(f"🗑 Удалена эволюция '{row[0]}'.", reply_markup=kb_admin_evolutions())

# --- Админ: Выдать питомца
@dp.callback_query(F.data == "admin_give_pet")
async def admin_give_pet_start(cb: types.CallbackQuery, state: FSMContext):
    if not ensure_admin(cb): return
    await state.set_state(GivePet.user_id)
    await cb.message.edit_text("🎁 Введите user_id:", reply_markup=kb_back_main())

@dp.message(GivePet.user_id)
async def admin_give_pet_uid(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        uid = int(msg.text.strip())
    except:
        await msg.answer("User ID - число.")
        return
    if not db_one("SELECT 1 FROM users WHERE user_id=?", (uid,)):
        await msg.answer("Пользователь не найден.")
        return
    await state.update_data(user_id=uid)
    await state.set_state(GivePet.pet_id)
    await msg.answer("ID питомца из pets_config:", reply_markup=kb_back_main())

@dp.message(GivePet.pet_id)
async def admin_give_pet_pid(msg: types.Message, state: FSMContext):
    if not ensure_admin(msg): return
    try:
        pid = int(msg.text.strip())
    except:
        await msg.answer("ID - число.")
        return
    if not db_one("SELECT 1 FROM pets_config WHERE id=?", (pid,)):
        await msg.answer("Питомец не найден в конфиге.")
        return
    data = await state.get_data()
    uid = data["user_id"]
    db_exec("INSERT INTO user_pets (user_id, pet_id, egg_id) VALUES (?, ?, 0)", (uid, pid))
    pet_name = db_one("SELECT name FROM pets_config WHERE id=?", (pid,))[0]
    await notify_user(uid, f"🎁 Админ выдал вам питомца '{pet_name}'!")
    await state.clear()
    await msg.answer("✅ Питомец выдан.", reply_markup=kb_admin_pets())

async def auto_pet_management_task():
    while True:
        await asyncio.sleep(86400)  # Ежедневно
        now_day = int(time.time() // 86400)
        pets = db_all("SELECT id, user_id, last_feed_day, lives FROM user_pets WHERE lives > 0")
        for up_id, uid, last_day, lives in pets:
            if last_day < now_day - 1:  # Не кормили вчера
                new_lives = max(0, lives - 1)
                db_exec("UPDATE user_pets SET lives=?, feed_streak=0 WHERE id=?", (new_lives, up_id))
                if new_lives == 0:
                    await notify_user(uid, "❗ Один из ваших питомцев умер от голода!")

# Обеспечение админ-доступа
def ensure_admin(obj: types.CallbackQuery | types.Message) -> bool:
    if isinstance(obj, types.CallbackQuery):
        obj = obj.message
    if obj.from_user.id not in ADMIN_IDS:
        obj.answer("⛔ Нет доступа", show_alert=True)
        return False
    return True

async def main():
    asyncio.create_task(auto_check_crypto_orders())
    asyncio.create_task(auto_pet_management_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
