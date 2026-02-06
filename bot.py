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
import asyncio
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("REMINDERS_DB", "reminders.db")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

BTN_LIST = "📋 Мои напоминания"
BTN_DELETE = "🗑 Удалить напоминание"

TASKS = {}
VOSK_MODEL = None
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "vosk-model-small-ru-0.22")
VOSK_MODEL_URL = os.getenv(
    "VOSK_MODEL_URL",
    "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                run_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()

def add_reminder(chat_id: int, text: str, run_at: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO reminders (chat_id, text, run_at) VALUES (?, ?, ?)",
            (chat_id, text, run_at),
        )
        conn.commit()
        return cur.lastrowid

def delete_reminder(reminder_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()

def list_reminders(chat_id: int, limit: int = 10):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, text, run_at FROM reminders WHERE chat_id = ? AND run_at >= ? ORDER BY run_at ASC LIMIT ?",
            (chat_id, now_ts, limit),
        )
        return cur.fetchall()

def load_pending_reminders():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, chat_id, text, run_at FROM reminders WHERE run_at >= ?",
            (now_ts,),
        )
        return cur.fetchall()

def keyboard():
    return ReplyKeyboardMarkup([[BTN_LIST, BTN_DELETE]], resize_keyboard=True)

def format_run_at(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(MOSCOW_TZ)
    return dt.strftime("%d.%m %H:%M")

def schedule_reminder(app: Application, reminder_id: int, chat_id: int, text: str, run_at: int):
    task = app.create_task(reminder_task(app, reminder_id, chat_id, text, run_at))
    TASKS[reminder_id] = task

async def reminder_task(app: Application, reminder_id: int, chat_id: int, text: str, run_at: int):
    try:
        delay = run_at - int(datetime.now(timezone.utc).timestamp())
        if delay > 0:
            await asyncio.sleep(delay)
        await app.bot.send_message(chat_id=chat_id, text=f"🔔 Напоминание:\n{text}")
    except asyncio.CancelledError:
        return
    except Exception as e:
        logging.exception("Ошибка при отправке напоминания: %s", e)
    finally:
        TASKS.pop(reminder_id, None)
        delete_reminder(reminder_id)

def parse_time_from_text(text: str):
    lower = text.lower()
    time_match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", lower)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        if hour > 23 or minute > 59:
            return None, "❌ Неверное время. Пример: 15:00"

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

    return None, "❌ Не смог распознать время. Скажи, например: напомни в 15:00 купить хлеб"

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
    await update.message.reply_text(
        '👋 Привет! Я бот-напоминалка.\n\n'
        'Напиши сообщение в свободной форме, например:\n'
        'Напомни в 15:00 купить хлеб\n'
        'или: Напомни через 15 минут выключить плиту'
        , reply_markup=keyboard()
    )

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
    await update.message.reply_text("Выбери напоминание для удаления:", reply_markup=InlineKeyboardMarkup(buttons))

async def on_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await query.edit_message_text("Напоминание удалено.")

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
            await update.message.reply_text("❌ Не удалось распознать голос. Попробуй еще раз.", reply_markup=keyboard())
            return

        parsed, error = parse_time_from_text(text)
        if error:
            await update.message.reply_text(f"{error}\nЯ услышал: \"{text}\"", reply_markup=keyboard())
            return

        reminder_text, run_at = parsed
        reminder_id = add_reminder(update.effective_chat.id, reminder_text, run_at)
        schedule_reminder(context.application, reminder_id, update.effective_chat.id, reminder_text, run_at)
        await update.message.reply_text(
            f'⏰ Напомню в {format_run_at(run_at)}: "{reminder_text}"',
            reply_markup=keyboard()
        )
    except Exception as e:
        logging.exception("Ошибка обработки голосового сообщения: %s", e)
        await update.message.reply_text(
            "❌ Ошибка при обработке голосового. Проверь, что в Railway задано APT_PACKAGES=ffmpeg.",
            reply_markup=keyboard(),
        )
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

        parsed, error = parse_time_from_text(text)
        if parsed:
            reminder_text, run_at = parsed
            reminder_id = add_reminder(update.effective_chat.id, reminder_text, run_at)
            await update.message.reply_text(
                f'⏰ Напомню в {format_run_at(run_at)}: "{reminder_text}"',
                reply_markup=keyboard()
            )
            schedule_reminder(context.application, reminder_id, update.effective_chat.id, reminder_text, run_at)
            return

        await update.message.reply_text(
            '❌ Напиши так:\n'
            'Напомни в 15:00 купить хлеб\n'
            'или: Напомни через 15 минут выключить плиту',
            reply_markup=keyboard()
        )
        return

    except ValueError:
        await update.message.reply_text('❌ Неверный формат. Пример: Напомни в 15:00 купить хлеб')
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {str(e)}')

async def on_startup(app: Application):
    for reminder_id, chat_id, text, run_at in load_pending_reminders():
        schedule_reminder(app, reminder_id, chat_id, text, run_at)

def main():
    token = os.getenv('BOT_TOKEN')
    if not token:
        print("Ошибка: не найден BOT_TOKEN")
        return

    init_db()

    app = Application.builder().token(token).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(on_delete_callback))

    print("Бот запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
