from typing import Optional
from asyncpg import create_pool

from config import *

pool = None  # Глобальная переменная для пула подключений к базе данных


class Database:
    _pool = None

    @classmethod
    async def get_pool(cls):
        if cls._pool is None:
            cls._pool = await create_pool(
                DATABASE_URL,
                min_size=5,
                max_size=20,
                command_timeout=60
            )
        return cls._pool

    @classmethod
    async def close(cls):
        if cls._pool:
            await cls._pool.close()
            cls._pool = None


async def get_user(chat_id: int) -> Optional[dict]:
    """Получает пользователя из базы данных по chat_id."""
    pool = await Database.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE chat_id = $1", chat_id)
        return dict(row) if row else None


async def save_user(user_data: dict):
    """Сохраняет или обновляет данные пользователя в таблице users."""
    pool = await Database.get_pool()

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (chat_id, access_token, refresh_token, expires, domain, 
                             member_id, user_id, user_name, is_admin)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (chat_id) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                expires = EXCLUDED.expires,
                domain = EXCLUDED.domain,
                member_id = EXCLUDED.member_id,
                user_id = EXCLUDED.user_id,
                user_name = EXCLUDED.user_name,
                is_admin = EXCLUDED.is_admin
        """,
                           user_data['chat_id'],
                           user_data['access_token'],
                           user_data['refresh_token'],
                           user_data['expires'],
                           user_data['domain'],
                           user_data['member_id'],
                           user_data['user_id'],
                           user_data['user_name'],
                           user_data['is_admin'])


async def get_notification_settings(chat_id: int) -> dict:
    """Получает настройки уведомлений для пользователя.
    Если не существует — создаёт запись с настройками по умолчанию."""
    pool = await Database.get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM notification_settings WHERE chat_id = $1",
            chat_id
        )

        if row:
            return dict(row)

        # Создаем запись по умолчанию
        await conn.execute(
            "INSERT INTO notification_settings (chat_id) VALUES ($1)",
            chat_id
        )
        return {
            'new_deals': True,
            'deal_updates': True,
            'task_creations': True,
            'task_updates': True,
            'comments': True
        }


async def update_notification_setting(chat_id: int, setting: str, value: bool):
    """
        Обновляет конкретную настройку уведомлений пользователя.

        :param chat_id: Telegram chat ID пользователя
        :param setting: Название поля настройки
        :param value: Новое значение (True/False)
        """
    pool = await Database.get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE notification_settings SET {setting} = $1 WHERE chat_id = $2",
            value,
            chat_id
        )


async def delete_user(chat_id: int):
    """
        Удаляет пользователя из базы данных по chat_id.
        Для рефреш токен.
        :param chat_id: Telegram chat ID пользователя
        """
    pool = await Database.get_pool()

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE chat_id = $1", chat_id)
