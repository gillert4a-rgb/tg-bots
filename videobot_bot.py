"""🎬 VideoBot — Telegram-бот для скачивания видео.

Источники: YouTube, TikTok, VK, Instagram.
Флоу (по образцу лучших ботов): ссылка → превью с кнопками качества →
живой прогресс → готовый стриминговый файл.

Ключевые решения:
  • один проход скачивания: размеры форматов известны заранее,
    качество подбирается автоматически под лимит Telegram (50 МБ)
  • полностью автономный: пути к ffmpeg и Deno прописываются на старте,
    чтобы не зависеть от PATH
  • таймауты на каждую фазу + кнопка «Отмена», чтобы ничего не висело вечно
"""

import asyncio
import html
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

# Windows-консоль в cp1251: заставляем вывод не падать на эмодзи
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

TOKEN = os.getenv("VIDEO_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")

# ---------- делаем ffmpeg/Deno доступными независимо от PATH ----------
_EXTRA_PATHS = (
    r"C:\ffmpeg\bin",
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe",
)
for _p in _EXTRA_PATHS:
    _p_str = str(_p)
    if Path(_p_str).is_dir() and _p_str not in os.environ.get("PATH", ""):
        os.environ["PATH"] += os.pathsep + _p_str

# ---------- константы ----------
BUDGET = 48 * 1024 * 1024     # целевой потолок размера с запасом до лимита 50 МБ
MENU_TTL = 20 * 60            # сколько живёт меню выбора качества
PROBE_TIMEOUT = 60            # сек на предварительный запрос метаданных
DOWNLOAD_TIMEOUT = 600        # сек максимум на скачивание
SEND_TIMEOUT = 300            # сек максимум на отправку в Telegram
POLL_DELAY = 2.5              # период обновления прогресс-бара

URL_RE = re.compile(r"https?://\S+")


def platform_of(url: str) -> str:
    """Определяет платформу по ссылке."""
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return "🎥 Видео"
    if host == "youtu.be" or host.endswith((".youtube.com", "youtu.be")):
        return "🎬 YouTube"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "🎵 TikTok"
    if host in ("vk.com", "vkvideo.ru", "vk.ru", "m.vk.com") or host.endswith(".vk.com"):
        return "📺 VK"
    if host == "instagr.am" or host == "instagram.com" or host.endswith(".instagram.com"):
        return "📸 Instagram"
    return "🎥 Видео"


def is_supported_url(text: str) -> str | None:
    """Первая распознанная ссылка на поддерживаемую платформу."""
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?)]}")
        platform = platform_of(url)
        if platform != "🎥 Видео":
            return url
    return None


dp = Dispatcher()
# chat_id -> текущее меню пользователя
pending: dict[int, dict] = {}
# chat_id -> prog последнего активного скачивания (для кнопки «Отмена»)
_active_prog: dict[int, dict] = {}


class Cancelled(Exception):
    """Пользователь нажал «Отмена»."""


BASE_OPTS = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "restrictfilenames": True,
    "concurrent_fragment_downloads": 8,
    "retries": 3,
    "fragment_retries": 5,
    "extractor_retries": 2,
    "socket_timeout": 25,
}

# ---------- тексты ----------

WELCOME = (
    "Привет! 👋 Я <b>VideoBot</b> — качаю видео, чтобы ты мог смотреть их в любой момент.\n\n"
    "🎬 <b>YouTube</b> · 🎵 <b>TikTok</b> (без водяного знака) · 📺 <b>VK</b> · 📸 <b>Instagram</b>\n\n"
    "<b>Как это работает</b>\n"
    "1️⃣ Просто пришли мне ссылку.\n"
    "2️⃣ Выбери качество в меню под превью.\n"
    "3️⃣ Получи файл, который можно <i>смотреть прямо в чате</i>, не скачивая на телефон.\n\n"
    "🎵 Умею вытаскивать звук в MP3.\n"
    "Жду ссылку! ✨"
)

HELP = (
    "<b>Шпаргалка</b>\n\n"
    "📩 Отправь ссылку на видео — YouTube, TikTok, VK или Instagram.\n\n"
    "В меню качества:\n"
    "  • <b>Авто</b> — выберу сам лучшее из тех, что влазят в лимит.\n"
    "  • У каждого качества написан <b>точный размер</b>; если «&gt;50 МБ» — Telegram не примет.\n"
    "  • <b>MP3</b> — только звук под названием видео.\n\n"
    "<b>Полезно знать</b>\n"
    "🚦 Лимит Telegram Bot API — <b>50 МБ</b> на файл.\n"
    "🛑 У любого скачивания есть кнопка «Отмена».\n"
    "⚡ Готовое видео приходит как <i>стрим</i>: проигрывай в чате без сохранения.\n"
    "⏱ Долгие у меня не висят: есть таймауты на каждую фазу.\n"
)

PLACEHOLDER_URL = (
    "📩 Пришли ссылку на видео:\n"
    "🎬 YouTube · 🎵 TikTok · 📺 VK · 📸 Instagram\n\n"
    "Например:\n<i>https://youtu.be/dQw4w9WgXcQ</i>"
)


def hint_for(platform: str) -> str:
    if "TikTok" in platform:
        return "🚫 Без водяных знаков, как привыкли"
    if "Instagram" in platform:
        return "📸 Работает и с рилс, и с постами"
    if "VK" in platform:
        return "📺 В оригинальном качестве"
    return ""


# ---------- форматирование ----------

def fmt_size(n) -> str:
    if not n:
        return "?"
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.2f} ГБ"
    if n >= 1e6:
        return f"{n / 1e6:.1f} МБ"
    return f"{n / 1e3:.0f} Кб"


def fmt_dur(sec) -> str:
    if not sec:
        return "?"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _disp_len(s: str) -> int:
    """Ширина строки в колонках консоли: широкие эмодзи считаем за 2."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def print_banner() -> None:
    """Краткая карточка о боте — печатается до старта процесса."""
    from importlib.metadata import version as pkg_version

    def pr(name: str, fallback: str) -> str:
        try:
            return pkg_version(name)
        except Exception:
            return fallback

    rows = [
        ("Платформы", "YouTube · TikTok · VK · Instagram"),
        ("Форматы", "видео до 4K · звук в MP3"),
        ("Как работает", "превью → выбор качества → живой прогресс → файл"),
        ("Доставка", "стриминговое видео с обложкой — смотри прямо в чате"),
        ("Лимит", "файл ≤ 50 МБ — больше Telegram не примет"),
        (
            "Стек",
            f"Python {sys.version.split()[0]}"
            f" · aiogram {pr('aiogram', '?')}"
            f" · yt-dlp {pr('yt-dlp', '?')}",
        ),
    ]
    title = "🎬 VideoBot — видео из соцсетей — в Telegram"

    label_pad = max(_disp_len(l) for l, _ in rows) + 2
    texts = [title] + [
        lab + " " * (label_pad - _disp_len(lab)) + val for lab, val in rows
    ]
    wmax = max(_disp_len(t) for t in texts)
    border = "═" * (wmax + 2)

    print("╔" + border + "╗", flush=True)
    for i, t in enumerate(texts):
        if i == 1:
            print("╠" + border + "╣", flush=True)
        print("║ " + t + " " * (wmax - _disp_len(t)) + " ║", flush=True)
    print("╚" + border + "╝", flush=True)


def progress_text(prog: dict) -> str:
    if prog.get("phase") == "processing":
        return "🛠 Обрабатываю: склеиваю видеоряд и звук…"
    dl = prog.get("downloaded") or 0
    total = prog.get("total") or 0
    if not total:
        return "⏳ Скачиваю…"
    pct = min(dl / total, 1.0)
    filled = int(pct * 16)
    bar = "█" * filled + "░" * (16 - filled)
    extra = ""
    speed = prog.get("speed") or 0
    if speed:
        extra += f" · {fmt_size(speed)}/с"
    eta = prog.get("eta")
    if eta:
        extra += f" · ~{fmt_dur(eta)}"
    return (
        f"⏳ <b>{pct * 100:.0f}%</b> [{bar}]\n"
        f"{fmt_size(dl)} из {fmt_size(total)}{extra}"
    )


# ---------- yt-dlp (синхронные функции; вызываются в отдельных потоках) ----------

def probe(url: str) -> dict:
    """Лёгкий запрос метаданных без скачивания."""
    opts = {**BASE_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = list(info["entries"])
        if not entries:
            raise yt_dlp.utils.DownloadError("пустой плейлист")
        info = entries[0]
    return info


def analyze(info: dict) -> dict:
    """Сводка по форматам: доступные высоты с оценкой размеров."""
    duration = info.get("duration") or 0
    formats = info.get("formats") or []

    def size(f: dict) -> int:
        return f.get("filesize") or f.get("filesize_approx") or 0

    # оценка аудиодорожки для blends (видео+звук)
    audio_est = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            bitrate = f.get("abr") or f.get("tbr")
            est = bitrate * duration / 8 if bitrate and duration else size(f)
            audio_est = max(audio_est, int(est or 0))

    by_height: dict[int, dict] = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        e = by_height.setdefault(h, {"whole": 0, "video": 0})
        if f.get("acodec") != "none":
            e["whole"] = max(e["whole"], size(f))
        else:
            e["video"] = max(e["video"], size(f))

    heights = {}
    for h, e in sorted(by_height.items()):
        total = e["whole"] or (e["video"] + audio_est)
        heights[h] = total
    has_audio = any(f.get("vcodec") == "none" and f.get("acodec") != "none" for f in formats)
    return {"heights": heights, "has_audio": has_audio}


def pick_auto(info: dict) -> int | None:
    stats = analyze(info)["heights"]
    feasible = [h for h, t in stats.items() if t and t <= BUDGET]
    if feasible:
        return max(feasible)
    return min(stats) if stats else None


def download(url: str, height: int | None, audio_only: bool, prog: dict, outdir: str) -> Path:
    """Скачивает один файл; следит за prog['cancel'] и кидает Cancelled."""

    def hook(d):
        if prog.get("cancel"):
            raise Cancelled()
        if d.get("status") == "downloading":
            prog["downloaded"] = d.get("downloaded_bytes") or 0
            prog["total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            prog["speed"] = d.get("speed") or 0
            prog["eta"] = d.get("eta")
        elif d.get("status") == "finished":
            prog["phase"] = "processing"

    opts = {
        **BASE_OPTS,
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
    }
    if audio_only:
        opts["format"] = "ba/b"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ]
    else:
        h = height or 1080
        opts["format"] = (
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/"
            f"b[height<={h}]/b"
        )
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Cancelled:
        raise
    except yt_dlp.utils.DownloadError as e:
        if prog.get("cancel"):
            raise Cancelled() from e
        raise

    files = sorted(
        (p for p in Path(outdir).iterdir() if p.is_file()),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("Скачивание не дало файлов")
    return files[0]


def _http_get(url: str) -> bytes | None:
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


def fetch_thumb_bytes(info: dict) -> bytes | None:
    """Качает превью в память — для мгновенной обложки у результата."""
    url = info.get("thumbnail")
    if url:
        data = _http_get(url)
        if data:
            return data
    for f in reversed(info.get("formats") or []):
        for t in (f.get("thumbnails") or []):
            if t.get("url"):
                data = _http_get(t["url"])
                if data:
                    return data
    return None


def cleanup(path: Path | None) -> None:
    if path and path.exists():
        shutil.rmtree(path, ignore_errors=True)


async def safe_edit(bot: Bot, chat_id: int, message_id: int | None, text: str, reply_markup=None) -> None:
    """Единая точка правки статуса: ключевые аргументы, io ошибок не роняют."""
    if message_id is None:
        return
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
    except Exception:
        pass


# ---------- клавиатуры ----------

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Отмена", callback_data="cancel")],
    ])


def quality_keyboard(item: dict) -> InlineKeyboardMarkup:
    platform = item.get("platform") or "🎥"
    platform_short = platform.split(" ", 1)[-1]
    rows = [[
        InlineKeyboardButton(text=f"✨ Авто · {platform_short}", callback_data="q:auto"),
        InlineKeyboardButton(text="🎵 MP3", callback_data="q:audio"),
    ]]
    stats = item.get("heights") or {}
    labels = [
        (2160, "🔥 4K"),
        (1440, "✨ 2K"),
        (1080, "💎 1080p"),
        (720, "🖥 720p"),
        (480, "📱 480p"),
        (360, "▫️ 360p"),
    ]
    quality_rows, row = [], []
    for h, label in labels:
        if h not in stats:
            continue
        total = stats[h] or 0
        note = fmt_size(total) if total and total <= BUDGET else ">50 МБ ⚠️"
        row.append(InlineKeyboardButton(text=f"{label} · {note}", callback_data=f"q:{h}"))
        if len(row) == 3:
            quality_rows.append(row)
            row = []
    if row:
        quality_rows.append(row)
    rows += quality_rows
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- сценарий скачивания ----------

async def run_download(
    bot: Bot,
    chat_id: int,
    item: dict,
    height: int | None,
    audio_only: bool,
    prog: dict,
) -> None:
    url = item["url"]
    platform = item.get("platform") or "🎥"
    title = item.get("title") or "video"

    status = await bot.send_message(chat_id, "⏳ Готовлюсь…", reply_markup=cancel_kb())
    outdir = Path(tempfile.mkdtemp(prefix="videobot_"))
    _active_prog[chat_id] = prog

    async def poller() -> None:
        last = ""
        while True:
            await asyncio.sleep(POLL_DELAY)
            txt = progress_text(prog)
            if txt != last and not prog.get("cancel"):
                last = txt
                await safe_edit(bot, chat_id, status.message_id, txt, reply_markup=cancel_kb())

    poll_task = asyncio.create_task(poller())
    try:
        t0 = time.time()
        print(f"[dbg] chat {chat_id}: {platform} старт скачивания h={height} audio={audio_only}", flush=True)
        path = await asyncio.wait_for(
            asyncio.to_thread(download, url, height, audio_only, prog, str(outdir)),
            timeout=DOWNLOAD_TIMEOUT,
        )
        poll_task.cancel()
        if prog.get("cancel"):
            raise Cancelled()
        size = path.stat().st_size
        print(f"[dbg] chat {chat_id}: скачано {fmt_size(size)} за {time.time() - t0:.1f} с", flush=True)

        if not audio_only and size > 50 * 1024 * 1024:
            await safe_edit(
                bot, chat_id, status.message_id,
                "📉 Файл получился больше 50 МБ — Telegram такой не примет.\n"
                "Выбери качество поменьше или MP3.",
            )
            return

        await safe_edit(bot, chat_id, status.message_id, "📤 Загружаю в Telegram…")
        t1 = time.time()

        height_note = f" · высота {height}p" if height else ""
        safe_title = html.escape(title[:80])   # <, >, & в названиях ломают HTML-парсер Telegram
        caption = (
            f"✅ <b>{safe_title}</b>\n{platform} · {fmt_size(size)}{height_note}"
        ) if not audio_only else (
            f"🎧 <b>{safe_title}</b>\nMP3 · {fmt_size(size)} · {platform}"
        )
        hint = hint_for(platform)
        if hint:
            caption += f"\n{hint}"

        thumb_files = None
        thumb_bytes = item.get("thumb_bytes")
        if thumb_bytes:
            thumb_files = BufferedInputFile(thumb_bytes, "cover.jpg")

        async def send_file() -> None:
            if audio_only:
                await bot.send_audio(
                    chat_id, FSInputFile(str(path)), caption=caption, thumbnail=thumb_files,
                )
            else:
                await bot.send_video(
                    chat_id,
                    FSInputFile(str(path)),
                    caption=caption,
                    supports_streaming=True,
                    thumbnail=thumb_files,
                )

        async def send_file_backoff() -> None:
            # по скиллу: 429 от Telegram — ждём retry_after и повторяем
            for attempt in range(3):
                try:
                    await send_file()
                    return
                except TelegramRetryAfter as e:
                    if attempt == 2:
                        raise
                    print(f"[dbg] chat {chat_id}: 429, повтор через {e.retry_after} с", flush=True)
                    await asyncio.sleep(e.retry_after + 1)

        await asyncio.wait_for(send_file_backoff(), timeout=SEND_TIMEOUT)
        await bot.delete_message(chat_id, status.message_id)
        print(f"[dbg] chat {chat_id}: отправка заняла {time.time() - t1:.1f} с", flush=True)

    except Cancelled:
        await safe_edit(bot, chat_id, status.message_id, "🛑 Отменено. Пришли ссылку ещё раз, если захочешь.")
    except asyncio.TimeoutError:
        await safe_edit(
            bot, chat_id, status.message_id,
            "⏰ Не уложился в разумное время и остановился.\nПопробуй качество поменьше.",
        )
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        print(f"[dbg] chat {chat_id}: DownloadError: {err[:500]}", flush=True)
        if "Private video" in err or ("login" in err.lower() and "Instagram" in platform):
            text = "🔒 Источник закрыт (приватность, гео или логин)."
        elif "unavailable" in err or "removed" in err or "not available" in err.lower():
            text = "🗑 Видео уже недоступно или удалено."
        elif "unsupported URL" in err:
            text = "🤔 Эту ссылку я не поймал. Проверь адрес."
        elif "Requested format is not available" in err:
            text = "🎚 Такого качества у видео нет — выбери другое."
        elif "is too large" in err or "file is larger" in err:
            text = "📦 Больше 50 МБ даже в этом качестве."
        elif "пустой плейлист" in err:
            text = "🔗 Плейлист пустой. Скинь ссылку на конкретное видео."
        else:
            text = "❌ Не получилось. Ссылка могла устареть."
        await safe_edit(bot, chat_id, status.message_id, text)
    except Exception:
        import traceback
        traceback.print_exc()
        await safe_edit(bot, chat_id, status.message_id, "❌ Что-то пошло не так. Попробуй ещё раз.")
    finally:
        poll_task.cancel()
        _active_prog.pop(chat_id, None)
        cleanup(outdir)


# ---------- хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@dp.message(F.text)
async def handle_url(message: Message, bot: Bot) -> None:
    text = message.text or ""
    url = is_supported_url(text)
    if not url:
        await message.answer(PLACEHOLDER_URL)
        return

    platform = platform_of(url)
    thinking = await bot.send_message(message.chat.id, "🔎 Изучаю ссылку…")
    try:
        info = await asyncio.wait_for(asyncio.to_thread(probe, url), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        await thinking.edit_text("⏰ Источник не отвечает. Попробуй позже.")
        return
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        print(f"[dbg] chat {message.chat.id}: probe err: {err[:300]}", flush=True)
        if "Private video" in err:
            await thinking.edit_text("🔒 Приватное видео — я не виднён доступ.")
        elif "login" in err.lower():
            await thinking.edit_text("🔑 Ссылка требует логина на источнике.")
        else:
            await thinking.edit_text("😕 Не смог открыть ссылку. Проверь и повтори.")
        return
    except Exception:
        import traceback
        traceback.print_exc()
        await thinking.edit_text("😕 Ссылка не открылась. Проверь адрес и повтори.")
        return

    stats = analyze(info)
    thumb = fetch_thumb_bytes(info)

    item = {
        "url": url,
        "ts": time.time(),
        "platform": platform,
        "heights": stats["heights"],
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "thumb_bytes": thumb,
    }
    pending[message.chat.id] = item

    head = f"<b>{html.escape((item.get('title') or 'Видео')[:120])}</b>"
    meta = f"👤 {html.escape(item.get('uploader') or 'автор')} · ⏱ {fmt_dur(item.get('duration'))}"
    footer = "<b>Выбери качество:</b>"
    caption_text = f"{head}\n{meta}\n\n{footer}"

    try:
        await thinking.delete()
    except Exception:
        pass

    if thumb:
        try:
            await bot.send_photo(
                message.chat.id,
                BufferedInputFile(thumb, "preview.jpg"),
                caption=caption_text,
                reply_markup=quality_keyboard(item),
            )
            return
        except Exception:
            pass
    await bot.send_message(message.chat.id, caption_text, reply_markup=quality_keyboard(item))


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb, bot: Bot) -> None:
    prog = _active_prog.get(cb.message.chat.id)
    if prog:
        prog["cancel"] = True
        await cb.answer("🛑 Останавливаю…")
    else:
        await cb.answer()


@dp.callback_query(F.data.startswith("q:"))
async def cb_quality(cb, bot: Bot) -> None:
    chat_id = cb.message.chat.id
    if chat_id in _active_prog:
        # по скиллу: per-user rate limiting — одно скачивание за раз на чат
        await cb.answer("Докачиваю предыдущее — выберешь сразу после 🙃", show_alert=True)
        return
    item = pending.get(chat_id)
    if not item or time.time() - item["ts"] > MENU_TTL:
        await cb.answer("Меню устарело — пришли ссылку заново 🙃", show_alert=True)
        return

    mode = (cb.data or "").split(":", 1)[1]
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    prog: dict = {"downloaded": 0, "total": 0, "speed": 0, "phase": None, "cancel": False}

    if mode == "audio":
        await run_download(bot, chat_id, item, None, audio_only=True, prog=prog)
    elif mode == "auto":
        try:
            info = await asyncio.wait_for(asyncio.to_thread(probe, item["url"]), timeout=PROBE_TIMEOUT)
            height = pick_auto(info)
            print(f"[dbg] chat {chat_id}: авто выбрал h={height}", flush=True)
        except Exception as e:
            height = None
            print(f"[dbg] chat {chat_id}: авто-проба не удалась: {e!r}", flush=True)
        await run_download(bot, chat_id, item, height, audio_only=False, prog=prog)
    else:
        await run_download(bot, chat_id, item, int(mode), audio_only=False, prog=prog)


async def health_server() -> None:
    """Мини-HTTP на порт 7860 — нужен облачным платформам (HF Spaces: проверка активности)."""
    port = int(os.environ.get("PORT", "7860"))

    async def handler(reader, writer):
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handler, "0.0.0.0", port)
        print(f"[dbg] health-сервер отвечает на порту {port}", flush=True)
        async with server:
            await server.serve_forever()
    except Exception as e:
        print(f"[dbg] health-сервер не поднялся: {e!r}", flush=True)


async def self_ping() -> None:
    """Облачный режим: пингуем собственный публичный URL — платформа не засыпает
    (HF Spaces: каждые 2 ч; Render: каждые 10 мин, там засыпание после 15 мин)."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    interval = 600
    if not url:
        space_id = os.environ.get("SPACE_ID")
        if not space_id:
            return
        url = f"https://{space_id.replace('/', '-').lower()}.hf.space"
        interval = 7200
    print(f"[dbg] self-ping каждые {interval // 60} мин на {url}", flush=True)
    while True:
        await asyncio.sleep(interval)
        if _http_get(url):
            print("[dbg] self-ping ok", flush=True)


async def main() -> None:
    print_banner()
    if not TOKEN:
        raise SystemExit("BOT_TOKEN не задан! Создай .env со строкой BOT_TOKEN=...")
    bot = Bot(
        TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Начать"),
        BotCommand(command="help", description="❓ Шпаргалка"),
    ])
    me = await bot.get_me()
    print(f"✅ Бот жив: @{me.username}", flush=True)
    await bot.delete_webhook(drop_pending_updates=True)
    # по скиллу: только нужные типы апдейтов — меньше трафика и шума
    asyncio.create_task(health_server())
    asyncio.create_task(self_ping())
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
