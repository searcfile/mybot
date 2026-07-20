import os
import threading
import asyncio
import time
import psycopg2
import cloudinary
import cloudinary.uploader
import re
import secrets
import string
import json
from zoneinfo import ZoneInfo

from urllib.request import (
    urlopen,
    Request
)

from urllib.parse import urlencode

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    jsonify
)

from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
 
# =========================
# CLOUDINARY CONFIG
# =========================
cloudinary.config(
    cloud_name=os.getenv("CLOUD_NAME"),
    api_key=os.getenv("CLOUD_API_KEY"),
    api_secret=os.getenv("CLOUD_API_SECRET"),
    secure=True
)

# =========================
# CONST
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "secret123")

# =========================
# CLOUDFLARE TURNSTILE
# =========================
TURNSTILE_SITE_KEY = os.getenv(
    "TURNSTILE_SITE_KEY",
    ""
).strip()

TURNSTILE_SECRET_KEY = os.getenv(
    "TURNSTILE_SECRET_KEY",
    ""
).strip()

TURNSTILE_ENABLED = bool(
    TURNSTILE_SITE_KEY
    and TURNSTILE_SECRET_KEY
)

# =========================
# FLASK APP
# =========================
flask_app = Flask(__name__)
flask_app.secret_key = SECRET_KEY

# =========================
# ADMIN LOGIN SESSION
# =========================

# Jangan panjangkan masa login apabila admin refresh halaman
flask_app.config[
    "SESSION_REFRESH_EACH_REQUEST"
] = False

# Lindungi cookie login
flask_app.config[
    "SESSION_COOKIE_HTTPONLY"
] = True

flask_app.config[
    "SESSION_COOKIE_SAMESITE"
] = "Lax"

# Railway menggunakan HTTPS
flask_app.config[
    "SESSION_COOKIE_SECURE"
] = True

# =========================
# TEXT FORMATTER
# =========================
def convert_markdown_bold_to_html(text: str):
    """
    Convert **bold** to <b>bold</b> for Telegram HTML parse_mode.
    """
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

 
# =========================
# WEBHOOK CLEAN
# =========================
def clear_webhook():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing, cannot clear webhook")
        return

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
        resp = urlopen(url, timeout=10)
        print("Webhook cleared:", resp.read().decode("utf-8"))
    except Exception as e:
        print("Webhook clear failed:", e)


# =========================
# CLOUDINARY UPLOAD
# =========================
def upload_to_cloudinary(file):
    result = cloudinary.uploader.upload(
        file,
        folder="telegram_bot",
        resource_type="image"
    )
    return result["secure_url"]


# =========================
# DB
# =========================
def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not found. Please set Railway PostgreSQL DATABASE_URL in Variables.")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")

    cur = conn.cursor()
    cur.execute("SET TIME ZONE 'Asia/Kuala_Lumpur';")
    cur.close()

    return conn


# =========================
# REFERRAL HELPERS
# =========================
def generate_referral_code(length=8):
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def get_user_referral_info(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT referral_code, referral_count
        FROM users
        WHERE telegram_id=%s
    """, (user_id,))
    row = cur.fetchone()

    cur.close()
    conn.close()

    if row:
        return row[0], row[1]
    return None, 0


def ensure_referral_code(user_id: int):
    """
    Ensure this user has referral_code generated.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT referral_code FROM users WHERE telegram_id=%s", (user_id,))
    row = cur.fetchone()

    if row and row[0]:
        cur.close()
        conn.close()
        return row[0]

    # generate unique code
    while True:
        code = generate_referral_code()
        cur.execute("SELECT telegram_id FROM users WHERE referral_code=%s", (code,))
        exists = cur.fetchone()
        if not exists:
            break

    cur.execute("UPDATE users SET referral_code=%s WHERE telegram_id=%s", (code, user_id))
    conn.commit()

    cur.close()
    conn.close()
    return code


def bind_referral(new_user_id: int, ref_code: str):
    """
    Bind referred_by if not already set.
    Increase referral_count for referrer.
    """
    if not ref_code:
        return False

    conn = get_db_connection()
    cur = conn.cursor()

    # get referrer by code
    cur.execute("SELECT telegram_id FROM users WHERE referral_code=%s", (ref_code,))
    ref_row = cur.fetchone()

    if not ref_row:
        cur.close()
        conn.close()
        return False

    referrer_id = ref_row[0]

    # cannot refer self
    if referrer_id == new_user_id:
        cur.close()
        conn.close()
        return False

    # check if already bound
    cur.execute("SELECT referred_by FROM users WHERE telegram_id=%s", (new_user_id,))
    existing = cur.fetchone()

    if existing and existing[0]:
        cur.close()
        conn.close()
        return False

    # bind
    cur.execute("UPDATE users SET referred_by=%s WHERE telegram_id=%s", (referrer_id, new_user_id))
    cur.execute("UPDATE users SET referral_count = referral_count + 1 WHERE telegram_id=%s", (referrer_id,))

    conn.commit()
    cur.close()
    conn.close()
    return True


# =========================
# INIT DB
# =========================
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # settings
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            username TEXT,
            first_seen TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # promos
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promos (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            image_url TEXT NOT NULL,
            caption TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)

    # promo buttons
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_buttons (
            id SERIAL PRIMARY KEY,
            promo_id INT REFERENCES promos(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            url TEXT NOT NULL,
            sort_order INT DEFAULT 0
        )
    """)

    # banner buttons
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banner_buttons (
            id SERIAL PRIMARY KEY,
            text TEXT NOT NULL,
            url TEXT,
            callback_data TEXT,
            sort_order INT DEFAULT 0
        )
    """)

        # blast vaults
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blast_vaults (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'random',
            send_time TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            last_sent_date TEXT
        )
    """)

    # blast items
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blast_items (
            id SERIAL PRIMARY KEY,
            vault_id INT REFERENCES blast_vaults(id) ON DELETE CASCADE,
            image_url TEXT,
            caption TEXT NOT NULL
        )
    """)
    # inline buttons for each blast item
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blast_item_buttons (
            id SERIAL PRIMARY KEY,
            item_id INT REFERENCES blast_items(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            button_type TEXT NOT NULL,
            button_value TEXT,
            sort_order INT DEFAULT 0
        )
    """)
    # multiple blast send times
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blast_times (
            id SERIAL PRIMARY KEY,
            vault_id INT REFERENCES blast_vaults(id) ON DELETE CASCADE,
            send_time TEXT NOT NULL,
            last_sent_date TEXT,
            UNIQUE(vault_id, send_time)
        )
    """)

    # pindahkan waktu lama ke table blast_times
    cur.execute("""
        INSERT INTO blast_times (
            vault_id,
            send_time,
            last_sent_date
        )
        SELECT
            id,
            send_time,
            last_sent_date
        FROM blast_vaults
        WHERE send_time IS NOT NULL
          AND send_time <> ''
        ON CONFLICT (vault_id, send_time) DO NOTHING
    """)

    conn.commit()

    # =========================
    # AUTO UPGRADE USERS TABLE
    # =========================
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT;")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_count INT DEFAULT 0;")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT;")
    conn.commit()

    # make referral_code unique (safe attempt)
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_referral_code_unique ON users(referral_code);")
        conn.commit()
    except:
        pass

    # defaults
    defaults = {
        "main_banner": "https://i.imgur.com/4M7IWwP.jpeg",
        "welcome_text": (
            "👋 **Welcome** {username}\n"
            "🆔 Your ID: **{user_id}**\n\n"
            "🔥 **Promotion Center**\n\n"
            "📊 Stats (Malaysia Time)\n"
            "📅 Today: **{today_count}**\n"
            "🗓 This Month: **{month_count}**"
        ),
        "about_text": "📌 **About Us**\n\nFast Withdraw | 24/7 Support",
        "about_banner": "",
        "register_url": "https://yourwebsite.com",
        "register_banner": "",
        "register_caption": "🎁 Register sekarang dan dapatkan bonus terbaik!",
        "telegram_support": "https://t.me/your_support",
        "whatsapp_url": "https://wa.me/60139661818",
        "contact_banner": "",
        "contact_caption": "📞 **Contact Us**",

        # manual correction
        "manual_today_add": "0",
        "manual_month_add": "0",

        # menu layout defaults
        "base_menu_layout": "📋 MENU, 📌 About\n📞 Contact, 🚀 Register",
        "promo_menu_layout": "AUTO_PROMOS_2\n📌 About\n⬅️ Back Menu\n📞 Contact, 🚀 Register",

        # referral settings
        "referral_enabled": "1",
        "referral_image": "https://i.imgur.com/4M7IWwP.jpeg",
        "referral_text": (
            "🎁 **Referral Program**\n\n"
            "Your referral code: **{ref_code}**\n"
            "Your referral link:\n"
            "{ref_link}\n\n"
            "👥 Total invited: **{ref_count}**"
        )
    }

    for k, v in defaults.items():
        cur.execute("SELECT key FROM settings WHERE key=%s", (k,))
        if not cur.fetchone():
            cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s)", (k, v))

    # default promos if empty
    cur.execute("SELECT COUNT(*) FROM promos")
    promo_count = cur.fetchone()[0]

    if promo_count == 0:
        cur.execute("""
            INSERT INTO promos (title, image_url, caption, is_active)
            VALUES
            (%s, %s, %s, TRUE),
            (%s, %s, %s, TRUE),
            (%s, %s, %s, TRUE)
            RETURNING id
        """, (
            "🔥 Promo 1", "https://i.imgur.com/5qHnQ0R.jpeg",
            "🔥 **WELCOME BONUS**\n\nDeposit RM50 → Free RM10\nFast Withdraw ⚡",

            "🎁 Promo 2", "https://i.imgur.com/8zQnF4T.jpeg",
            "🎁 **VIP CASHBACK**\n\nWeekly cashback up to 15%\nNo turnover required",

            "💎 Promo 3", "https://i.imgur.com/2gRkPjH.jpeg",
            "💎 **DAILY BONUS**\n\nDaily reward system\nFast payout ⚡"
        ))
        ids = cur.fetchall()

        for pid in ids:
            promo_id = pid[0]

            cur.execute("""
                INSERT INTO promo_buttons (promo_id, text, url, sort_order)
                VALUES (%s, %s, %s, %s)
            """, (promo_id, "🚀 Register", defaults["register_url"], 1))

            cur.execute("""
                INSERT INTO promo_buttons (promo_id, text, url, sort_order)
                VALUES (%s, %s, %s, %s)
            """, (promo_id, "💬 Contact", defaults["telegram_support"], 2))

    # default banner buttons if empty
    cur.execute("SELECT COUNT(*) FROM banner_buttons")
    banner_count = cur.fetchone()[0]

    if banner_count == 0:
        cur.execute("""
            INSERT INTO banner_buttons (text, url, callback_data, sort_order)
            VALUES (%s, %s, %s, %s)
        """, ("🚀 Register", defaults["register_url"], None, 1))

        cur.execute("""
            INSERT INTO banner_buttons (text, url, callback_data, sort_order)
            VALUES (%s, %s, %s, %s)
        """, ("📋 Menu", None, "open_menu", 2))

    conn.commit()
    cur.close()
    conn.close()


# =========================
# SETTINGS
# =========================
def get_setting(key):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT value FROM settings WHERE key=%s
    """, (key,))

    row = cur.fetchone()

    cur.close()
    conn.close()

    return row[0] if row else ""


def set_setting(key, value):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings (key, value)
        VALUES (%s, %s)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value
    """, (key, value))
    conn.commit()
    cur.close()
    conn.close()


def get_int_setting(key, default=0):
    try:
        return int(get_setting(key) or default)
    except:
        return default


# =========================
# USERS
# =========================
def ensure_user(user_id: int, username: str):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id FROM users WHERE telegram_id=%s", (user_id,))
    row = cur.fetchone()

    if not row:
        cur.execute("""
            INSERT INTO users (telegram_id, username)
            VALUES (%s, %s)
        """, (user_id, username))
    else:
        cur.execute("UPDATE users SET username=%s WHERE telegram_id=%s", (username, user_id))

    conn.commit()
    cur.close()
    conn.close()


def get_users_paginated(
    search=None,
    start_date=None,
    end_date=None,
    page=1,
    per_page=10
):
    offset = (page - 1) * per_page
    conn = get_db_connection()
    cur = conn.cursor()

    where = []
    params = []

    if search:
        where.append("""
            (
                CAST(telegram_id AS TEXT) ILIKE %s
                OR username ILIKE %s
                OR referral_code ILIKE %s
            )
        """)
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]

    if start_date and end_date:
        where.append("DATE(first_seen) BETWEEN %s AND %s")
        params += [start_date, end_date]

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    cur.execute(f"SELECT COUNT(*) FROM users {where_sql}", params)
    total = cur.fetchone()[0]

    cur.execute(f"""
        SELECT telegram_id,
               username,
               referral_code,
               referred_by,
               TO_CHAR(first_seen, 'DD Mon YYYY, HH12:MI AM') AS first_seen_fmt
        FROM users
        {where_sql}
        ORDER BY first_seen DESC
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])

    rows = cur.fetchall()
    cur.close()
    conn.close()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return rows, total, total_pages


def get_total_users():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return total


def get_today_count():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE DATE(first_seen) = DATE(NOW())
    """)

    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


def get_month_count():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE DATE_TRUNC('month', first_seen) = DATE_TRUNC('month', NOW())
    """)

    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


# =========================
# PROMOS
# =========================
def get_promos(active_only=True):
    conn = get_db_connection()
    cur = conn.cursor()

    if active_only:
        cur.execute("SELECT id, title, image_url, caption FROM promos WHERE is_active=TRUE ORDER BY id ASC")
    else:
        cur.execute("SELECT id, title, image_url, caption, is_active FROM promos ORDER BY id ASC")

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_promo_by_title(title):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, image_url, caption
        FROM promos
        WHERE title=%s AND is_active=TRUE
    """, (title,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def get_promo_buttons(promo_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, text, url
        FROM promo_buttons
        WHERE promo_id=%s
        ORDER BY sort_order ASC
    """, (promo_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_all_promo_buttons_by_promo():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            promo_id,
            id,
            text,
            url,
            sort_order
        FROM promo_buttons
        ORDER BY
            promo_id ASC,
            sort_order ASC,
            id ASC
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    buttons_by_promo = {}

    for (
        promo_id,
        button_id,
        text,
        url,
        sort_order
    ) in rows:

        buttons_by_promo.setdefault(
            promo_id,
            []
        ).append({
            "id": button_id,
            "text": text or "",
            "url": url or "",
            "sort_order": sort_order or 0
        })

    return buttons_by_promo
# =========================
# BANNER BUTTONS
# =========================
def get_banner_buttons():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, text, url, callback_data
        FROM banner_buttons
        ORDER BY sort_order ASC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# =========================
# KEYBOARD PARSER
# =========================
def parse_layout_to_keyboard(layout_text: str):
    rows = []
    for line in layout_text.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = [p.strip() for p in line.split(",") if p.strip()]
        if parts:
            rows.append(parts)

    return rows


# =========================
# KEYBOARDS
# =========================
def base_keyboard():
    layout = get_setting("base_menu_layout").strip()
    if not layout:
        layout = "📋 MENU, 📌 About\n📞 Contact, 🚀 Register"

    rows = parse_layout_to_keyboard(layout)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def expanded_keyboard():
    layout = get_setting("promo_menu_layout").strip()
    if not layout:
        layout = "AUTO_PROMOS_2\n📌 About\n⬅️ Back Menu\n📞 Contact, 🚀 Register"

    promos = get_promos(active_only=True)
    final_rows = []

    for line in layout.splitlines():
        line = line.strip()
        if not line:
            continue

        if line == "AUTO_PROMOS_2":
            row = []
            for p in promos:
                row.append(p[1])
                if len(row) == 2:
                    final_rows.append(row)
                    row = []
            if row:
                final_rows.append(row)
            continue

        if line == "AUTO_PROMOS_3":
            row = []
            for p in promos:
                row.append(p[1])
                if len(row) == 3:
                    final_rows.append(row)
                    row = []
            if row:
                final_rows.append(row)
            continue

        parts = [p.strip() for p in line.split(",") if p.strip()]
        if parts:
            final_rows.append(parts)

    return ReplyKeyboardMarkup(final_rows, resize_keyboard=True)


# =========================
# BOT HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username if user.username else user.first_name
    user_id = user.id

    ensure_user(user_id, username)

    # ensure referral_code exists
    ensure_referral_code(user_id)

    # referral bind if start has parameter
    if context.args:
        ref_code = context.args[0].strip().upper()
        if get_setting("referral_enabled") == "1":
            bind_referral(user_id, ref_code)

    real_today = get_today_count()
    real_month = get_month_count()

    manual_today = get_int_setting("manual_today_add", 0)
    manual_month = get_int_setting("manual_month_add", 0)

    today_count = real_today + manual_today
    month_count = real_month + manual_month

    banner_url = get_setting("main_banner")
    welcome_text = get_setting("welcome_text")

    text = (
        welcome_text
        .replace("{username}", username)
        .replace("{user_id}", str(user_id))
        .replace("{today_count}", str(today_count))
        .replace("{month_count}", str(month_count))
    )

    text = convert_markdown_bold_to_html(text)

    btns = get_banner_buttons()

    keyboard = []
    row = []

    for b in btns:
        _, text_btn, url, callback_data = b

        if url:
            row.append(InlineKeyboardButton(text_btn, url=url))
        elif callback_data:
            row.append(InlineKeyboardButton(text_btn, callback_data=callback_data))

    if row:
        keyboard.append(row)

    await update.message.reply_photo(
        photo=banner_url,
        caption=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def send_promo(update: Update, promo_id: int, image_url: str, caption: str):
    promo_btns = get_promo_buttons(promo_id)
    caption = convert_markdown_bold_to_html(caption)

    keyboard = []
    for btn in promo_btns:
        _, text_btn, url = btn
        keyboard.append([InlineKeyboardButton(text_btn, url=url)])

    keyboard.append([InlineKeyboardButton("⬅️ Back Menu", callback_data="back_menu")])

    await update.message.reply_photo(
        photo=image_url,
        caption=caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def send_referral_info(update: Update):
    user_id = update.effective_user.id
    ensure_referral_code(user_id)

    code, count = get_user_referral_info(user_id)

    bot_username = os.getenv("BOT_USERNAME", "").strip()
    if not bot_username:
        bot_username = "YourBotUsername"

    ref_link = f"https://t.me/{bot_username}?start={code}"

    ref_text = get_setting("referral_text")

    ref_text = (
        ref_text
        .replace("{ref_code}", code)
        .replace("{ref_link}", ref_link)
        .replace("{ref_count}", str(count))
    )

    ref_text = convert_markdown_bold_to_html(ref_text)

    # =========================
    # NEW: referral image
    # =========================
    referral_image = get_setting("referral_image")

    if not referral_image:
        referral_image = "https://i.imgur.com/4M7IWwP.jpeg"  # fallback image

    await update.message.reply_photo(
        photo=referral_image,
        caption=ref_text,
        parse_mode="HTML"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()

    # ================= MENU =================
    if msg == "📋 MENU":
        await update.message.reply_text("🔥 Menu Opened", reply_markup=expanded_keyboard())
        return

    if msg == "⬅️ Back Menu":
        await update.message.reply_text("Menu Closed", reply_markup=base_keyboard())
        return

    if msg == "📌 About":
        about_text = get_setting("about_text")
        about_text = convert_markdown_bold_to_html(about_text)

        about_banner = get_setting("about_banner")

        if about_banner:
            await update.message.reply_photo(
                photo=about_banner,
                caption=about_text,
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                about_text,
                parse_mode="HTML"
            )

        return

    if msg == "📞 Contact":
        telegram_support = get_setting("telegram_support")
        whatsapp_url = get_setting("whatsapp_url")

        contact_banner = get_setting("contact_banner")
        contact_caption = get_setting("contact_caption")
        contact_caption = convert_markdown_bold_to_html(contact_caption)

        keyboard = [
            [InlineKeyboardButton("💬 Telegram", url=telegram_support)],
            [InlineKeyboardButton("💬 WhatsApp", url=whatsapp_url)]
        ]

        if contact_banner:
            await update.message.reply_photo(
                photo=contact_banner,
                caption=contact_caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                contact_caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        return

    if msg == "🚀 Register":
        register_url = get_setting("register_url")
        register_banner = get_setting("register_banner")
        register_caption = get_setting("register_caption")

        keyboard = [
            [
                InlineKeyboardButton(
                    "🚀 Register Now",
                    url=register_url
                )
            ]
        ]

        if register_banner:
            await update.message.reply_photo(
                photo=register_banner,
                caption=convert_markdown_bold_to_html(register_caption),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                convert_markdown_bold_to_html(register_caption),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        return

    # ================= REFERRAL =================
    if (
        "referral" in msg.lower()
        or "invite" in msg.lower()
        or "邀请" in msg
        or "推荐" in msg
    ):
        if get_setting("referral_enabled") == "1":
            await send_referral_info(update)
        else:
            await update.message.reply_text("Referral system is disabled.")
        return

    # ================= PROMO =================
    promo = get_promo_by_title(msg)
    if promo:
        promo_id, title, image_url, caption = promo
        await send_promo(update, promo_id, image_url, caption)
        return

    # ================= ❌ INVALID FALLBACK =================
    await update.message.reply_text(
        "⚠️ Invalid input detected.\n\n"
        "👉 Please press /start to continue."
    )
        
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "open_menu":
        await query.message.reply_text("🔥 Promotion Menu", reply_markup=expanded_keyboard())

    elif query.data == "back_menu":
        await query.message.reply_text("Back to Menu", reply_markup=expanded_keyboard())


# =========================
# RUN BOT THREAD
# =========================
def bot_main():
    async def _runner():
        bot_app = Application.builder().token(BOT_TOKEN).build()

        bot_app.add_handler(CommandHandler("start", start))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        bot_app.add_handler(CallbackQueryHandler(button_handler))

        print("Bot running...")
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()

        await asyncio.Event().wait()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_runner())


def run_bot():
    bot_main()

# =========================
# CLOUDFLARE TURNSTILE VERIFY
# =========================
def verify_turnstile(
    token: str,
    remote_ip: str = ""
):
    if not TURNSTILE_ENABLED:
        return True, []

    if not token:
        return False, [
            "missing-input-response"
        ]

    payload = {
        "secret":
            TURNSTILE_SECRET_KEY,

        "response":
            token
    }

    if remote_ip:
        payload["remoteip"] = remote_ip

    request_data = urlencode(
        payload
    ).encode("utf-8")

    turnstile_request = Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=request_data,
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
        method="POST"
    )

    try:
        with urlopen(
            turnstile_request,
            timeout=10
        ) as response:

            result = json.loads(
                response
                .read()
                .decode("utf-8")
            )

        success = bool(
            result.get(
                "success",
                False
            )
        )

        error_codes = result.get(
            "error-codes",
            []
        )

        return success, error_codes

    except Exception as error:
        print(
            "[TURNSTILE VERIFY ERROR]",
            type(error).__name__,
            str(error)
        )

        return False, [
            "verification-request-failed"
        ]
# =========================
# ADMIN AUTH
# =========================
def require_login():
    # Kalau belum login
    if not session.get(
        "admin_logged_in"
    ):
        return False

    # Ambil masa session tamat
    expires_at = session.get(
        "admin_expires_at"
    )

    # Session lama yang belum mempunyai masa tamat
    if not expires_at:
        session.clear()
        return False

    try:
        expires_at = int(
            expires_at
        )
    except (
        TypeError,
        ValueError
    ):
        session.clear()
        return False

    # Kalau sudah lebih 24 jam
    if int(time.time()) >= expires_at:
        session.clear()

        # Simpan tanda supaya login page
        # boleh tunjuk mesej session expired
        session[
            "admin_session_expired"
        ] = True

        return False

    return True


@flask_app.route("/")
def home():
    return redirect("/admin")


@flask_app.route(
    "/admin/login",
    methods=["GET", "POST"]
)
def admin_login():
    # Kalau session masih aktif,
    # terus masuk dashboard.
    if require_login():
        return redirect("/admin")

    # Semak sama ada user dihantar keluar
    # kerana session sudah tamat.
    session_expired = session.pop(
        "admin_session_expired",
        False
    )

    def render_login_page(
        error_message=None
    ):
        return render_template(
            "login.html",
            error=error_message,
            turnstile_site_key=(
                TURNSTILE_SITE_KEY
                if TURNSTILE_ENABLED
                else ""
            )
        )

    if request.method == "POST":
        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        # Token Cloudflare Turnstile
        turnstile_token = (
            request.form.get(
                "cf-turnstile-response",
                ""
            )
            .strip()
        )

        # =========================
        # VERIFY CLOUDFLARE
        # =========================
        if TURNSTILE_ENABLED:
            remote_ip = (
                request.headers.get(
                    "CF-Connecting-IP"
                )
                or request.headers.get(
                    "X-Forwarded-For",
                    ""
                ).split(",")[0].strip()
                or request.remote_addr
                or ""
            )

            verified, error_codes = (
                verify_turnstile(
                    turnstile_token,
                    remote_ip
                )
            )

            if not verified:
                print(
                    "[TURNSTILE FAILED]",
                    error_codes
                )

                return render_login_page(
                    "Cloudflare verification failed. "
                    "Please try again."
                )

        # =========================
        # VERIFY ADMIN LOGIN
        # =========================
        if (
            username == ADMIN_USER
            and password == ADMIN_PASS
        ):
            # Buang session lama
            session.clear()

            # Tandakan sudah login
            session[
                "admin_logged_in"
            ] = True

            session[
                "admin_username"
            ] = username

            # Simpan waktu login
            session[
                "admin_login_at"
            ] = int(
                time.time()
            )

            # Auto logout selepas 24 jam
            session[
                "admin_expires_at"
            ] = int(
                time.time()
            ) + (24 * 60 * 60)

            return redirect("/admin")

        return render_login_page(
            "Invalid username or password"
        )

    # Paparkan mesej selepas session tamat
    if session_expired:
        return render_login_page(
            "Your login session has expired. "
            "Please log in again."
        )

    return render_login_page()


@flask_app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")

# =========================
# CHECK ADMIN SESSION
# =========================
@flask_app.route("/admin/session-status")
def admin_session_status():
    if not require_login():
        return jsonify({
            "logged_in": False,
            "redirect": "/admin/login"
        }), 401

    expires_at = session.get(
        "admin_expires_at",
        0
    )

    remaining_seconds = max(
        0,
        int(expires_at) - int(time.time())
    )

    return jsonify({
        "logged_in": True,
        "remaining_seconds": remaining_seconds
    })


# =========================
# DASHBOARD SETTINGS
# =========================
@flask_app.route(
    "/admin",
    methods=["GET", "POST"]
)
def admin_dashboard():
    if not require_login():
        return redirect("/admin/login")

    if request.method == "POST":
        main_banner = request.form.get(
            "main_banner",
            ""
        ).strip()

        if main_banner:
            set_setting(
                "main_banner",
                main_banner
            )

        set_setting(
            "welcome_text",
            request.form.get(
                "welcome_text",
                ""
            )
        )

        set_setting(
            "about_text",
            request.form.get(
                "about_text",
                ""
            )
        )

        set_setting(
            "about_banner",
            request.form.get(
                "about_banner",
                ""
            )
        )

        set_setting(
            "register_url",
            request.form.get(
                "register_url",
                ""
            )
        )

        set_setting(
            "register_banner",
            request.form.get(
                "register_banner",
                ""
            )
        )

        set_setting(
            "register_caption",
            request.form.get(
                "register_caption",
                ""
            )
        )

        set_setting(
            "telegram_support",
            request.form.get(
                "telegram_support",
                ""
            )
        )

        set_setting(
            "whatsapp_url",
            request.form.get(
                "whatsapp_url",
                ""
            )
        )

        set_setting(
            "contact_banner",
            request.form.get(
                "contact_banner",
                ""
            )
        )

        set_setting(
            "contact_caption",
            request.form.get(
                "contact_caption",
                ""
            )
        )

        set_setting(
            "manual_today_add",
            request.form.get(
                "manual_today_add",
                "0"
            )
        )

        set_setting(
            "manual_month_add",
            request.form.get(
                "manual_month_add",
                "0"
            )
        )

        set_setting(
            "referral_enabled",
            request.form.get(
                "referral_enabled",
                "0"
            )
        )

        set_setting(
            "referral_image",
            request.form.get(
                "referral_image",
                ""
            )
        )

        set_setting(
            "referral_text",
            request.form.get(
                "referral_text",
                ""
            )
        )

        return redirect("/admin")

    data = {
        "main_banner":
            get_setting(
                "main_banner"
            ),

        "welcome_text":
            get_setting(
                "welcome_text"
            ),

        "about_text":
            get_setting(
                "about_text"
            ),

        "about_banner":
            get_setting(
                "about_banner"
            ),

        "register_url":
            get_setting(
                "register_url"
            ),

        "register_banner":
            get_setting(
                "register_banner"
            ),

        "register_caption":
            get_setting(
                "register_caption"
            ),

        "telegram_support":
            get_setting(
                "telegram_support"
            ),

        "whatsapp_url":
            get_setting(
                "whatsapp_url"
            ),

        "contact_banner":
            get_setting(
                "contact_banner"
            ),

        "contact_caption":
            get_setting(
                "contact_caption"
            ),

        "manual_today_add":
            get_setting(
                "manual_today_add"
            ),

        "manual_month_add":
            get_setting(
                "manual_month_add"
            ),

        "referral_enabled":
            get_setting(
                "referral_enabled"
            ),

        "referral_image":
            get_setting(
                "referral_image"
            ),

        "referral_text":
            get_setting(
                "referral_text"
            )
    }

    uploaded_url = session.pop(
        "uploaded_url",
        None
    )

    upload_error = session.pop(
        "upload_error",
        None
    )

    return render_template(
        "dashboard.html",
        data=data,
        uploaded_url=uploaded_url,
        upload_error=upload_error
    )


# =========================
# MENU LAYOUT SAVE
# =========================
@flask_app.route("/admin/menu_layout", methods=["POST"])
def admin_save_menu_layout():
    if not require_login():
        return redirect("/admin/login")

    base_layout = request.form.get("base_menu_layout", "").strip()
    promo_layout = request.form.get("promo_menu_layout", "").strip()

    if base_layout:
        set_setting("base_menu_layout", base_layout)

    if promo_layout:
        set_setting("promo_menu_layout", promo_layout)

    return redirect("/admin/promos")


# =========================
# PROMOS LIST
# =========================
@flask_app.route("/admin/promos")
def admin_promos():
    if not require_login():
        return redirect("/admin/login")

    promos = get_promos(
        active_only=False
    )

    promo_buttons_by_promo = (
        get_all_promo_buttons_by_promo()
    )

    base_layout = get_setting(
        "base_menu_layout"
    )

    promo_layout = get_setting(
        "promo_menu_layout"
    )

    return render_template(
        "promos.html",
        promos=promos,
        promo_buttons_by_promo=promo_buttons_by_promo,
        base_layout=base_layout,
        promo_layout=promo_layout
    )


@flask_app.route("/admin/promos/add", methods=["POST"])
def admin_add_promo():
    if not require_login():
        return redirect("/admin/login")

    title = request.form.get("title", "").strip()
    image_url = request.form.get("image_url", "").strip()
    caption = request.form.get("caption", "").strip()

    if not title or not image_url or not caption:
        return redirect("/admin/promos")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO promos (title, image_url, caption, is_active)
        VALUES (%s, %s, %s, TRUE)
    """, (title, image_url, caption))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/admin/promos")


@flask_app.route("/admin/promos/delete/<int:promo_id>")
def admin_delete_promo(promo_id):
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM promos WHERE id=%s", (promo_id,))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/admin/promos")


@flask_app.route("/admin/promos/toggle/<int:promo_id>")
def admin_toggle_promo(promo_id):
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE promos SET is_active = NOT is_active WHERE id=%s", (promo_id,))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/admin/promos")


# =========================
# USERS PAGE
# =========================
@flask_app.route("/admin/users")
def admin_users():
    if not require_login():
        return redirect("/admin/login")

    from datetime import datetime
    import calendar

    q = request.args.get("q", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    # Default kepada seluruh bulan semasa
    if not start_date or not end_date:
        malaysia_tz = ZoneInfo("Asia/Kuala_Lumpur")
        now = datetime.now(malaysia_tz)

        start_date = now.replace(
            day=1
        ).strftime("%Y-%m-%d")

        last_day_number = calendar.monthrange(
            now.year,
            now.month
        )[1]

        end_date = now.replace(
            day=last_day_number
        ).strftime("%Y-%m-%d")

    try:
        page = max(
            1,
            int(request.args.get("page", 1))
        )
    except (TypeError, ValueError):
        page = 1

    try:
        per_page = int(
            request.args.get("per_page", 10)
        )
    except (TypeError, ValueError):
        per_page = 10

    if per_page not in (10, 20, 50, 100):
        per_page = 10

    total_users = get_total_users()
    today = get_today_count()
    month = get_month_count()

    users, total_filtered, total_pages = get_users_paginated(
        search=q if q else None,
        start_date=start_date,
        end_date=end_date,
        page=page,
        per_page=per_page
    )

    # Jika page lebih besar daripada total page,
    # bawa balik ke page terakhir.
    if page > total_pages:
        page = total_pages

        users, total_filtered, total_pages = get_users_paginated(
            search=q if q else None,
            start_date=start_date,
            end_date=end_date,
            page=page,
            per_page=per_page
        )

    return render_template(
        "users.html",
        total=total_users,
        today=today,
        month=month,
        users=users,
        q=q,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_filtered=total_filtered,
        start_date=start_date,
        end_date=end_date
    )
    
@flask_app.route("/admin/users/search")
def admin_users_search():
    if not require_login():
        return jsonify({
            "error": "unauthorized"
        }), 401

    q = request.args.get("q", "").strip()
    start_date = request.args.get(
        "start_date",
        ""
    ).strip()

    end_date = request.args.get(
        "end_date",
        ""
    ).strip()

    try:
        page = max(
            1,
            int(request.args.get("page", 1))
        )
    except (TypeError, ValueError):
        page = 1

    try:
        per_page = int(
            request.args.get("per_page", 10)
        )
    except (TypeError, ValueError):
        per_page = 10

    if per_page not in (10, 20, 50, 100):
        per_page = 10

    users, total_filtered, total_pages = get_users_paginated(
        search=q if q else None,
        start_date=(
            start_date
            if start_date
            else None
        ),
        end_date=(
            end_date
            if end_date
            else None
        ),
        page=page,
        per_page=per_page
    )

    if page > total_pages:
        page = total_pages

        users, total_filtered, total_pages = get_users_paginated(
            search=q if q else None,
            start_date=(
                start_date
                if start_date
                else None
            ),
            end_date=(
                end_date
                if end_date
                else None
            ),
            page=page,
            per_page=per_page
        )

    return jsonify({
        "users": users,
        "page": page,
        "per_page": per_page,
        "total_filtered": total_filtered,
        "total_pages": total_pages
    })
    
@flask_app.route("/admin/broadcast", methods=["POST"])
def admin_broadcast():
    if not require_login():
        return redirect("/admin/login")

    image_url = request.form.get("broadcast_image", "").strip()
    caption = request.form.get("broadcast_caption", "").strip()
    target = request.form.get("broadcast_target", "all")
    target_user_id = request.form.get("target_user_id", "").strip()

    if not caption:
        return redirect("/admin/users")

    if target == "single":
        if not target_user_id:
            return redirect("/admin/users")

        try:
            users = [(int(target_user_id),)]
        except ValueError:
            return redirect("/admin/users")
    else:
        users, total, total_pages = get_users_paginated(
            page=1,
            per_page=100000
        )

    async def send_all():
        bot_app = Application.builder().token(BOT_TOKEN).build()
        bot = bot_app.bot

        text = convert_markdown_bold_to_html(caption)

        for u in users:
            telegram_id = u[0]

            try:
                if image_url:
                    await bot.send_photo(
                        chat_id=telegram_id,
                        photo=image_url,
                        caption=text,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=text,
                        parse_mode="HTML"
                    )

            except Exception as e:
                print("Broadcast failed:", telegram_id, e)

    threading.Thread(
        target=lambda: asyncio.run(send_all()),
        daemon=True
    ).start()

    return redirect("/admin/users")
    
def get_all_user_ids():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM users ORDER BY first_seen DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


async def send_blast_to_all(item_id, image_url, caption):
    users = get_all_user_ids()
    text = convert_markdown_bold_to_html(caption)

    reply_markup = get_blast_item_keyboard(item_id)

    print(f"[BLAST USERS] Total target users: {len(users)}")

    success_count = 0
    failed_count = 0

    async with Bot(token=BOT_TOKEN) as bot:
        for user in users:
            telegram_id = user[0]

            try:
                if image_url:
                    await bot.send_photo(
                        chat_id=telegram_id,
                        photo=image_url,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                else:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )

                success_count += 1
                print(f"[BLAST USER SUCCESS] {telegram_id}")

            except Exception as e:
                failed_count += 1

                print(
                    f"[BLAST USER FAILED] {telegram_id}: "
                    f"{type(e).__name__}: {e}"
                )

    print(
        f"[BLAST FINISHED] "
        f"Success={success_count}, Failed={failed_count}"
    )
    
def get_blast_item_keyboard(item_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            text,
            button_type,
            button_value
        FROM blast_item_buttons
        WHERE item_id=%s
        ORDER BY sort_order ASC, id ASC
    """, (item_id,))

    buttons = cur.fetchall()

    cur.close()
    conn.close()

    if not buttons:
        return None

    keyboard = []
    current_row = []

    for text, button_type, button_value in buttons:
        text = (text or "").strip()
        button_type = (button_type or "").strip()
        button_value = (button_value or "").strip()

        if not text:
            continue

        if button_type == "url":
            if not button_value.startswith(("https://", "http://")):
                continue

            telegram_button = InlineKeyboardButton(
                text=text,
                url=button_value
            )

        elif button_type == "menu":
            telegram_button = InlineKeyboardButton(
                text=text,
                callback_data="open_menu"
            )

        else:
            continue

        current_row.append(telegram_button)

        # dua tombol setiap baris
        if len(current_row) == 2:
            keyboard.append(current_row)
            current_row = []

    if current_row:
        keyboard.append(current_row)

    if not keyboard:
        return None

    return InlineKeyboardMarkup(keyboard)
    
@flask_app.route("/admin/blast", methods=["GET", "POST"])
def admin_blast():
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        mode = request.form.get("mode", "random").strip()

        send_times = [
            value.strip()
            for value in request.form.getlist("send_time[]")
            if value.strip()
        ]

        send_times = list(dict.fromkeys(send_times))

        images = request.form.getlist("image_url[]")
        captions = request.form.getlist("caption[]")
        buttons_json_list = request.form.getlist("buttons_json[]")

        valid_items = []

        max_items = max(
            len(images),
            len(captions),
            len(buttons_json_list),
            0
        )

        for index in range(max_items):
            image_url = (
                images[index].strip()
                if index < len(images)
                else ""
            )

            caption = (
                captions[index].strip()
                if index < len(captions)
                else ""
            )

            raw_buttons = (
                buttons_json_list[index]
                if index < len(buttons_json_list)
                else "[]"
            )

            try:
                submitted_buttons = json.loads(raw_buttons)
            except (json.JSONDecodeError, TypeError):
                submitted_buttons = []

            clean_buttons = []

            for button in submitted_buttons:
                text = str(button.get("text", "")).strip()
                button_type = str(button.get("type", "")).strip()
                value = str(button.get("value", "")).strip()

                if not text:
                    continue

                if button_type == "url":
                    if not value.startswith(("https://", "http://")):
                        continue

                    clean_buttons.append({
                        "text": text,
                        "type": "url",
                        "value": value
                    })

                elif button_type == "menu":
                    clean_buttons.append({
                        "text": text,
                        "type": "menu",
                        "value": "open_menu"
                    })

            if caption:
                valid_items.append({
                    "image_url": image_url,
                    "caption": caption,
                    "buttons": clean_buttons
                })

        if (
            not name
            or not send_times
            or not valid_items
            or mode not in ("random", "fixed")
        ):
            cur.close()
            conn.close()
            return redirect("/admin/blast")

        first_time = send_times[0]

        cur.execute("""
            INSERT INTO blast_vaults (
                name,
                mode,
                send_time,
                is_active,
                last_sent_date
            )
            VALUES (%s, %s, %s, TRUE, NULL)
            RETURNING id
        """, (
            name,
            mode,
            first_time
        ))

        vault_id = cur.fetchone()[0]

        for send_time in send_times:
            cur.execute("""
                INSERT INTO blast_times (
                    vault_id,
                    send_time,
                    last_sent_date
                )
                VALUES (%s, %s, NULL)
                ON CONFLICT (vault_id, send_time) DO NOTHING
            """, (
                vault_id,
                send_time
            ))

        for item in valid_items:
            cur.execute("""
                INSERT INTO blast_items (
                    vault_id,
                    image_url,
                    caption
                )
                VALUES (%s, %s, %s)
                RETURNING id
            """, (
                vault_id,
                item["image_url"],
                item["caption"]
            ))

            item_id = cur.fetchone()[0]

            for sort_order, button in enumerate(
                item["buttons"],
                start=1
            ):
                cur.execute("""
                    INSERT INTO blast_item_buttons (
                        item_id,
                        text,
                        button_type,
                        button_value,
                        sort_order
                    )
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    item_id,
                    button["text"],
                    button["type"],
                    button["value"],
                    sort_order
                ))

        conn.commit()
        cur.close()
        conn.close()

        return redirect("/admin/blast")

    cur.execute("""
        SELECT
            id,
            name,
            mode,
            send_time,
            is_active,
            last_sent_date
        FROM blast_vaults
        ORDER BY id DESC
    """)
    vaults = cur.fetchall()

    cur.execute("""
        SELECT
            id,
            vault_id,
            image_url,
            caption
        FROM blast_items
        ORDER BY id ASC
    """)
    items = cur.fetchall()

    cur.execute("""
        SELECT
            id,
            vault_id,
            send_time,
            last_sent_date
        FROM blast_times
        ORDER BY send_time ASC
    """)
    blast_times = cur.fetchall()

    cur.close()
    conn.close()

    times_by_vault = {}

    for time_row in blast_times:
        time_id, vault_id, send_time, last_sent_date = time_row

        times_by_vault.setdefault(vault_id, []).append({
            "id": time_id,
            "send_time": send_time,
            "last_sent_date": last_sent_date
        })

    return render_template(
        "blast.html",
        vaults=vaults,
        items=items,
        times_by_vault=times_by_vault
    )


@flask_app.route("/admin/blast/delete/<int:vault_id>")
def admin_blast_delete(vault_id):
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM blast_vaults WHERE id=%s", (vault_id,))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/admin/blast")


@flask_app.route("/admin/blast/toggle/<int:vault_id>")
def admin_blast_toggle(vault_id):
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE blast_vaults SET is_active = NOT is_active WHERE id=%s", (vault_id,))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/admin/blast")

@flask_app.route(
    "/admin/blast/edit/<int:vault_id>",
    methods=["GET", "POST"]
)
def admin_blast_edit(vault_id):
    if not require_login():
        if request.method == "GET":
            return jsonify({
                "success": False,
                "message": "Unauthorized"
            }), 401

        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()

    # =========================
    # SAVE EDITED VAULT
    # =========================
    if request.method == "POST":
        name = request.form.get(
            "name",
            ""
        ).strip()

        mode = request.form.get(
            "mode",
            "random"
        ).strip()

        send_times = [
            value.strip()
            for value in request.form.getlist(
                "send_time[]"
            )
            if value.strip()
        ]

        send_times = list(
            dict.fromkeys(send_times)
        )

        images = request.form.getlist(
            "image_url[]"
        )

        captions = request.form.getlist(
            "caption[]"
        )

        buttons_json_list = request.form.getlist(
            "buttons_json[]"
        )

        valid_items = []

        max_items = max(
            len(images),
            len(captions),
            len(buttons_json_list),
            0
        )

        for index in range(max_items):
            image_url = (
                images[index].strip()
                if index < len(images)
                else ""
            )

            caption = (
                captions[index].strip()
                if index < len(captions)
                else ""
            )

            raw_buttons = (
                buttons_json_list[index]
                if index < len(buttons_json_list)
                else "[]"
            )

            try:
                submitted_buttons = json.loads(
                    raw_buttons
                )

                if not isinstance(
                    submitted_buttons,
                    list
                ):
                    submitted_buttons = []

            except (
                json.JSONDecodeError,
                TypeError
            ):
                submitted_buttons = []

            clean_buttons = []

            for button in submitted_buttons:
                if not isinstance(button, dict):
                    continue

                text = str(
                    button.get(
                        "text",
                        ""
                    )
                ).strip()

                button_type = str(
                    button.get(
                        "type",
                        ""
                    )
                ).strip()

                value = str(
                    button.get(
                        "value",
                        ""
                    )
                ).strip()

                if not text:
                    continue

                if button_type == "url":
                    if not value.startswith(
                        (
                            "https://",
                            "http://"
                        )
                    ):
                        continue

                    clean_buttons.append({
                        "text": text,
                        "type": "url",
                        "value": value
                    })

                elif button_type == "menu":
                    clean_buttons.append({
                        "text": text,
                        "type": "menu",
                        "value": "open_menu"
                    })

            if caption:
                valid_items.append({
                    "image_url": image_url,
                    "caption": caption,
                    "buttons": clean_buttons
                })

        if (
            not name
            or not send_times
            or not valid_items
            or mode not in (
                "random",
                "fixed"
            )
        ):
            cur.close()
            conn.close()

            return redirect("/admin/blast")

        # Fixed mode hanya simpan satu item.
        if mode == "fixed":
            valid_items = valid_items[:1]

        first_time = send_times[0]

        try:
            cur.execute("""
                UPDATE blast_vaults
                SET
                    name=%s,
                    mode=%s,
                    send_time=%s,
                    last_sent_date=NULL
                WHERE id=%s
            """, (
                name,
                mode,
                first_time,
                vault_id
            ))

            cur.execute("""
                DELETE FROM blast_times
                WHERE vault_id=%s
            """, (
                vault_id,
            ))

            for send_time in send_times:
                cur.execute("""
                    INSERT INTO blast_times (
                        vault_id,
                        send_time,
                        last_sent_date
                    )
                    VALUES (%s, %s, NULL)
                """, (
                    vault_id,
                    send_time
                ))

            # Item button ikut terhapus kerana
            # ON DELETE CASCADE.
            cur.execute("""
                DELETE FROM blast_items
                WHERE vault_id=%s
            """, (
                vault_id,
            ))

            for item in valid_items:
                cur.execute("""
                    INSERT INTO blast_items (
                        vault_id,
                        image_url,
                        caption
                    )
                    VALUES (%s, %s, %s)
                    RETURNING id
                """, (
                    vault_id,
                    item["image_url"],
                    item["caption"]
                ))

                item_id = cur.fetchone()[0]

                for sort_order, button in enumerate(
                    item["buttons"],
                    start=1
                ):
                    cur.execute("""
                        INSERT INTO blast_item_buttons (
                            item_id,
                            text,
                            button_type,
                            button_value,
                            sort_order
                        )
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        item_id,
                        button["text"],
                        button["type"],
                        button["value"],
                        sort_order
                    ))

            conn.commit()

        except Exception as error:
            conn.rollback()

            print(
                "[BLAST EDIT ERROR]",
                type(error).__name__,
                str(error)
            )

            cur.close()
            conn.close()

            return redirect("/admin/blast")

        cur.close()
        conn.close()

        return redirect("/admin/blast")

    # =========================
    # GET JSON FOR EDIT MODAL
    # =========================
    cur.execute("""
        SELECT
            id,
            name,
            mode,
            send_time,
            is_active
        FROM blast_vaults
        WHERE id=%s
    """, (
        vault_id,
    ))

    vault = cur.fetchone()

    if not vault:
        cur.close()
        conn.close()

        return jsonify({
            "success": False,
            "message": "Blast vault not found"
        }), 404

    cur.execute("""
        SELECT
            send_time
        FROM blast_times
        WHERE vault_id=%s
        ORDER BY send_time ASC
    """, (
        vault_id,
    ))

    time_rows = cur.fetchall()

    cur.execute("""
        SELECT
            id,
            image_url,
            caption
        FROM blast_items
        WHERE vault_id=%s
        ORDER BY id ASC
    """, (
        vault_id,
    ))

    item_rows = cur.fetchall()

    item_ids = [
        item[0]
        for item in item_rows
    ]

    buttons_by_item = {}

    if item_ids:
        cur.execute("""
            SELECT
                item_id,
                text,
                button_type,
                button_value,
                sort_order
            FROM blast_item_buttons
            WHERE item_id = ANY(%s)
            ORDER BY
                item_id ASC,
                sort_order ASC,
                id ASC
        """, (
            item_ids,
        ))

        button_rows = cur.fetchall()

        for (
            item_id,
            text,
            button_type,
            button_value,
            sort_order
        ) in button_rows:

            buttons_by_item.setdefault(
                item_id,
                []
            ).append({
                "text": text or "",
                "type": button_type or "url",
                "value": button_value or ""
            })

    cur.close()
    conn.close()

    send_times = [
        str(row[0])[:5]
        for row in time_rows
        if row[0]
    ]

    if not send_times and vault[3]:
        send_times = [
            str(vault[3])[:5]
        ]

    items_data = []

    for (
        item_id,
        image_url,
        caption
    ) in item_rows:

        items_data.append({
            "id": item_id,
            "image_url": image_url or "",
            "caption": caption or "",
            "buttons": buttons_by_item.get(
                item_id,
                []
            )
        })

    return jsonify({
        "success": True,
        "vault": {
            "id": vault[0],
            "name": vault[1],
            "mode": vault[2],
            "is_active": bool(vault[4]),
            "send_times": send_times,
            "items": items_data
        }
    })
# =========================
# PROMO EDIT + INLINE BUTTONS
# =========================
@flask_app.route(
    "/admin/promo/<int:promo_id>",
    methods=["GET", "POST"]
)
def admin_edit_promo(promo_id):
    if not require_login():
        return redirect("/admin/login")

    # Page promo_edit.html sudah dibuang.
    if request.method == "GET":
        return redirect("/admin/promos")

    title = request.form.get(
        "title",
        ""
    ).strip()

    image_url = request.form.get(
        "image_url",
        ""
    ).strip()

    caption = request.form.get(
        "caption",
        ""
    ).strip()

    button_texts = request.form.getlist(
        "button_text[]"
    )

    button_urls = request.form.getlist(
        "button_url[]"
    )

    if not title or not image_url or not caption:
        return redirect("/admin/promos")

    clean_buttons = []

    max_buttons = max(
        len(button_texts),
        len(button_urls),
        0
    )

    for index in range(max_buttons):
        text = (
            button_texts[index].strip()
            if index < len(button_texts)
            else ""
        )

        url = (
            button_urls[index].strip()
            if index < len(button_urls)
            else ""
        )

        # Row kosong diabaikan.
        if not text and not url:
            continue

        # Row tidak lengkap diabaikan.
        if not text or not url:
            continue

        # Telegram URL button perlukan URL sah.
        if not url.startswith(
            ("https://", "http://")
        ):
            continue

        clean_buttons.append({
            "text": text,
            "url": url
        })

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE promos
            SET
                title=%s,
                image_url=%s,
                caption=%s
            WHERE id=%s
        """, (
            title,
            image_url,
            caption,
            promo_id
        ))

        # Buang semua buttons lama promo ini.
        cur.execute("""
            DELETE FROM promo_buttons
            WHERE promo_id=%s
        """, (
            promo_id,
        ))

        # Simpan semula ikut urutan modal.
        for sort_order, button in enumerate(
            clean_buttons,
            start=1
        ):
            cur.execute("""
                INSERT INTO promo_buttons (
                    promo_id,
                    text,
                    url,
                    sort_order
                )
                VALUES (%s, %s, %s, %s)
            """, (
                promo_id,
                button["text"],
                button["url"],
                sort_order
            ))

        conn.commit()

    except Exception as error:
        conn.rollback()
        print(
            "[PROMO EDIT ERROR]",
            type(error).__name__,
            str(error)
        )

        cur.close()
        conn.close()

        return redirect("/admin/promos")

    cur.close()
    conn.close()

    return redirect("/admin/promos")

# =========================
# BANNER BUTTONS
# =========================
@flask_app.route("/admin/banner_buttons", methods=["GET", "POST"])
def admin_banner_buttons():
    if not require_login():
        return redirect("/admin/login")

    if request.method == "POST":
        text_btn = request.form.get("text", "").strip()
        url = request.form.get("url", "").strip()
        callback_data = request.form.get("callback_data", "").strip()

        if not text_btn:
            return redirect("/admin/banner_buttons")

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM banner_buttons")
        max_sort = cur.fetchone()[0] + 1

        cur.execute("""
            INSERT INTO banner_buttons (text, url, callback_data, sort_order)
            VALUES (%s, %s, %s, %s)
        """, (
            text_btn,
            url if url else None,
            callback_data if callback_data else None,
            max_sort
        ))

        conn.commit()
        cur.close()
        conn.close()

        return redirect("/admin/banner_buttons")

    buttons = get_banner_buttons()
    return render_template("banner_buttons.html", buttons=buttons)


@flask_app.route("/admin/banner_buttons/delete/<int:button_id>")
def admin_delete_banner_button(button_id):
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM banner_buttons WHERE id=%s", (button_id,))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/admin/banner_buttons")


# =========================
# UPLOAD BANNER
# =========================
@flask_app.route(
    "/admin/upload_banner",
    methods=["POST"]
)
def upload_banner():
    if not require_login():
        return redirect("/admin/login")

    try:
        file = request.files.get(
            "image"
        )

        # Field file tidak dihantar.
        if file is None:
            session["upload_error"] = (
                "Please choose or paste an image first."
            )

            return redirect("/admin")

        # Field ada tetapi fail kosong.
        if not file.filename:
            session["upload_error"] = (
                "The selected image is empty."
            )

            return redirect("/admin")

        # Pastikan file ialah gambar.
        if not (
            file.mimetype
            and file.mimetype.startswith(
                "image/"
            )
        ):
            session["upload_error"] = (
                "Only image files are allowed."
            )

            return redirect("/admin")

        url = upload_to_cloudinary(
            file
        )

        session["uploaded_url"] = url

        return redirect("/admin")

    except Exception as error:
        print(
            "[UPLOAD ERROR]",
            type(error).__name__,
            str(error)
        )

        session["upload_error"] = (
            "Image upload failed. "
            "Please try again."
        )

        return redirect("/admin")

def blast_scheduler():
    import time
    import random
    from datetime import datetime

    malaysia_tz = ZoneInfo("Asia/Kuala_Lumpur")

    while True:
        conn = None
        cur = None

        try:
            now = datetime.now(malaysia_tz)
            today = now.strftime("%Y-%m-%d")
            current_time = now.strftime("%H:%M")

            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("""
                SELECT
                    bt.id,
                    bt.vault_id,
                    bt.send_time,
                    bt.last_sent_date,
                    bv.name,
                    bv.mode
                FROM blast_times bt
                JOIN blast_vaults bv
                    ON bv.id = bt.vault_id
                WHERE bv.is_active = TRUE
                ORDER BY bt.id ASC
            """)

            schedules = cur.fetchall()

            for (
                time_id,
                vault_id,
                send_time,
                last_sent_date,
                vault_name,
                mode
            ) in schedules:

                saved_time = str(send_time or "").strip()[:5]
                last_sent = str(last_sent_date or "").strip()

                if saved_time != current_time:
                    continue

                if last_sent == today:
                    continue

                cur.execute("""
                    SELECT
                        id,
                        image_url,
                        caption
                    FROM blast_items
                    WHERE vault_id=%s
                    ORDER BY id ASC
                """, (vault_id,))

                items = cur.fetchall()

                if not items:
                    print(
                        f"[BLAST SKIP] Vault {vault_id} "
                        f"has no items"
                    )
                    continue

                if mode == "random":
                    item_id, image_url, caption = random.choice(items)
                else:
                    item_id, image_url, caption = items[0]

                print(
                    f"[BLAST SEND] "
                    f"Vault={vault_name}, "
                    f"Time={saved_time}"
                )

                asyncio.run(
                    send_blast_to_all(
                        item_id,
                        image_url,
                        caption
                    )
                )

                cur.execute("""
                    UPDATE blast_times
                    SET last_sent_date=%s
                    WHERE id=%s
                """, (
                    today,
                    time_id
                ))

                conn.commit()

                print(
                    f"[BLAST SUCCESS] "
                    f"Vault={vault_name}, "
                    f"Time={saved_time}"
                )

        except Exception as e:
            print(
                "[BLAST SCHEDULER ERROR]",
                type(e).__name__,
                str(e)
            )

            if conn:
                conn.rollback()

        finally:
            if cur:
                cur.close()

            if conn:
                conn.close()

        time.sleep(15)

@flask_app.route("/admin/blast/upload", methods=["POST"])
def admin_blast_upload():
    if not require_login():
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    file = request.files.get("image")

    if not file or not file.filename:
        return jsonify({
            "success": False,
            "message": "No image selected"
        }), 400

    try:
        image_url = upload_to_cloudinary(file)

        return jsonify({
            "success": True,
            "url": image_url
        })

    except Exception as e:
        print("Blast upload error:", e)

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500
# =========================
# START SYSTEM
# =========================
init_db()
clear_webhook()

BOT_STARTED = False
BLAST_SCHEDULER_STARTED = False


def start_bot_once():
    global BOT_STARTED

    if BOT_STARTED:
        return

    BOT_STARTED = True

    bot_thread = threading.Thread(
        target=run_bot,
        daemon=True
    )
    bot_thread.start()

    print("Bot thread running...")


def start_blast_scheduler_once():
    global BLAST_SCHEDULER_STARTED

    if BLAST_SCHEDULER_STARTED:
        return

    BLAST_SCHEDULER_STARTED = True

    blast_thread = threading.Thread(
        target=blast_scheduler,
        daemon=True
    )
    blast_thread.start()

    print("Blast scheduler running...")


if os.getenv("BOT_DISABLE") != "1":
    start_bot_once()

start_blast_scheduler_once()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))

    flask_app.run(
        host="0.0.0.0",
        port=port
    )
