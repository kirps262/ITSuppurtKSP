import logging
import os
import re
import tempfile
import json
import zipfile
import urllib.request
import subprocess
from datetime import datetime, timedelta, timezone
import sqlite3
import psycopg2
import psycopg2.extras
import asyncio
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("REMINDERS_DB", "reminders.db")
DATABASE_URL = os.getenv("DATABASE_URL")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

BTN_LIST = "📋 Мои напоминания"
BTN_DELETE = "🗑 Удалить напоминание"
BTN_HELP = "❓ Как писать"

REPEAT_INTERVAL_SEC = 120
MAX_ATTEMPTS = 3

HELP_TEXT = (
    "Как писать:\n"
    "В 13 купить хлеб\n"
    "В 13:30 купить хлеб\n"
    "В 13 30 купить хлеб\n"
    "Напомни в 15:00 купить хлеб\n"
    "Напомни через 15 минут выключить плиту\n"
    "\n"
    "Можно голосом.\n"
    "После напоминания нажми «Прочитал», иначе бот повторит."
)

TASKS = {}
VOSK_MODEL = None
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "vosk-model-small-ru-0.22")
VOSK_MODEL_URL = os.getenv(
    "VOSK_MODEL_URL",
    "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
)

def get_conn():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    text TEXT NOT NULL,
                    run_at BIGINT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
                    awaiting_confirm BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id BIGINT PRIMARY KEY,
                    intro_sent BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            cur.execute(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
            )
            cur.execute(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS acknowledged BOOLEAN NOT NULL DEFAULT FALSE"
            )
            cur.execute(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS awaiting_confirm BOOLEAN NOT NULL DEFAULT FALSE"
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    run_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    awaiting_confirm INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    intro_sent INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()
            }
            if "attempts" not in existing_cols:
                conn.execute(
                    "ALTER TABLE reminders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "acknowledged" not in existing_cols:
                conn.execute(
                    "ALTER TABLE reminders ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0"
                )
            if "awaiting_confirm" not in existing_cols:
                conn.execute(
                    "ALTER TABLE reminders ADD COLUMN awaiting_confirm INTEGER NOT NULL DEFAULT 0"
                )
        conn.commit()
    finally:
        conn.close()

def add_reminder(chat_id: int, text: str, run_at: int) -> int:
    conn = get_conn()
    try:
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO reminders (chat_id, text, run_at) VALUES (%s, %s, %s) RETURNING id",
                (chat_id, text, run_at),
            )
            reminder_id = cur.fetchone()[0]
        else:
            cur = conn.execute(
                "INSERT INTO reminders (chat_id, text, run_at) VALUES (?, ?, ?)",
                (chat_id, text, run_at),
            )
            reminder_id = cur.lastrowid
        conn.commit()
        return reminder_id
    finally:
        conn.close()

def delete_reminder(reminder_id: int):
    conn = get_conn()
    try:
        if DATABASE_URL:
            conn.cursor().execute("DELETE FROM reminders WHERE id = %s", (reminder_id,))
        else:
            conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
    finally:
        conn.close()

def list_reminders(chat_id: int, limit: int = 10):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    conn = get_conn()
    try:
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, text, run_at
                FROM reminders
                WHERE chat_id = %s AND acknowledged = FALSE AND (run_at >= %s OR awaiting_confirm = TRUE)
                ORDER BY run_at ASC
                LIMIT %s
                """,
                (chat_id, now_ts, limit),
            )
            return cur.fetchall()
        else:
            cur = conn.execute(
                """
                SELECT id, text, run_at
                FROM reminders
                WHERE chat_id = ? AND acknowledged = 0 AND (run_at >= ? OR awaiting_confirm = 1)
                ORDER BY run_at ASC
                LIMIT ?
                """,
                (chat_id, now_ts, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()

def is_intro_sent(chat_id: int) -> bool:
    conn = get_conn()
    try:
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(
                "SELECT intro_sent FROM chat_settings WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
        else:
            cur = conn.execute(
                "SELECT intro_sent FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
        return bool(row[0]) if row else False
    finally:
        conn.close()

def set_intro_sent(chat_id: int):
    conn = get_conn()
    try:
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO chat_settings (chat_id, intro_sent)
                VALUES (%s, TRUE)
                ON CONFLICT (chat_id) DO UPDATE SET intro_sent = TRUE
                """,
                (chat_id,),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO chat_settings (chat_id, intro_sent) VALUES (?, 1)",
                (chat_id,),
            )
        conn.commit()
    finally:
        conn.close()

def get_reminder(reminder_id: int):
    conn = get_conn()
    try:
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, chat_id, text, run_at, attempts, acknowledged, awaiting_confirm
                FROM reminders
                WHERE id = %s
                """,
                (reminder_id,),
            )
            return cur.fetchone()
        cur = conn.execute(
            """
            SELECT id, chat_id, text, run_at, attempts, acknowledged, awaiting_confirm
            FROM reminders
            WHERE id = ?
            """,
            (reminder_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()

def update_reminder_state(
    reminder_id: int,
    *,
    run_at: int | None = None,
    attempts: int | None = None,
    acknowledged: bool | None = None,
    awaiting_confirm: bool | None = None,
):
    fields = []
    values = []
    if run_at is not None:
        fields.append("run_at")
        values.append(run_at)
    if attempts is not None:
        fields.append("attempts")
        values.append(attempts)
    if acknowledged is not None:
        fields.append("acknowledged")
        values.append(acknowledged)
    if awaiting_confirm is not None:
        fields.append("awaiting_confirm")
        values.append(awaiting_confirm)
    if not fields:
        return

    conn = get_conn()
    try:
        if DATABASE_URL:
            set_parts = [f"{name} = %s" for name in fields]
            cur = conn.cursor()
            cur.execute(
                f"UPDATE reminders SET {', '.join(set_parts)} WHERE id = %s",
                (*values, reminder_id),
            )
        else:
            set_parts = [f"{name} = ?" for name in fields]
            conn.execute(
                f"UPDATE reminders SET {', '.join(set_parts)} WHERE id = ?",
                (*values, reminder_id),
            )
        conn.commit()
    finally:
        conn.close()

def load_pending_reminders():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    conn = get_conn()
    try:
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id
                FROM reminders
                WHERE run_at >= %s AND acknowledged = FALSE AND awaiting_confirm = FALSE
                """,
                (now_ts,),
            )
            return cur.fetchall()
        else:
            cur = conn.execute(
                """
                SELECT id
                FROM reminders
                WHERE run_at >= ? AND acknowledged = 0 AND awaiting_confirm = 0
                """,
                (now_ts,),
            )
            return cur.fetchall()
    finally:
        conn.close()

def keyboard():
    return ReplyKeyboardMarkup([[BTN_LIST, BTN_DELETE], [BTN_HELP]], resize_keyboard=True)

def format_run_at(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(MOSCOW_TZ)
    return dt.strftime("%d.%m %H:%M")

def reminder_ack_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Прочитал", callback_data=f"ack:{reminder_id}")]]
    )

def confirm_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да", callback_data=f"confirm_yes:{reminder_id}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"confirm_no:{reminder_id}")
        ]]
    )

def schedule_reminder(app: Application, reminder_id: int):
    existing = TASKS.pop(reminder_id, None)
    if existing:
        existing.cancel()
    task = app.create_task(reminder_task(app, reminder_id))
    TASKS[reminder_id] = task

async def reminder_task(app: Application, reminder_id: int):
    try:
        while True:
            reminder = get_reminder(reminder_id)
            if not reminder:
                return

            _, chat_id, text, run_at, attempts, acknowledged, awaiting_confirm = reminder
            if acknowledged:
                delete_reminder(reminder_id)
                return
            if awaiting_confirm:
                return

            now_ts = int(datetime.now(timezone.utc).timestamp())
            delay = run_at - now_ts
            if delay > 0:
                await asyncio.sleep(delay)
                continue

            if attempts >= MAX_ATTEMPTS:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"Вы получили напоминание?\n{text}",
                    reply_markup=confirm_keyboard(reminder_id),
                )
                update_reminder_state(reminder_id, awaiting_confirm=True)
                return

            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🔔 {text}",
                reply_markup=reminder_ack_keyboard(reminder_id),
            )
            attempts += 1
            next_run = int(datetime.now(timezone.utc).timestamp()) + REPEAT_INTERVAL_SEC
            update_reminder_state(reminder_id, attempts=attempts, run_at=next_run)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logging.exception("Ошибка при отправке напоминания: %s", e)
    finally:
        TASKS.pop(reminder_id, None)

def parse_time_from_text(text: str):
    lower = text.lower()
    lower = lower.replace("ё", "е")
    tokens = re.findall(r"[a-zа-я]+|\d+", lower)
    time_match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", lower)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        if hour > 23 or minute > 59:
            return None, "❌ Неверное время."

        now = datetime.now(MOSCOW_TZ)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)

        reminder_text = re.sub(r"\b(\d{1,2})[:.](\d{2})\b", "", text).strip()
        reminder_text = re.sub(
            r"\b(сделай|поставь|создай|напомни|напоминание|на|в|мне|пожалуйста)\b",
            "",
            reminder_text,
            flags=re.IGNORECASE,
        ).strip(" ,.-")

        if not reminder_text:
            reminder_text = "Напоминание"

        run_at = int(target.astimezone(timezone.utc).timestamp())
        return (reminder_text, run_at), None

    time_match_space = re.search(r"\b(?:в|во)\s*(\d{1,2})\s+(\d{2})\b", lower)
    if time_match_space:
        hour = int(time_match_space.group(1))
        minute = int(time_match_space.group(2))
        if hour > 23 or minute > 59:
            return None, "❌ Неверное время."

        now = datetime.now(MOSCOW_TZ)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)

        reminder_text = re.sub(r"\b(?:в|во)\s*\d{1,2}\s+\d{2}\b", "", text, count=1, flags=re.IGNORECASE).strip()
        reminder_text = re.sub(
            r"\b(сделай|поставь|создай|напомни|напоминание|на|в|мне|пожалуйста)\b",
            "",
            reminder_text,
            flags=re.IGNORECASE,
        ).strip(" ,.-")

        if not reminder_text:
            reminder_text = "Напоминание"

        run_at = int(target.astimezone(timezone.utc).timestamp())
        return (reminder_text, run_at), None

    time_match_hour = re.search(r"\b(?:в|во)\s*(\d{1,2})\b", lower)
    if time_match_hour:
        hour = int(time_match_hour.group(1))
        if hour > 23:
            return None, "❌ Неверное время."

        now = datetime.now(MOSCOW_TZ)
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)

        reminder_text = re.sub(r"\b(?:в|во)\s*\d{1,2}\b", "", text, count=1, flags=re.IGNORECASE).strip()
        reminder_text = re.sub(
            r"\b(сделай|поставь|создай|напомни|напоминание|на|в|мне|пожалуйста)\b",
            "",
            reminder_text,
            flags=re.IGNORECASE,
        ).strip(" ,.-")

        if not reminder_text:
            reminder_text = "Напоминание"

        run_at = int(target.astimezone(timezone.utc).timestamp())
        return (reminder_text, run_at), None

    def parse_number(tokens, idx):
        units = {
            "ноль": 0,
            "один": 1, "одна": 1,
            "два": 2, "две": 2,
            "три": 3,
            "четыре": 4,
            "пять": 5,
            "шесть": 6,
            "семь": 7,
            "восемь": 8,
            "девять": 9,
        }
        teens = {
            "десять": 10,
            "одиннадцать": 11,
            "двенадцать": 12,
            "тринадцать": 13,
            "четырнадцать": 14,
            "пятнадцать": 15,
            "шестнадцать": 16,
            "семнадцать": 17,
            "восемнадцать": 18,
            "девятнадцать": 19,
        }
        tens = {
            "двадцать": 20,
            "тридцать": 30,
            "сорок": 40,
            "пятьдесят": 50,
        }

        if idx >= len(tokens):
            return None, idx

        token = tokens[idx]
        if token.isdigit():
            return int(token), idx + 1
        if token in teens:
            return teens[token], idx + 1
        if token in tens:
            value = tens[token]
            if idx + 1 < len(tokens) and tokens[idx + 1] in units:
                value += units[tokens[idx + 1]]
                return value, idx + 2
            return value, idx + 1
        if token in units:
            return units[token], idx + 1
        return None, idx

    def parse_spoken_time(text_value: str):
        tokens_local = re.findall(r"[a-zа-я]+", text_value)
        tokens_local = [t.replace("ё", "е") for t in tokens_local]

        if "полдень" in tokens_local:
            return 12, 0
        if "полночь" in tokens_local:
            return 0, 0

        for i, tok in enumerate(tokens_local):
            if tok not in ("в", "во"):
                continue
            hour, j = parse_number(tokens_local, i + 1)
            if hour is None:
                continue

            if j < len(tokens_local) and tokens_local[j] in ("час", "часа", "часов"):
                j += 1

            minute = None
            if j < len(tokens_local):
                minute, j2 = parse_number(tokens_local, j)
                if minute is not None:
                    j = j2

            if minute is None:
                minute = 0

            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute

        return None

    spoken_time = parse_spoken_time(lower)
    if spoken_time:
        hour, minute = spoken_time
        now = datetime.now(MOSCOW_TZ)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)

        reminder_text = text
        time_words = [
            "в", "во", "час", "часа", "часов", "полдень", "полночь",
            "ноль", "один", "одна", "два", "две", "три", "четыре", "пять",
            "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
            "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
            "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
            "двадцать", "тридцать", "сорок", "пятьдесят",
        ]
        for w in time_words:
            reminder_text = re.sub(rf"\b{w}\b", " ", reminder_text, flags=re.IGNORECASE)
        reminder_text = re.sub(r"\s{2,}", " ", reminder_text).strip()
        reminder_text = re.sub(
            r"\b(сделай|поставь|создай|напомни|напоминание|на|в|мне|пожалуйста)\b",
            "",
            reminder_text,
            flags=re.IGNORECASE,
        ).strip(" ,.-")

        if not reminder_text:
            reminder_text = "Напоминание"

        run_at = int(target.astimezone(timezone.utc).timestamp())
        return (reminder_text, run_at), None

    if "через" in tokens:
        try:
            idx = tokens.index("через")
            minutes_val, _ = parse_number(tokens, idx + 1)
        except ValueError:
            minutes_val = None
        if minutes_val is not None:
            minutes = minutes_val
            if minutes <= 0:
                return None, "❌ Количество минут должно быть больше 0"
            run_at = int(datetime.now(timezone.utc).timestamp()) + minutes * 60
            reminder_text = re.sub(r"\bчерез\b", "", text, flags=re.IGNORECASE)
            reminder_text = re.sub(r"\bмин(ут|уты|уту)?\b", "", reminder_text, flags=re.IGNORECASE)
            reminder_text = re.sub(r"\b\d+\b", "", reminder_text)
            reminder_text = re.sub(
                r"\b(ноль|один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|"
                r"одиннадцать|двенадцать|тринадцать|четырнадцать|пятнадцать|шестнадцать|"
                r"семнадцать|восемнадцать|девятнадцать|двадцать|тридцать|сорок|пятьдесят)\b",
                "",
                reminder_text,
                flags=re.IGNORECASE,
            ).strip(" ,.-")
            reminder_text = re.sub(
                r"\b(сделай|сделали|поставь|создай|напомни|напоминание|мне|пожалуйста)\b",
                "",
                reminder_text,
                flags=re.IGNORECASE,
            ).strip(" ,.-")
            if not reminder_text:
                reminder_text = "Напоминание"
            return (reminder_text, run_at), None

    minutes_match = re.search(r"\bчерез\s+(\d{1,4})\s*мин", lower)
    if minutes_match:
        minutes = int(minutes_match.group(1))
        if minutes <= 0:
            return None, "❌ Количество минут должно быть больше 0"
        run_at = int(datetime.now(timezone.utc).timestamp()) + minutes * 60
        reminder_text = re.sub(r"\bчерез\s+\d{1,4}\s*мин(ут|уты|уту)?\b", "", text, flags=re.IGNORECASE).strip(" ,.-")
        reminder_text = re.sub(
            r"\b(сделай|поставь|создай|напомни|напоминание|мне|пожалуйста)\b",
            "",
            reminder_text,
            flags=re.IGNORECASE,
        ).strip(" ,.-")
        if not reminder_text:
            reminder_text = "Напоминание"
        return (reminder_text, run_at), None

    return None, "❌ Не смог распознать время."

def ensure_vosk_model():
    if os.path.isdir(VOSK_MODEL_PATH):
        return VOSK_MODEL_PATH
    zip_path = VOSK_MODEL_PATH + ".zip"
    if not os.path.isfile(zip_path):
        urllib.request.urlretrieve(VOSK_MODEL_URL, zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(".")
    return VOSK_MODEL_PATH

def get_vosk_model():
    global VOSK_MODEL
    if VOSK_MODEL is None:
        from vosk import Model
        model_path = ensure_vosk_model()
        VOSK_MODEL = Model(model_path)
    return VOSK_MODEL

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_intro_sent(chat_id):
        await update.message.reply_text(
            "👋 Привет! Я бот-напоминалка.\nПиши текстом или голосом.",
            reply_markup=keyboard(),
        )
        set_intro_sent(chat_id)
        return
    await update.message.reply_text("✅ Я на связи.", reply_markup=keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=keyboard())

async def show_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = list_reminders(update.effective_chat.id, limit=10)
    if not items:
        await update.message.reply_text("Сейчас нет активных напоминаний.", reply_markup=keyboard())
        return
    lines = ["Ближайшие напоминания:"]
    for _id, text, run_at in items:
        lines.append(f"• {format_run_at(run_at)} — {text}")
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard())

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = list_reminders(update.effective_chat.id, limit=10)
    if not items:
        await update.message.reply_text("Нет активных напоминаний для удаления.", reply_markup=keyboard())
        return
    buttons = []
    for _id, text, run_at in items:
        label = f"{format_run_at(run_at)} — {text[:30]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"del:{_id}")])
    buttons.append([InlineKeyboardButton("Отмена", callback_data="del:cancel")])
    await update.message.reply_text(
        "Выбери напоминание для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "del:cancel":
        await query.edit_message_text("Удаление отменено.")
        return
    if data.startswith("del:"):
        reminder_id = int(data.split(":", 1)[1])
        task = TASKS.pop(reminder_id, None)
        if task:
            task.cancel()
        delete_reminder(reminder_id)
        await query.edit_message_text("✅ Напоминание удалено.")
        return
    if data.startswith("ack:"):
        reminder_id = int(data.split(":", 1)[1])
        task = TASKS.pop(reminder_id, None)
        if task:
            task.cancel()
        delete_reminder(reminder_id)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if data.startswith("confirm_yes:"):
        reminder_id = int(data.split(":", 1)[1])
        task = TASKS.pop(reminder_id, None)
        if task:
            task.cancel()
        delete_reminder(reminder_id)
        await query.edit_message_text("✅ Принято.")
        return
    if data.startswith("confirm_no:"):
        reminder_id = int(data.split(":", 1)[1])
        update_reminder_state(
            reminder_id,
            attempts=0,
            awaiting_confirm=False,
            acknowledged=False,
            run_at=int(datetime.now(timezone.utc).timestamp()) + REPEAT_INTERVAL_SEC,
        )
        schedule_reminder(context.application, reminder_id)
        await query.edit_message_text("⏰ Ок, напомню еще раз через 2 минуты.")
        return

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        voice = update.message.voice
        if not voice:
            return
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            ogg_path = tmp.name
        await file.download_to_drive(ogg_path)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = tmp_wav.name

        subprocess.run(
            ["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        model = get_vosk_model()
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(model, 16000)
        with open(wav_path, "rb") as f:
            while True:
                data = f.read(4000)
                if len(data) == 0:
                    break
                rec.AcceptWaveform(data)
        result = json.loads(rec.FinalResult())
        text = (result.get("text") or "").strip()
        if not text:
            await update.message.reply_text("❌ Не удалось распознать голос.", reply_markup=keyboard())
            return

        parsed, error = parse_time_from_text(text)
        if error:
            await update.message.reply_text(f"{error}\nЯ услышал: \"{text}\"", reply_markup=keyboard())
            return

        reminder_text, run_at = parsed
        reminder_id = add_reminder(update.effective_chat.id, reminder_text, run_at)
        schedule_reminder(context.application, reminder_id)
        await update.message.reply_text(
            f"⏰ {format_run_at(run_at)} — {reminder_text}",
            reply_markup=keyboard(),
        )
    except Exception as e:
        logging.exception("Ошибка обработки голосового сообщения: %s", e)
        await update.message.reply_text("❌ Ошибка при обработке голосового.", reply_markup=keyboard())
    finally:
        try:
            if 'ogg_path' in locals() and os.path.exists(ogg_path):
                os.remove(ogg_path)
            if 'wav_path' in locals() and os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text
        if text == BTN_LIST:
            await show_reminders(update, context)
            return
        if text == BTN_DELETE:
            await delete_menu(update, context)
            return
        if text == BTN_HELP:
            await help_command(update, context)
            return

        parsed, error = parse_time_from_text(text)
        if parsed:
            reminder_text, run_at = parsed
            reminder_id = add_reminder(update.effective_chat.id, reminder_text, run_at)
            schedule_reminder(context.application, reminder_id)
            await update.message.reply_text(
                f"⏰ {format_run_at(run_at)} — {reminder_text}",
                reply_markup=keyboard(),
            )
            return

        await update.message.reply_text(error, reply_markup=keyboard())
        return

    except ValueError:
        await update.message.reply_text("❌ Неверный формат.", reply_markup=keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}", reply_markup=keyboard())

async def on_startup(app: Application):
    for (reminder_id,) in load_pending_reminders():
        schedule_reminder(app, reminder_id)

def main():
    token = os.getenv('BOT_TOKEN')
    if not token:
        print("Ошибка: не найден BOT_TOKEN")
        return

    init_db()

    app = Application.builder().token(token).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("Бот запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
