import asyncio
import uvicorn
from api import app
from bot import dp, bot
from db import Database


async def main():
    await Database.get_pool()

    # Конфигурируем и запускаем uvicorn — ASGI сервер для FastAPI
    config = uvicorn.Config(app=app, host="0.0.0.0", port=5000, log_level="info")
    server = uvicorn.Server(config)

    # Параллельно запускаем FastAPI (uvicorn) и поллинг Telegram-бота
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(Database.close())