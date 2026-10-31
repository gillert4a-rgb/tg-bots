FROM python:3.12-slim

# ffmpeg — конвертация, deno — нужен свежему yt-dlp для YouTube
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && deno --version

# системный конфиг yt-dlp: разрешаем удалённые компоненты (ejs, YouTube)
RUN mkdir -p /etc/yt-dlp && echo "--remote-components ejs:github" > /etc/yt-dlp/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# токены приходят из переменных окружения Render (VIDEO_BOT_TOKEN, MUSIC_BOT_TOKEN)
CMD ["python", "-u", "runner.py"]
