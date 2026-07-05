import os
import threading
import asyncio
import psycopg2
import cloudinary
import cloudinary.uploader
import re
import secrets
import string

from urllib.request import urlopen

from flask import Flask, render_template, request, redirect, session

from telegram import (
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
# FLASK APP
# =========================
flask_app = Flask(__name__)
flask_app.secret_key = SECRET_KEY


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
        "register_url": "https://yourwebsite.com",
        "telegram_support": "https://t.me/your_support",
        "whatsapp_url": "https://wa.me/60139661818",

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


def get_users_paginated(search=None, page=1, per_page=50):
    offset = (page - 1) * per_page

    conn = get_db_connection()
    cur = conn.cursor()

    if search:
        cur.execute("""
            SELECT COUNT(*)
            FROM users
            WHERE CAST(telegram_id AS TEXT) ILIKE %s
               OR username ILIKE %s
               OR referral_code ILIKE %s
        """, (f"%{search}%", f"%{search}%", f"%{search}%"))
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT telegram_id,
                   username,
                   referral_code,
                   referred_by,
                   TO_CHAR(first_seen, 'DD Mon YYYY, HH12:MI AM') AS first_seen_fmt
            FROM users
            WHERE CAST(telegram_id AS TEXT) ILIKE %s
               OR username ILIKE %s
               OR referral_code ILIKE %s
            ORDER BY first_seen DESC
            LIMIT %s OFFSET %s
        """, (f"%{search}%", f"%{search}%", f"%{search}%", per_page, offset))
        rows = cur.fetchall()

    else:
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT telegram_id,
                   username,
                   referral_code,
                   referred_by,
                   TO_CHAR(first_seen, 'DD Mon YYYY, HH12:MI AM') AS first_seen_fmt
            FROM users
            ORDER BY first_seen DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        rows = cur.fetchall()

    cur.close()
    conn.close()

    total_pages = (total + per_page - 1) // per_page

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


def add_promo_button(promo_id, text, url):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM promo_buttons WHERE promo_id=%s", (promo_id,))
    max_sort = cur.fetchone()[0] + 1

    cur.execute("""
        INSERT INTO promo_buttons (promo_id, text, url, sort_order)
        VALUES (%s, %s, %s, %s)
    """, (promo_id, text, url, max_sort))

    conn.commit()
    cur.close()
    conn.close()


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
        await update.message.reply_text(about_text, parse_mode="HTML")
        return

    if msg == "📞 Contact":
        telegram_support = get_setting("telegram_support")
        whatsapp_url = get_setting("whatsapp_url")

        keyboard = [
            [InlineKeyboardButton("💬 Telegram", url=telegram_support)],
            [InlineKeyboardButton("💬 WhatsApp", url=whatsapp_url)]
        ]

        await update.message.reply_text(
            "📞 Contact Us",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if msg == "🚀 Register":
        register_url = get_setting("register_url")
        keyboard = [[InlineKeyboardButton("🌍 Register", url=register_url)]]

        await update.message.reply_text(
            "🚀 Register Now",
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
# ADMIN AUTH
# =========================
def require_login():
    return session.get("admin_logged_in")


@flask_app.route("/")
def home():
    return redirect("/admin")


@flask_app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if username == ADMIN_USER and password == ADMIN_PASS:
            session["admin_logged_in"] = True
            return redirect("/admin")

        return render_template("login.html", error="Invalid username or password")

    return render_template("login.html")


@flask_app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


# =========================
# DASHBOARD SETTINGS
# =========================
@flask_app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    if not require_login():
        return redirect("/admin/login")

    if request.method == "POST":
        main_banner = request.form.get("main_banner")
        if main_banner:
            set_setting("main_banner", main_banner)

        set_setting("welcome_text", request.form.get("welcome_text", ""))
        set_setting("about_text", request.form.get("about_text", ""))
        set_setting("register_url", request.form.get("register_url", ""))
        set_setting("telegram_support", request.form.get("telegram_support", ""))
        set_setting("whatsapp_url", request.form.get("whatsapp_url", ""))

        set_setting("manual_today_add", request.form.get("manual_today_add", "0"))
        set_setting("manual_month_add", request.form.get("manual_month_add", "0"))

        # referral admin settings
        set_setting("referral_enabled", request.form.get("referral_enabled", "0"))
        set_setting("referral_image", request.form.get("referral_image", ""))
        set_setting("referral_text", request.form.get("referral_text", ""))

        return redirect("/admin")

    data = {
        "main_banner": get_setting("main_banner"),
        "welcome_text": get_setting("welcome_text"),
        "about_text": get_setting("about_text"),
        "register_url": get_setting("register_url"),
        "telegram_support": get_setting("telegram_support"),
        "whatsapp_url": get_setting("whatsapp_url"),
        "manual_today_add": get_setting("manual_today_add"),
        "manual_month_add": get_setting("manual_month_add"),

        "referral_enabled": get_setting("referral_enabled"),
        "referral_text": get_setting("referral_text")
    }

    return render_template("dashboard.html", data=data)


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

    promos = get_promos(active_only=False)

    base_layout = get_setting("base_menu_layout")
    promo_layout = get_setting("promo_menu_layout")

    return render_template(
        "promos.html",
        promos=promos,
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

    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", 1))
    per_page = 50

    total_users = get_total_users()
    today = get_today_count()
    month = get_month_count()

    users, total_filtered, total_pages = get_users_paginated(
        search=q if q else None,
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
        total_pages=total_pages,
        total_filtered=total_filtered
    )


# =========================
# PROMO EDIT + BUTTONS
# =========================
@flask_app.route("/admin/promo/<int:promo_id>", methods=["GET", "POST"])
def admin_edit_promo(promo_id):
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        title = request.form.get("title", "")
        image_url = request.form.get("image_url", "")
        caption = request.form.get("caption", "")

        cur.execute("""
            UPDATE promos SET title=%s, image_url=%s, caption=%s
            WHERE id=%s
        """, (title, image_url, caption, promo_id))
        conn.commit()

        cur.close()
        conn.close()
        return redirect(f"/admin/promo/{promo_id}")

    cur.execute("SELECT id, title, image_url, caption FROM promos WHERE id=%s", (promo_id,))
    promo = cur.fetchone()

    cur.execute("""
        SELECT id, text, url, sort_order
        FROM promo_buttons
        WHERE promo_id=%s
        ORDER BY sort_order ASC
    """, (promo_id,))
    buttons = cur.fetchall()

    cur.close()
    conn.close()

    return render_template("promo_edit.html", promo=promo, buttons=buttons)


@flask_app.route("/admin/promo/<int:promo_id>/button/add", methods=["POST"])
def admin_add_promo_button_route(promo_id):
    if not require_login():
        return redirect("/admin/login")

    text_btn = request.form.get("text", "").strip()
    url = request.form.get("url", "").strip()

    if not text_btn or not url:
        return redirect(f"/admin/promo/{promo_id}")

    add_promo_button(promo_id, text_btn, url)
    return redirect(f"/admin/promo/{promo_id}")


@flask_app.route("/admin/promo/button/delete/<int:button_id>/<int:promo_id>")
def admin_delete_promo_button(button_id, promo_id):
    if not require_login():
        return redirect("/admin/login")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM promo_buttons WHERE id=%s", (button_id,))
    conn.commit()
    cur.close()
    conn.close()

    return redirect(f"/admin/promo/{promo_id}")


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
@flask_app.route("/admin/upload_banner", methods=["POST"])
def upload_banner():
    if not require_login():
        return redirect("/admin/login")

    try:
        file = request.files.get("image")

        if not file:
            return "No file uploaded", 400

        url = upload_to_cloudinary(file)

        return render_template("dashboard.html",
            data={
                "main_banner": get_setting("main_banner"),
                "welcome_text": get_setting("welcome_text"),
                "about_text": get_setting("about_text"),
                "register_url": get_setting("register_url"),
                "telegram_support": get_setting("telegram_support"),
                "whatsapp_url": get_setting("whatsapp_url"),
                "manual_today_add": get_setting("manual_today_add"),
                "manual_month_add": get_setting("manual_month_add"),
                "referral_enabled": get_setting("referral_enabled"),
                "referral_text": get_setting("referral_text")
            },
            uploaded_url=url
        )

    except Exception as e:
        print("UPLOAD ERROR:", e)
        return f"Internal Error: {str(e)}", 500


# =========================
# START SYSTEM
# =========================
init_db()
clear_webhook()

BOT_STARTED = False

def start_bot_once():
    global BOT_STARTED
    if BOT_STARTED:
        return
    BOT_STARTED = True

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()


if os.getenv("BOT_DISABLE") != "1":
    start_bot_once()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
