"""Render free tier: оба бота в одном контейнере.

Render даёт один бесплатный инстанс — запускаем в нём обоих ботов как две
asyncio-задачи. Health-HTTP у каждого свой (первый занимает порт, второй
тихо мимо — он уже защищён try/except).
"""
import asyncio

import musicbot_bot
import videobot_bot


async def main() -> None:
    await asyncio.gather(
        videobot_bot.main(),
        musicbot_bot.main(),
    )


if __name__ == "__main__":
    asyncio.run(main())
