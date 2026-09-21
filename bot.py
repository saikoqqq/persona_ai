import os
import re
import asyncio
import logging
from collections import Counter

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI

# ================= Инициализация окружения =================
load_dotenv()  # Загружаем переменные из файла .env

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not BOT_TOKEN or not DEEPSEEK_API_KEY:
    raise ValueError("❌ Токен бота или API ключ DeepSeek не найдены в переменных окружения (.env)!")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Инициализация клиентов
client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()

# Временное хранилище в памяти (В продакшене лучше использовать Redis или БД)
user_data_store = {}


# ================= Модуль "Сырого" Парсинга =================

def extract_data_raw(file_path: str) -> list[dict]:
    """Ультра-всеядный парсер: ищет сообщения по всему массиву текста."""
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    combined_pattern = re.compile(
        r'"from":\s*"([^"]+)"'
        r'.*?'
        r'"text":\s*(?:"([^"]+)"|\[(.*?)\])',
        re.DOTALL
    )

    cleaned = []
    for match in combined_pattern.finditer(content):
        author = match.group(1)

        if match.group(2):
            text = match.group(2)
        elif match.group(3):
            text_parts = re.findall(r'"text":\s*"([^"]+)"', match.group(3))
            if not text_parts:
                text_parts = re.findall(r'"([^"]+)"', match.group(3))
            text = " ".join(text_parts)
        else:
            continue

        if author.lower() in ["null", "telegram", "none"]:
            continue

        text = text.replace('\\n', ' ').replace('\\"', '"').strip()

        if text:
            cleaned.append({"from": author, "text": text})

    if len(cleaned) < 2:
        authors = re.findall(r'"from":\s*"([^"]+)"', content)
        texts = re.findall(r'"text":\s*"([^"]+)"', content)
        if len(authors) == len(texts) and len(authors) > 0:
            for a, t in zip(authors, texts):
                if a.lower() not in ["null", "none"]:
                    cleaned.append({"from": a, "text": t})

    return cleaned


def get_stats(data: list[dict]) -> str:
    """Генерация статистики по сообщениям."""
    msg_counts = Counter(m['from'] for m in data)
    word_counts = Counter()
    for m in data:
        word_counts[m['from']] += len(m['text'].split())

    res = "📊 **Цифровая активность:**\n"
    for user, count in msg_counts.most_common(5):
        res += f"\n👤 **{user}**\n├ Сообщений: {count}\n└ Слов: {word_counts[user]}"
    return res


# ================= Психологические Промпты =================

PROMPTS = {
    "tone": "Ты психолог. Проанализируй атмосферу чата. Используй <b>Жирный текст</b> для заголовков. Учитывай юмор и близость собеседников. Опиши всё в стиле краткого отчета.",
    "psycho": "Ты эксперт по личностям. Опиши персонажей. Используй <b>Жирный текст</b> для имен и психотипов. Будь остроумен и лаконичен.",
    "dispute": "Ты медиатор. Разбери конфликт, если он есть. Используй <b>Жирный текст</b> для выводов. Дай 2-3 совета по примирению."
}


async def analyze_with_ai(prompt: str, chat_data: list[dict]) -> str:
    """Отправка контекста в нейросеть для анализа."""
    context = "\n".join([f"{m['from']}: {m['text']}" for m in chat_data[-10000:]])
    try:
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Проанализируй этот диалог:\n\n{context}"}
            ],
            temperature=0.6
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Ошибка API DeepSeek: {e}")
        return f"❌ Ошибка нейросети: {str(e)}"


# ================= Интерфейс PersonaAI =================

def main_kb() -> InlineKeyboardMarkup:
    """Клавиатура главного меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="do_stats")],
        [InlineKeyboardButton(text="🌈 Тон общения", callback_data="do_tone")],
        [InlineKeyboardButton(text="👤 Психотипы", callback_data="do_psycho")],
        [InlineKeyboardButton(text="⚖️ Решить спор", callback_data="do_dispute")]
    ])


@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "✨ **PersonaAI**\n\n"
        "Я готов проанализировать твой чат, даже если файл поврежден.\n"
        "1️⃣ Пришли мне файл `result.json` из экспорта Telegram.\n"
        "2️⃣ Я извлеку из него суть и проведу психологический разбор."
    )
    await message.answer(text)


@dp.message(F.document)
async def handle_doc(message: Message):
    loading_msg = await message.answer("🛠 Читаю файл в обход системных ошибок...")

    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    file_name = f"raw_{message.from_user.id}.json"

    await bot.download_file(file.file_path, file_name)

    try:
        cleaned_data = extract_data_raw(file_name)

        if not cleaned_data:
            await loading_msg.edit_text("❌ Не удалось найти сообщения. Проверь, что это экспорт из Telegram.")
            return

        user_data_store[message.from_user.id] = cleaned_data

        await loading_msg.edit_text(
            f"✅ Готово! Я смог спасти и прочитать **{len(cleaned_data)}** сообщений.\n"
            "Выбирай режим анализа:",
            reply_markup=main_kb()
        )
    except Exception as e:
        logging.error(f"Ошибка парсинга: {e}")
        await loading_msg.edit_text("❌ Даже текстовый парсер не справился с этим файлом.")
    finally:
        # Гарантированное удаление файла, даже если произошла ошибка
        if os.path.exists(file_name):
            os.remove(file_name)


@dp.callback_query(F.data.startswith("do_"))
async def process_analysis(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_data_store:
        return await callback.answer("Данные потеряны. Загрузи файл снова.", show_alert=True)

    action = callback.data.split("_")[1]
    data = user_data_store[user_id]

    temp_msg = await callback.message.answer("🧠 PersonaAI анализирует контекст... Это займет пару секунд.")
    await callback.answer()

    if action == "stats":
        result = get_stats(data)
    else:
        raw_result = await analyze_with_ai(PROMPTS[action], data)
        result = raw_result.replace("<br>", "\n")

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]])

    try:
        # Поскольку промпты используют <b>, пытаемся отрендерить HTML
        await temp_msg.edit_text(result, parse_mode="HTML", reply_markup=back_kb)
    except Exception as e:
        logging.warning(f"Ошибка парсинга HTML, отправка в безопасном режиме: {e}")
        await temp_msg.edit_text(result, parse_mode=None, reply_markup=back_kb)


@dp.callback_query(F.data == "back")
async def go_back(callback: CallbackQuery):
    await callback.message.edit_text("Выбери тип анализа:", reply_markup=main_kb())


async def main():
    try:
        logging.info("Запуск бота...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())