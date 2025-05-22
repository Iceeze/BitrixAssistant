import asyncio
import uvicorn
from api import app
from bot import dp, bot
from db import init_db, close_db


async def main():
    await init_db()

    # Конфигурируем и запускаем uvicorn — ASGI сервер для FastAPI
    config = uvicorn.Config(app=app, host="0.0.0.0", port=5000, log_level="info")
    server = uvicorn.Server(config)

    # Параллельно запускаем FastAPI (uvicorn) и поллинг Telegram-бота
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Закрываем пул подключений
        asyncio.run(close_db())