import logging
import os
import re
import tempfile
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
WHISPER_MODEL = None

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
    task = asyncio.create_task(reminder_task(app, reminder_id, chat_id, text, run_at))
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
            return None, "❌ Не понял текст напоминания. Пример: напомни в 15:00 купить хлеб"

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
            return None, "❌ Не понял текст напоминания. Пример: напомни через 15 минут купить хлеб"
        return (reminder_text, run_at), None

    return None, "❌ Не смог распознать время. Скажи, например: напомни в 15:00 купить хлеб"

def get_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        import whisper
        WHISPER_MODEL = whisper.load_model("base")
    return WHISPER_MODEL

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '👋 Привет! Я бот-напоминалка.\n\n'
        'Отправь мне сообщение в формате:\n'
        '<текст напоминания> | <минуты>\n\n'
        'Например: Выключить плиту | 15'
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
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        model = get_whisper_model()
        result = await asyncio.to_thread(
            model.transcribe,
            tmp_path,
            language="ru",
            fp16=False,
        )
        text = (result.get("text") or "").strip()
        if not text:
            await update.message.reply_text("❌ Не удалось распознать голос. Попробуй еще раз.", reply_markup=keyboard())
            return

        parsed, error = parse_time_from_text(text)
        if error:
            await update.message.reply_text(error, reply_markup=keyboard())
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
        await update.message.reply_text("❌ Ошибка при обработке голосового.", reply_markup=keyboard())

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text
        if text == BTN_LIST:
            await show_reminders(update, context)
            return
        if text == BTN_DELETE:
            await delete_menu(update, context)
            return

        if '|' not in text:
            await update.message.reply_text('❌ Используй формат: текст | минуты\nНапример: Позвонить маме | 30')
            return

        reminder_text, minutes_str = text.split('|', 1)
        reminder_text = reminder_text.strip()
        minutes = int(minutes_str.strip())

        if minutes <= 0:
            await update.message.reply_text('❌ Количество минут должно быть больше 0')
            return

        run_at = int(datetime.now(timezone.utc).timestamp()) + minutes * 60
        reminder_id = add_reminder(update.effective_chat.id, reminder_text, run_at)

        await update.message.reply_text(f'⏰ Напомню через {minutes} мин: "{reminder_text}"', reply_markup=keyboard())

        schedule_reminder(context.application, reminder_id, update.effective_chat.id, reminder_text, run_at)

    except ValueError:
        await update.message.reply_text('❌ Неверный формат. Пример: Выключить духовку | 20')
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {str(e)}')

def main():
    token = os.getenv('BOT_TOKEN')
    if not token:
        print("Ошибка: не найден BOT_TOKEN")
        return

    init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(on_delete_callback))

    print("Бот запущен...")
    for reminder_id, chat_id, text, run_at in load_pending_reminders():
        schedule_reminder(app, reminder_id, chat_id, text, run_at)
    app.run_polling()

if __name__ == '__main__':
    main()
