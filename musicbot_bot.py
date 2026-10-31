"""🎧 MusicBot — Telegram-бот для скачивания музыки.

Источники: YouTube / YouTube Music, VK, Яндекс Музыка, Spotify,
а также поиск по названию песни.

Ключевые решения:
  • у Spotify и Яндекса аудиофайлов нет — берём из их публичных страниц
    название/артиста/обложку и ищем звук на YouTube Music (ytsearch)
  • битрейт подбирается автоматически под лимит Telegram (50 МБ),
    размеры написаны в меню заранее
  • полностью автономный: пути к ffmpeg и Deno прописываются на старте
  • таймауты на каждую фазу + кнопка «Отмена», чтобы ничего не висело
"""

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit

# Windows-консоль в cp1251: заставляем вывод не падать на эмодзи
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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

TOKEN = os.getenv("MUSIC_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")

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
BITRATES = (320, 256, 192, 128)
MENU_TTL = 20 * 60            # сколько живёт меню выбора битрейта
PROBE_TIMEOUT = 60            # сек на предварительный запрос метаданных
DOWNLOAD_TIMEOUT = 600        # сек максимум на скачивание
SEND_TIMEOUT = 300            # сек максимум на отправку в Telegram
POLL_DELAY = 2.5              # период обновления прогресс-бара

URL_RE = re.compile(r"https?://\S+")
GREETINGS = {"привет", "прив", "хай", "hi", "hello", "ку", "здравствуйте",
             "добрый день", "здоров", "start", "старт", "начать"}


def platform_of(url: str) -> str:
    """Короткая подпись платформы для подписи и логов."""
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return "🎧 Музыка"
    if host == "music.youtube.com":
        return "🎧 YouTube Music"
    if host == "youtu.be" or host.endswith(("youtube.com", "youtu.be")):
        return "▶️ YouTube"
    if host == "vk.com" or host in ("vkvideo.ru", "vk.ru", "m.vk.com") or host.endswith(".vk.com"):
        return "📺 VK"
    if host == "open.spotify.com" or host.endswith(".spotify.com"):
        return "🟢 Spotify"
    if host == "music.yandex.ru":
        return "🔴 Яндекс Музыка"
    return "🎧 Музыка"


def classify(url: str) -> str | None:
    """Тип ссылки: youtube / vk / spotify / yandex / None — не наша."""
    try:
        host = urlsplit(url).netloc.lower()
        path = urlsplit(url).path.lower()
    except ValueError:
        return None
    if host == "youtu.be" or host.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    if host in ("vk.com", "vkvideo.ru", "vk.ru", "m.vk.com") or host.endswith(".vk.com"):
        return "vk"
    if host.endswith(".spotify.com"):
        return "spotify" if re.search(r"/track/[A-Za-z0-9]+", path) else "spotify:other"
    if host == "music.yandex.ru":
        return "yandex" if re.search(r"track/\d+", path) else "yandex:other"
    return None


def first_url(text: str) -> tuple[str, str] | None:
    """Первая ссылка на поддерживаемый источник → (url, kind)."""
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?)]}\"'")
        kind = classify(url)
        if kind and not kind.endswith(":other"):
            return url, kind
    return None


def has_foreign_url(text: str) -> bool:
    """В тексте есть ссылка, но не на поддерживаемый источник."""
    return bool(URL_RE.search(text or ""))


# ---------- HTTP-помощники ----------

def http_get(url: str, timeout: int = 15, headers: dict | None = None) -> bytes | None:
    try:
        import urllib.request

        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _meta_tag(page: str, prop: str) -> str | None:
    m = re.search(rf'og:{re.escape(prop)}"\s+content="([^"]*)"', page)
    return m.group(1) if m else None


def _artist_from_yandex_desc(desc: str | None) -> str | None:
    """og:description у Яндекса: 'Артист • Сингл • 1995' — артист до первого маркера."""
    if not desc:
        return None
    desc = desc.replace("\xa0", " ").strip()
    for sep in (" • ", " · ", " — ", " – ", " - "):
        if sep in desc:
            return desc.split(sep, 1)[0].strip()
    return None


def resolve_spotify(url: str) -> dict:
    """Метаданные трека из oEmbed Spotify: название + обложка (артиста oEmbed не даёт)."""
    data = http_get("https://open.spotify.com/oembed?url=" + quote(url, safe=""))
    if not data:
        raise RuntimeError("не ответило")
    j = json.loads(data.decode("utf-8", "replace"))
    title = (j.get("title") or "").strip()
    if not title:
        raise RuntimeError("нет названия")
    return {"title": title, "artist": None, "cover_url": j.get("thumbnail_url")}


def resolve_yandex(url: str) -> dict:
    """Метаданные трека со страницы Яндекса: og:title=название, og:description='Артист — …'."""
    page_bytes = http_get(url, headers={"Accept-Language": "ru,RU;q=0.9"})
    if not page_bytes:
        raise RuntimeError("не ответило")
    page = page_bytes.decode("utf-8", "replace")
    title = (_meta_tag(page, "title") or "").strip()
    if not title:
        raise RuntimeError("нет названия")
    artist = _artist_from_yandex_desc(_meta_tag(page, "description"))
    cover = _meta_tag(page, "image")
    return {"title": title, "artist": artist, "cover_url": cover}


# ---------- соответствие чатов ----------

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
    "Привет! 👋 Я <b>MusicBot</b> — скачиваю музыку и раскладываю её по чату как MP3.\n\n"
    "🎧 <b>YouTube Music</b> · 📺 <b>VK</b> · 🔴 <b>Яндекс Музыка</b> · 🟢 <b>Spotify</b>\n\n"
    "<b>Как это работает</b>\n"
    "1️⃣ Пришли ссылку на трек или просто его название.\n"
    "2️⃣ Выбери битрейт в меню (размер файла виден заранее).\n"
    "3️⃣ Получи MP3 с <i>обложкой и тегами</i> — слушается прямо в чате.\n\n"
    "🎚 Сам подберу битрейт под лимит Telegram.\n"
    "Жду песню! ✨"
)

HELP = (
    "<b>Шпаргалка</b>\n\n"
    "📩 Отправь ссылку на трек или <b>название песни</b> — я найду и скачаю.\n\n"
    "В меню битрейта:\n"
    "  • <b>Авто</b> — выберу максимальный битрейт, что влезает в лимит.\n"
    "  • У каждого битрейта написан <b>размер файла</b>; если «&gt;50 МБ» — трек слишком длинный.\n\n"
    "<b>Полезно знать</b>\n"
    "🚦 Лимит Telegram Bot API — <b>50 МБ</b> на файл.\n"
    "🛑 У любого скачивания есть кнопка «Отмена».\n"
    "🖼 В MP3 зашита обложка и теги (исполнитель, название).\n"
    "⏱ Долгие у меня не висят: есть таймауты на каждую фазу.\n"
)

PLACEHOLDER = (
    "🎧 Пришли ссылку на трек или просто название песни:\n"
    "🎧 YouTube Music · 📺 VK · 🔴 Яндекс Музыка · 🟢 Spotify\n\n"
    "Например:\n<i>The Weeknd — Blinding Lights</i>"
)

UNSUPPORTED = (
    "🤔 Эту ссылку я не поддерживаю.\n"
    "Я про музыку: 🎧 YouTube Music · 📺 VK · 🔴 Яндекс Музыка · 🟢 Spotify\n"
    "Или просто напиши название песни — найду сам."
)

NOT_TRACK = {
    "spotify:other": "🟢 Пришли мне ссылку на <b>конкретный трек</b> Spotify — альбомы и плейлисты целиком не влезут 🙃",
    "yandex:other": "🔴 Пришли мне ссылку на <b>конкретный трек</b> Яндекс Музыки — альбом целиком не влезет 🙃",
}

VK_AUDIO_HINT = (
    "🎧 Музыку VK иногда не отдают без авторизации.\n"
    "Пришли ссылку на <b>клип/видео</b> с этой музыкой или просто <b>название песни</b> — найду сам 💫"
)

GREETING = "👋 Привет! Пришли ссылку на трек или название песни — и в чат прилетит MP3 🎶"

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
        ("Источники", "YouTube Music · VK · Яндекс Музыка · Spotify"),
        ("Формат", "MP3 (320/256/192/128) · обложка и теги"),
        ("Как работает", "трек → выбор битрейта → живой прогресс → файл"),
        ("Бонус", "можно прислать просто название песни — найду сам"),
        ("Лимит", "файл ≤ 50 МБ — битрейт подберётся под него сам"),
        (
            "Стек",
            f"Python {sys.version.split()[0]}"
            f" · aiogram {pr('aiogram', '?')}"
            f" · yt-dlp {pr('yt-dlp', '?')}",
        ),
    ]
    title = "🎧 MusicBot — музыка из соцсетей — в Telegram"

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
        return "🛠 Конвертирую в MP3 и прописываю теги…"
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


# ---------- размеры по битрейтам ----------

def size_for(duration: float, kbps: int) -> int:
    """Оценка размера MP3: битрейт (кбит/с) * длительность / 8."""
    return int(kbps * 1000 * duration / 8) if duration else 0


def pick_auto(duration: float) -> int:
    if not duration:
        return BITRATES[0]  # длительность неизвестна: обычная песня влезает на 320
    feasible = [b for b in BITRATES if size_for(duration, b) <= BUDGET]
    if feasible:
        return max(feasible)
    return BITRATES[-1]  # 128 — на случай очень длинных треков


# ---------- yt-dlp (синхронные функции; вызываются в отдельных потоках) ----------

def probe(url: str) -> dict:
    """Лёгкий запрос метаданных без скачивания; поиск тоже получает полный результат."""
    opts = {**BASE_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = list(info["entries"])
        if not entries:
            raise yt_dlp.utils.DownloadError("ничего не нашлось")
        info = entries[0]
    elif info.get("_type") == "playlist":
        raise yt_dlp.utils.DownloadError("ничего не нашлось")
    return info


def search_probe(query: str) -> dict:
    """Ищет первый результат на YouTube Music-подобном поиске. Кидает DownloadError, если пусто."""
    return probe("ytsearch1:" + query.strip())


def _clean_artist(name: str) -> str:
    name = (name or "").strip()
    for suffix in (" - Topic", " — Topic", "VEVO", " - Official Artist Channel",
                   " Official Artist Channel"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name or "неизвестный исполнитель"


def yt_page_url(entry: dict) -> str:
    return entry.get("webpage_url") or (
        "https://www.youtube.com/watch?v=" + (entry.get("id") or "")
    )


def _hook_factory(prog: dict):
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
    return hook


def _find_final_file(outdir: Path) -> Path | None:
    """Сначала MP3 (крупнейший), иначе любой аудиофайл — дляfallback-конвертации."""
    mp3s = [p for p in Path(outdir).rglob("*.mp3") if p.is_file()]
    if mp3s:
        return max(mp3s, key=lambda p: p.stat().st_size)
    audio_exts = {".m4a", ".webm", ".opus", ".ogg", ".mp4", ".aac", ".wav", ".flac"}
    others = [p for p in Path(outdir).rglob("*") if p.is_file() and p.suffix.lower() in audio_exts]
    return max(others, key=lambda p: p.stat().st_size) if others else None


def _convert_with_ffmpeg(src: Path, outdir: Path, kbps: int) -> Path | None:
    """Fallback: конвертируем скачанный аудиофайл в MP3 руками."""
    dst = src.with_name(src.stem + "_m.mp3")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-vn", "-b:a", f"{kbps}k", str(dst)],
            capture_output=True, timeout=300,
        )
    except Exception:
        return None
    if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
        return dst
    return None


def download(track_url: str, bitrate: int, override: dict | None, prog: dict, outdir: str) -> Path:
    """Скачивает лучший аудиопоток и превращает в MP3; следит за prog['cancel']."""

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

    pps = [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)},
    ]
    if override:
        md = {}
        if override.get("title"):
            md["title"] = override["title"]
        if override.get("artist"):
            md["artist"] = override["artist"]
        if md:
            pps.append({"key": "FFmpegMetadata", "metadata": md})
    pps.append({"key": "EmbedThumbnail"})  # вшивает обложку в MP3 (mutagen)

    opts = {
        **BASE_OPTS,
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "format": "ba/b",
        "writethumbnail": True,          # файл обложки для EmbedThumbnail
        "postprocessors": pps,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(track_url, download=True)
    except Cancelled:
        raise
    except yt_dlp.utils.DownloadError as e:
        if prog.get("cancel"):
            raise Cancelled() from e
        raise

    final = _find_final_file(Path(outdir))
    if final and final.suffix.lower() != ".mp3":
        final = _convert_with_ffmpeg(final, Path(outdir), bitrate) or final
    if not final:
        raise FileNotFoundError("Скачивание не дало файлов")
    return final


def fetch_thumb_bytes(info: dict, extra_url: str | None = None) -> bytes | None:
    """Обложка в память: сначала cover_url из источника, потом превью yt-dlp."""
    for url in (extra_url, info.get("thumbnail")):
        if url:
            data = http_get(url)
            if data:
                return data
    for f in reversed(info.get("formats") or []):
        for t in (f.get("thumbnails") or []):
            if t.get("url"):
                data = http_get(t["url"])
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


def bitrate_menu(item: dict) -> InlineKeyboardMarkup:
    dur = item.get("duration") or 0
    rows = [[
        InlineKeyboardButton(
            text="✨ Авто" + (f" · {fmt_size(size_for(dur, pick_auto(dur)))}" if dur else ""),
            callback_data="b:auto",
        ),
    ]]
    labels = [
        (320, "🔥 320 кбит/с"),
        (256, "✨ 256 кбит/с"),
        (192, "🎚 192 кбит/с"),
        (128, "📻 128 кбит/с"),
    ]
    quality_rows, row = [], []
    for kbps, label in labels:
        size = size_for(dur, kbps)
        if dur and size > BUDGET * 1.04:
            note = ">50 МБ ⚠️"
        elif dur:
            note = f"~{fmt_size(size)}"
        else:
            note = "размер неизвестен"
        row.append(InlineKeyboardButton(text=f"{label} · {note}", callback_data=f"b:{kbps}"))
        if len(row) == 2:
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
    bitrate: int,
    prog: dict,
) -> None:
    url = item["fetch_url"]
    platform = item.get("platform") or "🎧"
    title = item.get("title") or "трек"
    artist = item.get("artist") or ""

    status = await bot.send_message(chat_id, "⏳ Готовлюсь…", reply_markup=cancel_kb())
    outdir = Path(tempfile.mkdtemp(prefix="musicbot_"))
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
        print(f"[dbg] chat {chat_id}: {platform} старт скачивания b={bitrate}", flush=True)
        path = await asyncio.wait_for(
            asyncio.to_thread(download, url, bitrate, item.get("override"), prog, str(outdir)),
            timeout=DOWNLOAD_TIMEOUT,
        )
        poll_task.cancel()
        if prog.get("cancel"):
            raise Cancelled()
        size = path.stat().st_size

        # не влез в лимит → пересобираем на 128
        if size > BUDGET * 1.04 and bitrate != 128:
            await safe_edit(
                bot, chat_id, status.message_id,
                "📉 Не влез в лимит Telegram — пересобираю на 128 кбит/с…",
            )
            prog.update({"downloaded": 0, "total": 0, "speed": 0, "phase": None})
            path = await asyncio.wait_for(
                asyncio.to_thread(download, url, 128, item.get("override"), prog, str(outdir)),
                timeout=DOWNLOAD_TIMEOUT,
            )
            size = path.stat().st_size

        print(f"[dbg] chat {chat_id}: скачано {fmt_size(size)} за {time.time() - t0:.1f} с", flush=True)

        if size > 50 * 1024 * 1024:
            msg = (
                "📦 Даже на 128 кбит/с файл больше 50 МБ.\n"
                "Трек слишком длинный — пришли ссылку на конкретную песню 🙂"
            )
            await safe_edit(bot, chat_id, status.message_id, msg)
            return

        await safe_edit(bot, chat_id, status.message_id, "📤 Загружаю в Telegram…")
        t1 = time.time()

        safe_title = html.escape((title or "Без названия")[:100])
        head = f"🎧 <b>{safe_title}</b>"
        sub = []
        if artist:
            sub.append("👤 " + html.escape(str(artist)[:80]))
        sub.append(f"⏱ {fmt_dur(item.get('duration'))}")
        sub.append(f"MP3 {bitrate}")
        sub.append(f"{fmt_size(size)}")
        sub.append(platform)
        caption = head + "\n" + " · ".join(sub)

        extra = (item.get("override") or {}).get("credit")
        if extra:
            caption += f"\n<small>{html.escape(extra)}</small>"

        thumb_files = None
        thumb_bytes = item.get("thumb_bytes")
        if thumb_bytes:
            thumb_files = BufferedInputFile(thumb_bytes, "cover.jpg")

        async def send_file() -> None:
            await bot.send_audio(
                chat_id,
                FSInputFile(str(path)),
                caption=caption,
                title=(title or "Без названия")[:100],
                performer=(artist or "")[:100] or None,
                duration=int(item.get("duration") or 0),
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
            "⏰ Не уложился в разумное время и остановился.\nПопробуй битрейт поменьше.",
        )
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        print(f"[dbg] chat {chat_id}: DownloadError: {err[:500]}", flush=True)
        if "ничего не нашлось" in err:
            text = "🤷 Ничего не нашёл. Попробуй другое название."
        elif "video unavailable" in err or "removed" in err or "not available" in err.lower():
            text = "🗑 Трек уже недоступен или удалён."
        elif "unsupported URL" in err:
            text = ("🎧 Музыку VK иногда не отдают без авторизации.\n"
                    "Пришли ссылку на <b>видео</b> с этой музыкой или просто <b>название песни</b>.")
        elif "is too large" in err or "file is larger" in err:
            text = "📦 Больше лимита даже на 128 кбит/с — трек слишком длинный."
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


# ---------- сборка item из probe ----------

def build_item(info: dict, url: str, platform: str, override: dict | None) -> dict:
    uploader = _clean_artist(info.get("uploader") or "")
    artist = (override or {}).get("artist") or uploader
    title = (override or {}).get("title") or info.get("title") or "трек"
    cp_url = (override or {}).get("cover_url")
    return {
        "url": url,
        "fetch_url": yt_page_url(info) if platform == "🔎 Поиск" else url,
        "platform": platform,
        "title": title,
        "artist": artist,
        "duration": info.get("duration"),
        "thumb_bytes": fetch_thumb_bytes(info, cp_url),
        "override": override,
        "ts": time.time(),
    }


# ---------- хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


async def send_menu(bot: Bot, chat_id: int, item: dict) -> None:
    head = f"<b>{html.escape((item.get('title') or 'Трек')[:120])}</b>"
    meta = f"👤 {html.escape(item.get('artist') or 'исполнитель')} · ⏱ {fmt_dur(item.get('duration'))}"
    footer = "<b>Выбери битрейт:</b>"
    caption_text = f"{head}\n{meta}\n\n{footer}"

    thumb = item.get("thumb_bytes")
    if thumb:
        try:
            await bot.send_photo(
                chat_id,
                BufferedInputFile(thumb, "cover.jpg"),
                caption=caption_text,
                reply_markup=bitrate_menu(item),
            )
            return
        except Exception:
            pass
    await bot.send_message(chat_id, caption_text, reply_markup=bitrate_menu(item))


@dp.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    text = (message.text or "").strip()
    found = first_url(text)

    if found is None and has_foreign_url(text):
        for raw in URL_RE.findall(text):
            url_raw = raw.rstrip(".,;:!?)]}\"'")
            kind_raw = classify(url_raw)
            if kind_raw and kind_raw.endswith(":other"):
                await message.answer(NOT_TRACK[kind_raw])
                return
        await message.answer(UNSUPPORTED)
        return

    if found:
        url, kind = found
        thinking = await bot.send_message(message.chat.id, "🔎 Изучаю ссылку…")
        try:
            override: dict | None = None
            if kind == "spotify":
                override = await asyncio.wait_for(asyncio.to_thread(resolve_spotify, url), timeout=30)
                print(f"[dbg] chat {message.chat.id}: spotify → «{override['title']}»", flush=True)
            elif kind == "yandex":
                override = await asyncio.wait_for(asyncio.to_thread(resolve_yandex, url), timeout=30)
                print(f"[dbg] chat {message.chat.id}: yandex → «{override['title']}» ({override['artist']})", flush=True)
            if override:
                override["credit"] = (
                    "⚖️ Название и обложка из Spotify · звук — YouTube Music"
                    if kind == "spotify" else
                    "⚖️ Название и артист из Яндекс Музыки · звук — YouTube Music"
                )
                info = await asyncio.wait_for(
                    asyncio.to_thread(search_probe, f"{override['artist']} {override['title']}".strip()),
                    timeout=PROBE_TIMEOUT,
                )
            else:
                info = await asyncio.wait_for(asyncio.to_thread(probe, url), timeout=PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            await thinking.edit_text("⏰ Источник не отвечает. Попробуй позже.")
            return
        except RuntimeError as e:
            await thinking.edit_text("😕 Не прочитал трек по этой ссылке. Попробуй название песни.")
            print(f"[dbg] chat {message.chat.id}: resolve err: {e}", flush=True)
            return
        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            print(f"[dbg] chat {message.chat.id}: probe err: {err[:300]}", flush=True)
            if kind == "vk" and ("unsupported" in err or "login" in err.lower()):
                await thinking.edit_text(VK_AUDIO_HINT)
            elif "ничего не нашлось" in err:
                await thinking.edit_text("🤷 Ничего не нашёл по этой песне. Попробуй другое название.")
            else:
                await thinking.edit_text("😕 Ссылка не открылась. Проверь адрес и повтори.")
            return
        except Exception:
            import traceback
            traceback.print_exc()
            await thinking.edit_text("😕 Ссылка не открылась. Проверь адрес и повтори.")
            return

        item = build_item(info, url, platform_of(url), override)
        pending[message.chat.id] = item
        try:
            await thinking.delete()
        except Exception:
            pass
        await send_menu(bot, message.chat.id, item)
        return

    # нет ссылки: приветствие или поиск по названию
    if text.lower() in GREETINGS:
        await message.answer(GREETING)
        return
    if len(text) < 2:
        await message.answer(PLACEHOLDER)
        return

    await run_search(message, bot, text[:100])


async def run_search(message: Message, bot: Bot, query: str) -> None:
    thinking = await message.answer("🔎 Ищу трек…")
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(search_probe, query),
            timeout=PROBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await thinking.edit_text("⏰ Поиск не отвечает. Попробуй позже.")
        return
    except yt_dlp.utils.DownloadError:
        await thinking.edit_text("🤷 Ничего не нашёл. Попробуй другую формулировку.")
        return
    except Exception:
        import traceback
        traceback.print_exc()
        await thinking.edit_text("😕 Поиск не сработал. Попробуй ещё раз.")
        return

    item = build_item(info, yt_page_url(info), "🔎 Поиск", None)
    pending[message.chat.id] = item
    try:
        await thinking.delete()
    except Exception:
        pass
    await send_menu(bot, message.chat.id, item)


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb, bot: Bot) -> None:
    prog = _active_prog.get(cb.message.chat.id)
    if prog:
        prog["cancel"] = True
        await cb.answer("🛑 Останавливаю…")
    else:
        await cb.answer()


@dp.callback_query(F.data.startswith("b:"))
async def cb_bitrate(cb, bot: Bot) -> None:
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
    bitrate = pick_auto(item.get("duration") or 0) if mode == "auto" else int(mode)
    print(f"[dbg] chat {chat_id}: выбран битрейт {bitrate}", flush=True)
    await run_download(bot, chat_id, item, bitrate, prog)


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
        if http_get(url):
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
