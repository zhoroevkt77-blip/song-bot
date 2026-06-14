import os
import asyncio
import sqlite3
import logging
import httpx
import json
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── КОНФИГ ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")
REPLICATE_KEY    = os.getenv("REPLICATE_API_TOKEN", "YOUR_REPLICATE_KEY")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "123456789"))
SONG_PRICE       = int(os.getenv("SONG_PRICE", "150"))   # сом
PROVIDER_TOKEN   = os.getenv("PAYMENT_PROVIDER_TOKEN", "")  # Telegram Payments

bot = Bot(token=TELEGRAM_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ─── БАЗА ДАННЫХ ──────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("songs.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            username    TEXT,
            theme       TEXT,
            style       TEXT,
            lyrics      TEXT,
            audio_url   TEXT,
            audio_file_id TEXT,
            status      TEXT DEFAULT 'pending',
            paid        INTEGER DEFAULT 0,
            price       INTEGER,
            created_at  TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            songs_bought INTEGER DEFAULT 0,
            joined_at   TEXT
        )
    """)
    conn.commit()
    conn.close()

def db_save_order(user_id, username, theme, style, price):
    conn = sqlite3.connect("songs.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO orders (user_id, username, theme, style, price, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, username, theme, style, price, datetime.now().isoformat()))
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return order_id

def db_update_order(order_id, **kwargs):
    conn = sqlite3.connect("songs.db")
    c = conn.cursor()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [order_id]
    c.execute(f"UPDATE orders SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()

def db_get_order(order_id):
    conn = sqlite3.connect("songs.db")
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    row = c.fetchone()
    conn.close()
    if row:
        cols = ["id","user_id","username","theme","style","lyrics",
                "audio_url","audio_file_id","status","paid","price","created_at"]
        return dict(zip(cols, row))
    return None

def db_register_user(user_id, username, full_name):
    conn = sqlite3.connect("songs.db")
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO users (user_id, username, full_name, joined_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, username, full_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def db_stats():
    conn = sqlite3.connect("songs.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders WHERE paid=1")
    sold = c.fetchone()[0]
    c.execute("SELECT SUM(price) FROM orders WHERE paid=1")
    revenue = c.fetchone()[0] or 0
    conn.close()
    return users, sold, revenue

# ─── СОСТОЯНИЯ ────────────────────────────────────────────────────────────────
class OrderSong(StatesGroup):
    waiting_theme  = State()
    waiting_style  = State()
    confirming     = State()

# ─── AI: ЫР ЖАЗУУ (Claude) ───────────────────────────────────────────────────
async def generate_lyrics(theme: str, style: str) -> str:
    prompt = f"""Кыргыз тилинде {style} стилинде ыр жаз.
Темасы: {theme}

Талаптар:
- 2-3 куплет + кайтарым (припев)
- Кыргызча жана жүрөккө жакын сөздөр
- Ыр ритмине ылайыктуу болсун
- Эмоционалдуу, жандуу тил

Форматы:
[Куплет 1]
...

[Кайтарым]
...

[Куплет 2]
...

[Кайтарым]
...

Ырды гана жаз, башка эч нерсе жок."""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            }
        )
        data = resp.json()
        return data["content"][0]["text"]

# ─── AI: ОБОН (Replicate - MusicGen) ─────────────────────────────────────────
async def generate_music(lyrics: str, style: str) -> str:
    """Replicate аркылуу музыка жаратат, MP3 URL кайтарат"""
    style_map = {
        "Лирикалык":  "kyrgyz folk ballad, emotional, acoustic guitar, soft",
        "Эстрада":    "central asian pop music, upbeat, modern production",
        "Элдик":      "kyrgyz traditional folk, komuz instrument, nomadic",
        "Рэп":        "central asian hip hop, modern beat, urban",
        "Романтикалык": "romantic ballad, piano, strings, emotional vocals",
    }
    prompt_music = style_map.get(style, "kyrgyz music, melodic, modern")
    # Куплеттин биринчи 200 символун кошобуз
    short_lyrics = lyrics[:200].replace('\n', ' ')
    full_prompt = f"{prompt_music}. Lyrics theme: {short_lyrics}"

    async with httpx.AsyncClient(timeout=120) as client:
        # Prediction баштоо
        resp = await client.post(
            "https://api.replicate.com/v1/predictions",
            headers={
                "Authorization": f"Token {REPLICATE_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "version": "671ac645ce5e552cc63a54a2bbff63fcf798043055d2dac5fc9e36a837eedcfb",
                "input": {
                    "prompt": full_prompt,
                    "model_version": "stereo-large",
                    "output_format": "mp3",
                    "duration": 30
                }
            }
        )
        pred = resp.json()
        pred_id = pred.get("id")
        if not pred_id:
            raise Exception(f"Replicate error: {pred}")

        # Аяктаганча күтөбүз
        for _ in range(60):
            await asyncio.sleep(3)
            check = await client.get(
                f"https://api.replicate.com/v1/predictions/{pred_id}",
                headers={"Authorization": f"Token {REPLICATE_KEY}"}
            )
            result = check.json()
            if result["status"] == "succeeded":
                output = result.get("output")
                if isinstance(output, list):
                    return output[0]
                return output
            elif result["status"] == "failed":
                raise Exception(f"Music gen failed: {result.get('error')}")

    raise Exception("Музыка убакытынан өтүп кетти (timeout)")

# ─── КЛАВИАТУРАЛАР ────────────────────────────────────────────────────────────
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Ыр жасат", callback_data="new_song")],
        [InlineKeyboardButton(text="📋 Менин заказдарым", callback_data="my_orders")],
        [InlineKeyboardButton(text="ℹ️ Бот жөнүндө", callback_data="about")],
    ])

def style_keyboard():
    styles = ["Лирикалык", "Эстрада", "Элдик", "Рэп", "Романтикалык"]
    buttons = [[InlineKeyboardButton(text=s, callback_data=f"style_{s}")] for s in styles]
    buttons.append([InlineKeyboardButton(text="🔙 Артка", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_keyboard(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Төлөй ({SONG_PRICE} сом)", callback_data=f"pay_{order_id}")],
        [InlineKeyboardButton(text="❌ Жок, башка тема", callback_data="new_song")],
    ])

def admin_keyboard(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Төлөм расталды", callback_data=f"admin_confirm_{order_id}")],
        [InlineKeyboardButton(text="❌ Жокко чыгаруу", callback_data=f"admin_cancel_{order_id}")],
    ])

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    db_register_user(msg.from_user.id, msg.from_user.username, msg.from_user.full_name)
    await msg.answer(
        f"🎶 *Ыр Устасы* ботуна кош келдиңиз!\n\n"
        f"Мен сиз үчүн AI аркылуу:\n"
        f"✍️ Кыргызча ыр жазам\n"
        f"🎵 Обонго салам\n"
        f"📥 MP3 жиберем\n\n"
        f"Баасы: *{SONG_PRICE} сом* бир ыр",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    users, sold, revenue = db_stats()
    await msg.answer(
        f"📊 *Администратор панели*\n\n"
        f"👥 Колдонуучулар: {users}\n"
        f"🎵 Сатылган ырлар: {sold}\n"
        f"💰 Кирешe: {revenue} сом",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "new_song")
async def cb_new_song(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OrderSong.waiting_theme)
    await cb.message.edit_text(
        "✍️ *Ырдын темасын жазыңыз:*\n\n"
        "Мисалы: Апам жөнүндө, Ала-Тоо сулуулугу, Биринчи сүйүү...",
        parse_mode="Markdown"
    )

@dp.message(OrderSong.waiting_theme)
async def handle_theme(msg: Message, state: FSMContext):
    await state.update_data(theme=msg.text)
    await state.set_state(OrderSong.waiting_style)
    await msg.answer(
        "🎸 *Стилди тандаңыз:*",
        parse_mode="Markdown",
        reply_markup=style_keyboard()
    )

@dp.callback_query(F.data.startswith("style_"), OrderSong.waiting_style)
async def handle_style(cb: CallbackQuery, state: FSMContext):
    style = cb.data.replace("style_", "")
    data  = await state.get_data()
    theme = data["theme"]

    # Заказды сакта
    order_id = db_save_order(
        cb.from_user.id,
        cb.from_user.username,
        theme, style, SONG_PRICE
    )
    await state.update_data(order_id=order_id)
    await state.set_state(OrderSong.confirming)

    wait_msg = await cb.message.edit_text(
        "⏳ *ЫР ЖАЗЫЛЫП ЖАТАТ...*\n\nClaude AI ыр жазып берет, бир аз күтүңүз...",
        parse_mode="Markdown"
    )

    try:
        # 1) Ыр тексти жазуу
        lyrics = await generate_lyrics(theme, style)
        db_update_order(order_id, lyrics=lyrics, status="lyrics_ready")

        await cb.message.answer(
            f"✅ *Ырыңыз даяр!*\n\n"
            f"📝 *Тема:* {theme}\n"
            f"🎸 *Стиль:* {style}\n\n"
            f"{'─'*30}\n{lyrics}\n{'─'*30}\n\n"
            f"💰 Баасы: *{SONG_PRICE} сом*\n"
            f"Төлөп, обонго салдыруу үчүн төмөндөгү баскычты басыңыз:",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard(order_id)
        )
    except Exception as e:
        logger.error(f"Lyrics error: {e}")
        await cb.message.answer("❌ Ката чыкты, кайра аракет кылыңыз.")

@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(cb: CallbackQuery):
    order_id = int(cb.data.replace("pay_", ""))
    order    = db_get_order(order_id)
    if not order:
        await cb.answer("Заказ табылган жок!")
        return

    if PROVIDER_TOKEN:
        # Реалдуу Telegram Payments
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title=f"🎵 Ыр: {order['theme'][:30]}",
            description=f"{order['style']} стилиндеги жеке ырыңыз",
            payload=f"song_{order_id}",
            provider_token=PROVIDER_TOKEN,
            currency="KGS",
            prices=[LabeledPrice(label="Ыр MP3", amount=order['price'] * 100)],
        )
    else:
        # Manually (admin тастыктайт)
        await cb.message.answer(
            f"💳 *Төлөм маалыматы:*\n\n"
            f"Сумма: *{SONG_PRICE} сом*\n"
            f"Заказ №: `{order_id}`\n\n"
            f"Которуңуз:\n"
            f"📱 Mbank: `+996 700 000 000`\n"
            f"📱 O!Money: `+996 700 000 000`\n\n"
            f"Скриншотту @admin_username га жиберип, заказ номериңизди айтыңыз.",
            parse_mode="Markdown"
        )
        # Adminге билдирүү
        await bot.send_message(
            ADMIN_ID,
            f"🔔 *ЖАҢЫ ЗАКАЗ #{order_id}*\n\n"
            f"👤 Колдонуучу: @{order.get('username', 'N/A')}\n"
            f"📝 Тема: {order['theme']}\n"
            f"🎸 Стиль: {order['style']}\n"
            f"💰 Сумма: {order['price']} сом",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(order_id)
        )

@dp.callback_query(F.data.startswith("admin_confirm_"))
async def admin_confirm(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return
    order_id = int(cb.data.replace("admin_confirm_", ""))
    order    = db_get_order(order_id)
    if not order:
        await cb.answer("Заказ жок!")
        return

    db_update_order(order_id, paid=1, status="generating_music")
    await cb.message.edit_text(f"✅ Заказ #{order_id} тасталды. Музыка жаратылып жатат...")

    try:
        # Колдонуучуга күтүүнү айт
        await bot.send_message(
            order["user_id"],
            "✅ *Төлөмүңүз тасталды!*\n\n"
            "🎵 Обон жаратылып жатат... (1-2 мүнөт)",
            parse_mode="Markdown"
        )

        # Музыка жасат
        audio_url = await generate_music(order["lyrics"], order["style"])
        db_update_order(order_id, audio_url=audio_url, status="done")

        # MP3 жүктөп колдонуучуга жибер
        async with httpx.AsyncClient(timeout=60) as client:
            audio_resp = await client.get(audio_url)
            audio_bytes = audio_resp.content

        sent = await bot.send_audio(
            order["user_id"],
            audio=("song.mp3", audio_bytes, "audio/mpeg"),
            caption=(
                f"🎵 *Сиздин ырыңыз даяр!*\n\n"
                f"📝 Тема: {order['theme']}\n"
                f"🎸 Стиль: {order['style']}\n\n"
                f"Ырды колдонуу үчүн рахмат! 🙏"
            ),
            parse_mode="Markdown"
        )
        db_update_order(order_id, audio_file_id=sent.audio.file_id)
        await cb.message.edit_text(f"✅ Заказ #{order_id} аяктады. MP3 жиберилди!")

    except Exception as e:
        logger.error(f"Music gen error: {e}")
        await bot.send_message(
            order["user_id"],
            "❌ Обон жаратуу катасы чыкты. Кечиресиз, кайра байланышабыз."
        )
        db_update_order(order_id, status="error")
        await cb.message.edit_text(f"❌ Заказ #{order_id}: ката — {e}")

@dp.callback_query(F.data.startswith("admin_cancel_"))
async def admin_cancel(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return
    order_id = int(cb.data.replace("admin_cancel_", ""))
    order    = db_get_order(order_id)
    db_update_order(order_id, status="cancelled")
    await bot.send_message(order["user_id"], "❌ Сиздин заказыңыз жокко чыгарылды.")
    await cb.message.edit_text(f"❌ Заказ #{order_id} жокко чыгарылды.")

# Telegram Payments
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(msg: Message):
    payload  = msg.successful_payment.invoice_payload
    order_id = int(payload.replace("song_", ""))
    order    = db_get_order(order_id)
    db_update_order(order_id, paid=1, status="generating_music")

    await msg.answer("✅ Төлөм кабыл алынды! Обон жаратылып жатат...")
    # Music generation (жогорудагы логика)
    try:
        audio_url = await generate_music(order["lyrics"], order["style"])
        async with httpx.AsyncClient(timeout=60) as client:
            audio_bytes = (await client.get(audio_url)).content
        await bot.send_audio(
            msg.from_user.id,
            audio=("song.mp3", audio_bytes, "audio/mpeg"),
            caption=f"🎵 *Сиздин ырыңыз!*\n📝 {order['theme']}",
            parse_mode="Markdown"
        )
        db_update_order(order_id, audio_url=audio_url, status="done")
    except Exception as e:
        await msg.answer("❌ Ката чыкты, adminге кабарлаңыз.")

@dp.callback_query(F.data == "my_orders")
async def cb_my_orders(cb: CallbackQuery):
    conn = sqlite3.connect("songs.db")
    c    = conn.cursor()
    c.execute("""
        SELECT id, theme, style, status, paid, created_at
        FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5
    """, (cb.from_user.id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await cb.message.edit_text("📋 Сизде азырынча заказ жок.", reply_markup=main_keyboard())
        return

    status_map = {
        "pending": "⏳ Күтүүдө",
        "lyrics_ready": "✍️ Ыр даяр",
        "generating_music": "🎵 Обон жасалып жатат",
        "done": "✅ Аяктады",
        "cancelled": "❌ Жокко чыгарылды",
        "error": "⚠️ Ката"
    }
    text = "📋 *Менин заказдарым:*\n\n"
    for row in rows:
        id_, theme, style, status, paid, created = row
        paid_icon = "💳" if paid else "⏳"
        text += f"{paid_icon} №{id_}: *{theme[:20]}*\n{style} • {status_map.get(status, status)}\n\n"

    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

@dp.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    await cb.message.edit_text(
        "ℹ️ *Ыр Устасы Боту жөнүндө*\n\n"
        "🤖 AI: Claude (Anthropic)\n"
        "🎵 Музыка: Replicate MusicGen\n\n"
        "Сиз тема бересиз — биз:\n"
        "1️⃣ Кыргызча ыр жазабыз\n"
        "2️⃣ Обонго саламыз\n"
        "3️⃣ MP3 жиберебиз\n\n"
        f"💰 Баасы: {SONG_PRICE} сом",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

@dp.callback_query(F.data == "back_main")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "🎶 Башкы меню:", reply_markup=main_keyboard()
    )

# ─── СТАРТ ────────────────────────────────────────────────────────────────────
async def main():
    init_db()
    logger.info("Бот иштеп баштады!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
