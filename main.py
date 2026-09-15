"""
Telegram-бот для скачивания видео и фото из Instagram, TikTok, X (Twitter)
и YouTube Shorts с обязательной подпиской на канал.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid

import requests
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")          # токен берём из переменной окружения
GROQ_API_KEY = os.getenv("GROQ_API_KEY")    # ключ Groq для расшифровки (Whisper)
CHANNEL_USERNAME = "@times_officialuz"      # канал, на который нужна подписка
CHANNEL_LINK = "https://t.me/times_officialuz"
MAX_FILE_SIZE = 50 * 1024 * 1024            # лимит Telegram Bot API — 50 МБ
# ===============================================

# Память для кнопки «Расшифровка»: короткий id -> ссылка на видео
transcribe_urls: dict[str, str] = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Регулярное выражение для поддерживаемых ссылок
URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?"
    r"(instagram\.com|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|"
    r"twitter\.com|x\.com|youtube\.com/shorts|youtu\.be)"
    r"[^\s]*",
    re.IGNORECASE,
)


def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
        ]
    )


async def is_subscribed(user_id: int) -> bool:
    """Проверяем, подписан ли пользователь на канал."""
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        logger.warning(f"Не удалось проверить подписку: {e}")
        return False


def download_media(url: str, tmp_dir: str) -> list[str]:
    """
    Скачивает видео (или фото) через yt-dlp.
    Возвращает список путей к скачанным файлам.
    Работает в отдельном потоке, чтобы не блокировать бота.
    """
    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        # видео не больше 50 МБ, лучшее качество в mp4
        "format": (
            "best[filesize<50M][ext=mp4]/"
            "best[filesize_approx<50M][ext=mp4]/"
            "best[ext=mp4]/best"
        ),
        "max_filesize": MAX_FILE_SIZE,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = [
        os.path.join(tmp_dir, f)
        for f in os.listdir(tmp_dir)
        if os.path.isfile(os.path.join(tmp_dir, f))
    ]
    return files


def download_audio(url: str, tmp_dir: str) -> str | None:
    """Скачивает только аудиодорожку и сжимает её в mp3 для расшифровки."""
    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "audio.%(ext)s"),
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",  # 64 кбит/с достаточно для распознавания речи
            }
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    audio_path = os.path.join(tmp_dir, "audio.mp3")
    return audio_path if os.path.exists(audio_path) else None


def transcribe_audio(audio_path: str) -> str:
    """Отправляет аудио в Groq API (Whisper) и возвращает текст расшифровки."""
    with open(audio_path, "rb") as f:
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("audio.mp3", f, "audio/mpeg")},
            data={"model": "whisper-large-v3", "response_format": "text"},
            timeout=300,
        )
    response.raise_for_status()
    return response.text.strip()


def download_photos_gallery_dl(url: str, tmp_dir: str) -> list[str]:
    """
    Запасной вариант для фото-постов (Instagram, X):
    качаем через gallery-dl, если yt-dlp не справился.
    """
    import subprocess

    subprocess.run(
        ["gallery-dl", "-D", tmp_dir, "--filter", "extension in ('jpg','jpeg','png','webp')", url],
        capture_output=True,
        timeout=120,
    )
    files = []
    for root, _, names in os.walk(tmp_dir):
        for name in names:
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                files.append(os.path.join(root, name))
    return files


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if await is_subscribed(message.from_user.id):
        await message.answer(
            "👋 Привет! Я умею скачивать видео и фото из:\n\n"
            "📸 Instagram (Reels, посты)\n"
            "🎵 TikTok\n"
            "🐦 X (Twitter)\n"
            "▶️ YouTube Shorts\n\n"
            "А ещё умею делать 📝 текстовую расшифровку речи из видео — "
            "кнопка появится под скачанным роликом.\n\n"
            "Просто отправь мне ссылку 🔗"
        )
    else:
        await message.answer(
            "👋 Привет! Чтобы пользоваться ботом, подпишись на наш канал 👇",
            reply_markup=subscribe_keyboard(),
        )


@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Отлично, подписка подтверждена!\n\n"
            "Теперь просто отправь мне ссылку на видео или фото из "
            "Instagram, TikTok, X или YouTube Shorts 🔗"
        )
    else:
        await callback.answer("❌ Ты ещё не подписался на канал!", show_alert=True)


@dp.callback_query(F.data.startswith("tr:"))
async def handle_transcribe(callback: CallbackQuery):
    """Нажатие на кнопку «📝 Расшифровка» под видео."""
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("🔒 Сначала подпишись на канал!", show_alert=True)
        return

    url_id = callback.data.split(":", 1)[1]
    url = transcribe_urls.get(url_id)
    if not url:
        await callback.answer(
            "⏰ Кнопка устарела. Отправь ссылку ещё раз.", show_alert=True
        )
        return

    await callback.answer("⏳ Делаю расшифровку...")
    status_msg = await callback.message.reply("🎙 Извлекаю аудио и распознаю речь...")
    tmp_dir = tempfile.mkdtemp()

    try:
        loop = asyncio.get_running_loop()
        audio_path = await loop.run_in_executor(None, download_audio, url, tmp_dir)
        if not audio_path:
            await status_msg.edit_text("😔 Не удалось извлечь аудио из этого видео.")
            return

        text = await loop.run_in_executor(None, transcribe_audio, audio_path)
        if not text:
            await status_msg.edit_text("🔇 Похоже, в этом видео нет речи.")
            return

        # Telegram ограничивает сообщение 4096 символами — режем на части
        header = "📝 <b>Расшифровка:</b>\n\n"
        chunk_size = 4000
        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
        await status_msg.edit_text(header + chunks[0])
        for chunk in chunks[1:]:
            await callback.message.reply(chunk)

    except Exception as e:
        logger.error(f"Ошибка расшифровки: {e}")
        await status_msg.edit_text("😔 Не получилось сделать расшифровку. Попробуй позже.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dp.message(F.text)
async def handle_link(message: Message):
    # 1. Проверка подписки
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "🔒 Для скачивания нужно подписаться на канал 👇",
            reply_markup=subscribe_keyboard(),
        )
        return

    # 2. Проверка, что в сообщении есть поддерживаемая ссылка
    match = URL_PATTERN.search(message.text)
    if not match:
        await message.answer(
            "🤔 Не вижу ссылку. Отправь ссылку из Instagram, TikTok, "
            "X (Twitter) или YouTube Shorts."
        )
        return

    url = match.group(0)
    if not url.startswith("http"):
        url = "https://" + url

    status_msg = await message.answer("⏳ Скачиваю, подожди немного...")
    tmp_dir = tempfile.mkdtemp()

    try:
        loop = asyncio.get_running_loop()

        # Сначала пробуем yt-dlp (видео)
        files = []
        try:
            files = await loop.run_in_executor(None, download_media, url, tmp_dir)
        except Exception as e:
            logger.info(f"yt-dlp не справился ({e}), пробую gallery-dl...")

        # Если видео не скачалось — пробуем фото через gallery-dl
        if not files:
            files = await loop.run_in_executor(
                None, download_photos_gallery_dl, url, tmp_dir
            )

        if not files:
            await status_msg.edit_text(
                "😔 Не удалось скачать. Возможно, пост приватный, удалён "
                "или видео слишком большое (лимит — 50 МБ)."
            )
            return

        # 3. Отправляем результат
        videos = [f for f in files if f.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
        photos = [f for f in files if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]

        caption = f"📥 Скачано через @{(await bot.get_me()).username}"

        # Кнопка «Расшифровка» (показываем, только если задан ключ Groq)
        transcribe_kb = None
        if GROQ_API_KEY and videos:
            url_id = uuid.uuid4().hex[:16]
            transcribe_urls[url_id] = url
            # чистим память, если ссылок стало слишком много
            if len(transcribe_urls) > 1000:
                for old_key in list(transcribe_urls)[:500]:
                    transcribe_urls.pop(old_key, None)
            transcribe_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📝 Расшифровка", callback_data=f"tr:{url_id}"
                        )
                    ]
                ]
            )

        for video in videos:
            if os.path.getsize(video) > MAX_FILE_SIZE:
                await message.answer("⚠️ Видео больше 50 МБ — Telegram не даёт его отправить.")
                continue
            await message.answer_video(
                FSInputFile(video), caption=caption, reply_markup=transcribe_kb
            )

        # Фото отправляем альбомами по 10 штук
        for i in range(0, len(photos), 10):
            chunk = photos[i : i + 10]
            if len(chunk) == 1:
                await message.answer_photo(FSInputFile(chunk[0]), caption=caption)
            else:
                media = [InputMediaPhoto(media=FSInputFile(p)) for p in chunk]
                media[0].caption = caption
                await message.answer_media_group(media)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await status_msg.edit_text("😔 Произошла ошибка при скачивании. Попробуй другую ссылку.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def main():
    logger.info("Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
