import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import asyncio

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '👋 Привет! Я бот-напоминалка.\n\n'
        'Отправь мне сообщение в формате:\n'
        '<текст напоминания> | <минуты>\n\n'
        'Например: Выключить плиту | 15'
    )

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text

        if '|' not in text:
            await update.message.reply_text('❌ Используй формат: текст | минуты\nНапример: Позвонить маме | 30')
            return

        reminder_text, minutes_str = text.split('|', 1)
        reminder_text = reminder_text.strip()
        minutes = int(minutes_str.strip())

        if minutes <= 0:
            await update.message.reply_text('❌ Количество минут должно быть больше 0')
            return

        await update.message.reply_text(f'⏰ Напомню через {minutes} мин: "{reminder_text}"')

        await asyncio.sleep(minutes * 60)

        await update.message.reply_text(f'🔔 Напоминание:\n{reminder_text}')

    except ValueError:
        await update.message.reply_text('❌ Неверный формат. Пример: Выключить духовку | 20')
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {str(e)}')

def main():
    token = os.getenv('BOT_TOKEN')
    if not token:
        print("Ошибка: не найден BOT_TOKEN")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))

    print("Бот запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
