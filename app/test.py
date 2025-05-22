import asyncio
import json
import logging
from time import time
from typing import Dict, Optional
from collections import defaultdict
from datetime import datetime
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import asyncpg
from asyncpg import create_pool
from aiogram.fsm.context import FSMContext


DATABASE_URL = "postgresql://botuser:123456789@localhost/bitrixbot"

# BITRIX_CLIENT_ID = "local.68187191a08683.25172914"  # client_id Данила
BITRIX_CLIENT_ID = "local.682b075811e9c7.97053039"  # client_id Ильгиза

# BITRIX_CLIENT_SECRET = "46wPWoUU1YLv5d86ozDh7FbhODOi2L2mlmNBWweaA6jNxV2xX1"  # client_secret Данила
BITRIX_CLIENT_SECRET = "1G4LgG178KbNUuuTiFjMPVjQlh1kSLyLSsSieuTfbFk0CHQRCA"  # client_secret Ильгиза

REDIRECT_URI = "https://mybitrixbot.ru/callback"
WEBHOOK_DOMAIN = "https://mybitrixbot.ru"
TELEGRAM_TOKEN = "8179379861:AAEoKsITnDaREJINuHJu4qXONwxTIlSncxc"

# BITRIX_DOMAIN = "b24-rqyyhh.bitrix24.ru"  # Домен портала Битрикс24 Данила
BITRIX_DOMAIN = "b24-eu9n9c.bitrix24.ru"  # Домен портала Битрикс24 Ильгиза

is_registered_events: Dict[str, bool] = {}

member_map: Dict[str, set[str]] = defaultdict(set)  # ключ — это member_id портала, а значение — set чат‑ID

# Базовая конфигурация логирования для всего приложения
logging.basicConfig(level=logging.INFO)

# Инициализация компонентов FastAPI и Telegram-бота на Aiogram
app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class NotificationSettings(StatesGroup):
    """Состояния для настройки уведомлений."""

    waiting_action = State()  # Ожидание выбора действия от пользователя

class TaskHistoryStates(StatesGroup):
    """Состояния для вывода истории изменений задачи."""
    waiting_for_task_id = State()


async def get_user(chat_id: int) -> Optional[dict]:
    """Получает пользователя из базы данных по chat_id."""
    if not pool:
        raise RuntimeError("Database pool is not initialized")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE chat_id = $1", chat_id)
        return dict(row) if row else None


async def save_user(user_data: dict):
    """Сохраняет или обновляет данные пользователя в таблице users."""
    if not pool:
        raise RuntimeError("Database pool is not initialized")

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
    if not pool:
        raise RuntimeError("Database pool is not initialized")

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
    if not pool:
        raise RuntimeError("Database pool is not initialized")

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
    if not pool:
        raise RuntimeError("Database pool is not initialized")

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE chat_id = $1", chat_id)


# --- Вспомогательные функции ---
async def refresh_token(chat_id: str) -> bool:
    """Обновление access token, используя refresh token"""
    user_data = await get_user(int(chat_id))
    if not user_data:
        return False

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth.bitrix.info/oauth/token/",
                data={
                    "grant_type": "refresh_token",
                    "client_id": BITRIX_CLIENT_ID,
                    "client_secret": BITRIX_CLIENT_SECRET,
                    "refresh_token": user_data["refresh_token"]
                }
            )
            resp.raise_for_status()
            data = resp.json()

            new_data = {
                "chat_id": int(chat_id),
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "expires": int(time()) + int(data["expires_in"]),
                "domain": user_data["domain"],
                "member_id": user_data["member_id"],
                "user_id": user_data["user_id"],
                "user_name": user_data["user_name"],
                "is_admin": user_data["is_admin"]
            }
            await save_user(new_data)
            return True
    except httpx.HTTPStatusError as e:
        if "invalid_grant" in str(e):
            await bot.send_message(chat_id, "❌ Сессия истекла, выполните /start")
            await delete_user(int(chat_id))
        return False


async def get_user_info(domain: str, access_token: str) -> Dict:
    """Получение информации о пользователе, включая роли"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{domain}/rest/profile.json",
            params={"auth": access_token}
        )
        data = resp.json()
        data = data.get("result", {})

        # logging.info(f"Data User Get: {data}") # Логи

        return {
            "id": data.get("ID"),
            "is_admin": data.get("ADMIN"),
            "name": f"{data.get('NAME')} {data.get('LAST_NAME')}".strip(),
        }


async def get_user_name(domain: str, access_token: str, user_id: int) -> str:
    """Получение имени пользователя по ID"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{domain}/rest/user.get.json",
                params={
                    "auth": access_token,
                    "ID": user_id
                }
            )
            user_data = resp.json().get('result', [{}])[0]
            return f"{user_data.get('NAME', '')} {user_data.get('LAST_NAME', '')}".strip() or "Неизвестный"
    except Exception as e:
        logging.error(f"Error getting user name: {e}")
        return "Неизвестный"


async def check_user_exists(domain: str, access_token: str, user_id: int) -> bool:
    """Проверка, существует ли пользователь на портале Битрикс24"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{domain}/rest/user.get.json",
            params={"auth": access_token, "ID": user_id}
        )
        data = resp.json()
        return data.get("result") is not None


async def register_webhooks(domain: str, access_token: str):
    """
    1) Для каждого события вызываем event.get, чтобы получить список handler’ов
    2) Удаляем каждый из них через event.unbind (по event + handler)
    3) Ждём, пока всё отвязалось, и вешаем ровно по одному через event.bind
    """
    events = [
        "OnTaskAdd", "OnTaskUpdate", "OnTaskCommentAdd",
        "OnCrmDealAdd", "OnCrmDealUpdate"
    ]

    async with httpx.AsyncClient() as client:
        for event in events:
            # 1) Получаем все текущие handler’ы для event
            resp_get = await client.post(
                f"https://{domain}/rest/event.get",
                data={
                    "event": event,
                    "auth": access_token
                }
            )
            resp_get.raise_for_status()
            handlers = resp_get.json().get("result", [])

            # 2) Удаляем каждый handler
            for h in handlers:
                handler_url = h.get("handler")
                try:
                    resp_un = await client.post(
                        f"https://{domain}/rest/event.unbind",
                        data={
                            "event": event,
                            "handler": handler_url,
                            "auth": access_token
                        }
                    )
                    resp_un.raise_for_status()
                    logging.info(f"Unbound {event} → {handler_url}")
                except Exception as e:
                    logging.warning(f"Failed to unbind {event} → {handler_url}: {e}")

        # 3) Привязываем заново по одному
        for event in events:
            try:
                resp_bind = await client.post(
                    f"https://{domain}/rest/event.bind",
                    data={
                        "event": event,
                        "handler": f"{WEBHOOK_DOMAIN}/callback",
                        "auth": access_token
                    }
                )
                resp_bind.raise_for_status()
                logging.info(f"Bound {event} → {WEBHOOK_DOMAIN}/callback")
            except Exception as e:
                logging.error(f"Failed to bind {event}: {e}")


def parse_form_data(form_data: dict) -> dict:
    """Парсинг полученных данных из Битрикса"""
    result = {}
    for key, value in form_data.items():
        parts = key.split('[')
        current = result
        for part in parts[:-1]:
            part = part.rstrip(']')
            if part not in current:
                current[part] = {}
            current = current[part]
        last_part = parts[-1].rstrip(']')
        current[last_part] = value
    return result


# --- API ---
@app.api_route("/callback", methods=["GET", "POST", "HEAD"])
async def unified_handler(request: Request):
    """Обработка запросов Битрикс"""
    if request.method == "GET":  # регистрация
        return await handle_oauth_callback(request)
    elif request.method == "POST":  # обработка событий
        return await handle_webhook_event(request)
    return JSONResponse({"status": "ok"})


async def handle_oauth_callback(request: Request):
    """Авторизация OAuth 2.0"""
    params = dict(request.query_params)
    #logging.info(f"OAuth callback params: {params}")  # Логи
    domain = params['domain']

    global is_registered_events

    try:
        required = ["code", "state", "domain"]
        if missing := [key for key in required if key not in params]:
            raise HTTPException(400, f"Missing params: {missing}")

        chat_id = int(params["state"])

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{params['domain']}/oauth/token/",
                data={
                    "grant_type": "authorization_code",
                    "code": params["code"],
                    "client_id": BITRIX_CLIENT_ID,
                    "client_secret": BITRIX_CLIENT_SECRET,
                    "redirect_uri": REDIRECT_URI
                }
            )
            token_data = resp.json()

        if not is_registered_events.get(domain, False):
            try:
                await register_webhooks(domain=domain, access_token=token_data['access_token'])
                is_registered_events[domain] = True
            except Exception as e:
                logging.error(f"Webhook registration failed for {domain}: {e}")

        member_id = params.get("member_id")
        if member_id:
            member_map[member_id].add(chat_id)

        user_info = await get_user_info(params['domain'], token_data['access_token'])

        # Сохранение в PostgreSQL
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            # Сохраняем пользователя
            await conn.execute("""
                INSERT INTO users (
                    chat_id, access_token, refresh_token, expires,
                    domain, member_id, user_id, user_name, is_admin
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
                               chat_id,
                               token_data["access_token"],
                               token_data["refresh_token"],
                               int(time()) + int(token_data["expires_in"]),
                               params["domain"],
                               params.get("member_id", ""),
                               int(user_info["id"]),
                               user_info["name"],
                               user_info["is_admin"]
                               )

            # Создаем настройки по умолчанию
            await conn.execute("""
                INSERT INTO notification_settings (chat_id)
                VALUES ($1)
                ON CONFLICT (chat_id) DO NOTHING
            """, chat_id)
        finally:
            await conn.close()

        await bot.send_message(chat_id, "✅ Авторизация успешна!")
        return HTMLResponse("""
            <html><head><meta charset='utf-8'><title>Авторизация</title>
            <style>
            body { display:flex; justify-content:center; align-items:center; height:100vh; background:#f0f0f0; font-family:Arial,sans-serif; }
            .card { background:white; padding:2em; border-radius:8px; box-shadow:0 2px  6px rgba(0,0,0,0.2); text-align:center; }
            h1 { color:#4caf50; }
            </style>
            </head><body><div class='card'>
            <h1>✅ Авторизация успешна!</h1>
            <p>Закройте это окно и вернитесь в Telegram.</p>
            </div></body></html>
            """)

    except Exception as e:
        logging.error(f"OAuth error: {str(e)}")
        raise HTTPException(500, "Internal error")


async def handle_webhook_event(request: Request):
    """Обработка событий"""
    try:
        form_data = await request.form()
        parsed_data = parse_form_data(dict(form_data))

        #logging.info(f"Parsed webhook data: {json.dumps(parsed_data, indent=2)}")  # Логи

        auth_data = parsed_data.get('auth', {})
        event = parsed_data.get('event', '').lower()
        member_id = auth_data.get('member_id')

        if not member_id:
            return JSONResponse({"status": "invalid_member_id"}, status_code=400)

        chat_ids = member_map.get(member_id, set())
        if not chat_ids:
            logging.error(f"Member ID {member_id} not mapped to any chat")
            return JSONResponse({"status": "member_not_found"}, status_code=404)

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            for chat_id in chat_ids:
                # Получаем данные пользователя из БД
                user_data = await conn.fetchrow(
                    "SELECT * FROM users WHERE chat_id = $1",
                    int(chat_id)
                )

                if not user_data:
                    logging.error(f"User data not found for chat {chat_id}")
                    continue

                user_data = dict(user_data)
                logging.info(f"Sending to chat {chat_id} with token expires at {user_data['expires']}")

                # Проверяем срок действия токена
                if time() > user_data["expires"]:
                    if not await refresh_token(chat_id):
                        logging.error(f"Token refresh failed for chat {chat_id}")
                        continue

                # Получаем настройки уведомлений
                settings = await conn.fetchrow(
                    "SELECT * FROM notification_settings WHERE chat_id = $1",
                    int(chat_id)
                )
                settings = dict(settings) if settings else {
                    'new_deals': True,
                    'deal_updates': True,
                    'task_creations': True,
                    'task_updates': True,
                    'comments': True
                }

                # Проверяем настройки уведомлений
                event_handlers = {
                    "oncrmdealadd": settings['new_deals'],
                    "oncrmdealupdate": settings['deal_updates'],
                    "ontaskadd": settings['task_creations'],
                    "ontaskupdate": settings['task_updates'],
                    "ontaskcommentadd": settings['comments']
                }

                event_type = event.split('_')[0]  # Для обработки составных событий
                if not event_handlers.get(event_type, True):
                    continue

                # Обработка событий
                if event.startswith("ontaskcomment"):
                    await process_comment_event(event, parsed_data, user_data, chat_id)
                elif event.startswith("ontask"):
                    await process_task_event(event, parsed_data, user_data, chat_id)
                elif event.startswith("oncrmdeal"):
                    await process_deal_event(event, parsed_data, user_data, chat_id)

        finally:
            await conn.close()

        return JSONResponse({"status": "ok"})

    except Exception as e:
        logging.error(f"Webhook handler error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def process_task_event(event: str, data: dict, user_data: dict, chat_id: str):
    """Получение уведомлений о задачах из Битрикса"""
    try:
        task_id = None
        #logging.info(f"data: {data}")  # Логи

        if event != "ontaskdelete":
            task_id = data.get('data', {}).get('FIELDS_AFTER', {}).get('ID')
            if not task_id and event:
                logging.error("No task ID in webhook data")
                return

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://{user_data['domain']}/rest/tasks.task.get",
                    params={
                        "taskId": task_id,
                        "auth": user_data["access_token"]
                    }
                )
                resp.raise_for_status()
                task_data = resp.json()

                if 'error' in task_data:
                    logging.error(f"Bitrix API error: {task_data['error_description']}")
                    return

                task = task_data.get('result', {}).get('task', {})

                #logging.info(f"Task data: {task}")  # Логи

        message = ""
        responsible_id = None

        status_map = {
            '2': "🆕 Ждет выполнения",
            '3': "🔄 Выполняется",
            '4': "⏳ Ожидает контроля",
            '5': "✅ Завершена",
            '6': "⏸ Отложена"
        }

        priority_map = {
            '0': "Низкий",
            '1': "Средний",
            '2': "Высокий"
        }

        title = task.get('title', 'Без названия')
        description = task.get('description', 'Отсутствует')
        priority_code = task.get('priority')
        priority = priority_map.get(priority_code)
        status_code = task.get('status')
        status = status_map.get(status_code, f"Неизвестный статус ({status_code})")
        responsible_id = task.get('responsibleId')
        creator_name = task.get('creator').get('name')
        responsible_name = task.get('responsible').get('name')
        deadline = task.get('deadline')
        user_id = user_data["user_id"]

        deadline_str = deadline
        if deadline:
            try:
                deadline_date = datetime.strptime(deadline, "%Y-%m-%dT%H:%M:%S%z")  # Добавляем обработку часового пояса
                deadline_str = deadline_date.strftime("%Y-%m-%d %H:%M")  # Новый формат
            except Exception as e:
                logging.error(f"Ошибка обработки даты: {deadline}")

        if event == "ontaskadd":
            message = (
                f"Задача <b><a href='https://{BITRIX_DOMAIN}/company/personal/user/{user_id}/tasks/task/view/{task_id}/'>№{task_id}</a></b> - 🆕Создана🆕\n"
                f"📌Название: {title}\n"
                f"📝Описание: {description}\n"
                f"🚨Приоритет: {priority}\n"
                f"📊Cтатус: {status}\n"
                f"⏰Срок исполнения: {deadline_str}\n"
                f"👤Постановщик: {creator_name}\n"
                f"👤Исполнитель: {responsible_name}"
            )
        elif event == "ontaskupdate":
            changed_by_id = task.get('changedBy')

            changed_by_name = await get_user_name(
                domain=user_data['domain'],
                access_token=user_data["access_token"],
                user_id=changed_by_id
            )

            message = (
                f"Задача <b><a href='https://{BITRIX_DOMAIN}/company/personal/user/{user_id}/tasks/task/view/{task_id}/'>№{task_id}</a></b> - 🔄Изменена🔄\n"
                f"📌Название: {title}\n"
                f"📝Описание: {description}\n"
                f"🚨Приоритет: {priority}\n"
                f"📊Cтатус: {status}\n"
                f"⏰Срок исполнения: {deadline_str}\n"
                f"👤Постановщик: {creator_name}\n"
                f"👤Исполнитель: {responsible_name}\n"
                f"👤Кто изменил: {changed_by_name}"
            )
        if responsible_id:
            if not (str(user_data.get('user_id')) == str(responsible_id) or user_data.get('is_admin')):
                return
        await bot.send_message(chat_id, message)
    except httpx.HTTPStatusError as e:
        logging.error(f"API request failed: {e.response.text}")
    except Exception as e:
        logging.error(f"Task processing error: {e}")

async def process_deal_event(event: str, data: dict, user_data: dict, chat_id: str):
    """Получение уведомлений о сделках из Битрикса"""
    try:
        responsible_id = None
        message = ""
        deal = {}
        domain = user_data['domain']
        user_id = user_data["user_id"]

        if event != "oncrmdealdelete":
            deal_id = data.get('data', {}).get('FIELDS', {}).get('ID')
            if not deal_id:
                return

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://{domain}/rest/crm.deal.get",
                    params={
                        "id": deal_id,
                        "auth": user_data["access_token"]
                    }
                )
                deal = resp.json().get("result", {})
                responsible_id = deal.get('ASSIGNED_BY_ID')

            # Получение имен
            responsible_name = await get_user_name(
                domain=domain,
                access_token=user_data["access_token"],
                user_id=responsible_id
            ) if responsible_id else "Не указан"

            changed_by_id = deal.get('MODIFY_BY_ID') or deal.get('MODIFIED_BY_ID')
            changed_by_name = await get_user_name(
                domain=domain,
                access_token=user_data["access_token"],
                user_id=changed_by_id
            ) if changed_by_id else "Неизвестно"

            # Формирование сообщения
            deal_url = f"https://{domain}/crm/deal/details/{deal_id}/"
            title = deal.get('TITLE', 'Без названия')
            address = deal.get('COMMENTS', 'Не указано')
            stage = deal.get('STAGE_ID', 'Неизвестно')

            if event == "oncrmdealadd":
                message = (
                    f"Сделка <b><a href='{deal_url}'>№{deal_id}</a></b> - 🆕Создана🆕\n"
                    f"🏢 Название: {title}\n"
                    f"📍 Адрес: {address}\n"
                    f"📈 Стадия: {stage}\n"
                    f"👤 Ответственный: {responsible_name}"
                )
            elif event == "oncrmdealupdate":
                message = (
                    f"Сделка <b><a href='{deal_url}'>№{deal_id}</a></b> - 🔄Изменена🔄\n"
                    f"🏢 Название: {title}\n"
                    f"📍 Адрес: {address}\n"
                    f"📈 Стадия: {stage}\n"
                    f"👤 Ответственный: {responsible_name}\n"
                    f"✍️ Изменено: {changed_by_name}"
                )

            # logging.info(f"Deal data: {deal}")  # Логи

        if responsible_id:
            if str(user_data.get('user_id')) == str(responsible_id) or user_data.get('is_admin'):
                await bot.send_message(chat_id, message, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Ошибка обработки сделки: {e}")

async def process_comment_event(event: str, data: dict, user_data: dict, chat_id: str):
    """Обработка комментариев к задачам из Битрикса"""
    settings = await get_notification_settings(chat_id)
    if not settings['comments']:
        logging.info("Comments notifications are disabled")
        return
    try:
        comment_data = data.get('data', {}).get('FIELDS_AFTER')
        # logging.info(f"Comment data: {comment_data}") # Логи

        comment_id = comment_data.get('ID')
        task_id = comment_data.get('TASK_ID')
        message = ""
        responsible_id = None
        user_id = user_data["user_id"]

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{user_data['domain']}/rest/task.commentitem.get",
                params={
                    "taskId": task_id,
                    "itemId": comment_id,
                    "auth": user_data["access_token"]
                }
            )
            comment = resp.json().get('result', {})
            #logging.info(f"Comment data: {comment}")  # Логи

            author_name = comment.get('AUTHOR_NAME')
            comment_text = comment.get('POST_MESSAGE', '')[:1000]  # Обрезаем длинные сообщения
            comment_date = datetime.strptime(comment['POST_DATE'], "%Y-%m-%dT%H:%M:%S%z").strftime("%Y-%m-%d %H:%M")
            message = (
                f"💬 Новый комментарий к задаче <b><a href='https://{BITRIX_DOMAIN}/company/personal/user/{user_id}/tasks/task/view/{task_id}/'>№{task_id}</a></b>\n"
                f"Автор: {author_name}\n"
                f"Текст: {comment_text}\n"
                f"Дата: {comment_date}\n"
            )

        # Добавляем ответственного
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{user_data['domain']}/rest/tasks.task.get",
                params={
                    "taskId": task_id,
                    "auth": user_data["access_token"]
                }
            )
            task = resp.json().get('result', {}).get('task', {})
            responsible_id = task.get('responsibleId')

        if responsible_id:
            if str(user_data.get('user_id')) == str(responsible_id) or user_data.get('is_admin'):
                await bot.send_message(chat_id, message)

    except Exception as e:
        logging.error(f"Ошибка обработки комментария: {e}")


# --- Telegram Bot ---
@dp.message(Command("start"))
async def cmd_start(m: Message):
    """Запуск бота, формирование ссылки для авторизации"""
    auth_url = (
        f"https://{BITRIX_DOMAIN}/oauth/authorize/"
        f"?client_id={BITRIX_CLIENT_ID}"
        f"&response_type=code"
        f"&state={m.from_user.id}"
        f"&redirect_uri={REDIRECT_URI}"
    )

    message_to_user = (f"Добро пожаловать!\n🔑 Для начала работы BitrixAssistant пройдите авторизацию: {auth_url}")
    await m.answer(message_to_user)


@dp.message(Command("task"))
async def cmd_task(m: Message):
    """Создание задачи"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь: /start")

    try:
        parts = m.text.split(maxsplit=1)[1].split('|')
        parts = [p.strip() for p in parts]

        title = parts[0]
        description = parts[1] if len(parts) > 1 else ""
        responsible_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else user_data["user_id"]
        priority = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        deadline = parts[4] if len(parts) > 4 else None

        if priority not in (0, 1, 2):
            raise ValueError("Приоритет должен быть 0, 1 или 2")

        if not await check_user_exists(user_data["domain"], user_data["access_token"], responsible_id):
            raise ValueError("Пользователь не найден")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{user_data['domain']}/rest/tasks.task.add.json",
                params={"auth": user_data["access_token"]},
                json={
                    "fields": {
                        "TITLE": title,
                        "DESCRIPTION": description,
                        "PRIORITY": priority,
                        "RESPONSIBLE_ID": responsible_id,
                        "DEADLINE": deadline
                    }
                }
            )

            # Расширенная обработка ответа
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                error_text = resp.text[:200]  # Первые 200 символов ответа
                logging.error(f"JSON decode error. Response: {error_text}")
                raise ValueError("Некорректный ответ от сервера Bitrix")

            # Проверка типа данных
            if not isinstance(data, dict):
                logging.error(f"Unexpected response type: {type(data)}. Content: {data}")
                raise ValueError("Ошибка формата ответа")

            # Обработка ошибок API
            if data.get('error'):
                error_msg = data.get('error_description', 'Неизвестная ошибка Bitrix')
                logging.error(f"Bitrix API Error: {error_msg}")
                raise ValueError(error_msg)

            # Получение ID задачи с проверкой структуры
            try:
                task_id = data['result']['task']['id']
            except KeyError:
                logging.error(f"Invalid response structure: {data}")
                raise ValueError("Некорректная структура ответа")

            await m.answer(f"✅ Задача создана! ID: {task_id}")

    except (IndexError, ValueError) as e:
        await m.answer(
            f"❌ Ошибка: {str(e)}\nФормат: /task Название | Описание | [ID_исполнителя] | [Приоритет] | [Срок исполнения]")
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}", exc_info=True)
        await m.answer(f"⚠️ Системная ошибка: {str(e)}")


@dp.message(Command("deal"))
async def cmd_deal(m: Message):
    """Создание сделки: /deal Название ЖК | Адрес | Стадия_ID"""
    user_data = await get_user(m.from_user.id)
    if not user_data or not user_data.get("is_admin"):
        return await m.answer("❗ Требуются права администратора. Авторизуйтесь через /start")

    try:
        parts = m.text.split(maxsplit=1)[1].split('|')
        parts = [p.strip() for p in parts]

        if len(parts) < 3:
            raise ValueError("Недостаточно параметров. Формат: /deal Название ЖК | Адрес | ID_стадии")

        title, address, stage_id = parts[0], parts[1], parts[2]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{user_data['domain']}/rest/crm.deal.add.json",
                params={"auth": user_data["access_token"]},
                json={
                    "fields": {
                        "TITLE": title,
                        "COMMENTS": address,
                        "STAGE_ID": stage_id,
                        "ASSIGNED_BY_ID": user_data["user_id"]
                    }
                }
            )
            data = resp.json()

            if data.get('error'):
                error_msg = data.get('error_description', 'Неизвестная ошибка Bitrix')
                raise ValueError(f"Bitrix API: {error_msg}")

            deal_id = data.get('result')
            await m.answer(f"✅ Сделка создана! ID: {deal_id}")

    except (IndexError, ValueError) as e:
        await m.answer(f"❌ Ошибка: {str(e)}\nФормат: /deal Название ЖК | Адрес | ID_стадии")
    except Exception as e:
        logging.error(f"Ошибка создания сделки: {str(e)}", exc_info=True)
        await m.answer(f"⚠️ Ошибка: {str(e)}")


@dp.message(Command("comment"))
async def cmd_comment(m: Message):
    """Добавить комментарий к задаче: /comment [ID задачи] | Комментарий"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    try:
        # Парсим аргументы
        parts = m.text.split(maxsplit=1)[1].split('|', 1)
        if len(parts) < 2:
            raise ValueError("Неверный формат команды")

        task_id = parts[0].strip()
        comment_text = parts[1].strip()

        # Проверяем доступ к задаче
        async with httpx.AsyncClient() as client:
            # Проверка существования задачи
            task_resp = await client.get(
                f"https://{user_data['domain']}/rest/tasks.task.get.json",
                params={
                    "taskId": task_id,
                    "auth": user_data["access_token"]
                }
            )
            task_data = task_resp.json()
            if 'error' in task_data:
                raise ValueError("Задача не найдена или нет доступа")

            # Отправка комментария
            comment_resp = await client.post(
                f"https://{user_data['domain']}/rest/task.commentitem.add.json",
                params={"auth": user_data["access_token"]},
                json={
                    "TASK_ID": task_id,
                    "fields": {
                        "AUTHOR_ID": user_data["user_id"],
                        "POST_MESSAGE": comment_text
                    }
                }
            )
            comment_data = comment_resp.json()

            if 'error' in comment_data:
                error_msg = comment_data.get('error_description', 'Ошибка добавления комментария')
                raise ValueError(error_msg)

            await m.answer(f"💬 Комментарий добавлен к задаче {task_id}")

    except (IndexError, ValueError) as e:
        await m.answer(f"❌ Ошибка: {str(e)}\nФормат: /comment [ID задачи] | [Текст комментария]")
    except Exception as e:
        logging.error(f"Comment error: {str(e)}", exc_info=True)
        await m.answer(f"⚠️ Ошибка: {str(e)}")


@dp.message(Command("stages"))
async def cmd_stages(m: Message):
    """Получить список стадий сделок"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь: /start")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{user_data['domain']}/rest/crm.dealcategory.stage.list",
                params={"auth": user_data["access_token"]}
            )
            stages = resp.json().get('result', [])

            message = "Доступные стадии:\n"
            for stage in stages:
                message += f"{stage['NAME']} (ID: {stage['STATUS_ID']})\n"

            await m.answer(message)

    except Exception as e:
        await m.answer(f"⚠️ Ошибка: {str(e)}")


@dp.message(Command("employees"))
async def cmd_employees(m: Message):
    """Получить список сотрудников Bitrix24"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{user_data['domain']}/rest/user.get.json",
                params={
                    "auth": user_data["access_token"],
                    "FILTER": {"USER_TYPE": "employee"},
                    "SELECT": ["ID", "NAME", "LAST_NAME"]
                }
            )
            data = resp.json()

            if 'error' in data:
                error_msg = data.get('error_description', 'Неизвестная ошибка')
                return await m.answer(f"❌ Ошибка Bitrix: {error_msg}")

            users = data.get('result', [])
            if not users:
                return await m.answer("🤷 На портале нет сотрудников")

            # Формируем список
            user_list = []
            for user in users:
                user_id = user.get('ID', 'N/A')
                name = f"{user.get('NAME', '')} {user.get('LAST_NAME', '')}".strip()
                user_list.append(f"👤 {name} (ID: {user_id})")

            # Разбиваем на сообщения по 20 пользователей
            chunk_size = 20
            for i in range(0, len(user_list), chunk_size):
                chunk = user_list[i:i + chunk_size]
                await m.answer(
                    "Список сотрудников:\n\n" + "\n".join(chunk),
                    parse_mode="HTML"
                )

    except Exception as e:
        logging.error(f"Employees error: {str(e)}", exc_info=True)
        await m.answer(f"⚠️ Ошибка при получении списка: {str(e)}")


@dp.message(Command("tasks"))
async def cmd_tasks(m: Message):
    """Показать список задач пользователя"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    try:
        user_id = user_data['user_id']

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{user_data['domain']}/rest/tasks.task.list.json",
                params={"auth": user_data["access_token"]},
                json={
                    "order": {"CREATED_DATE": "DESC"},
                    "select": ["ID", "TITLE", "RESPONSIBLE_ID", "CREATED_BY", "STATUS", "DEADLINE"]
                }
            )
            resp.raise_for_status()
            data = resp.json()

            if 'error' in data:
                error_msg = data.get('error_description', 'Неизвестная ошибка')
                raise ValueError(f"Bitrix API: {error_msg}")

            tasks = data.get('result', {}).get('tasks', [])
            if not tasks:
                await m.answer("📭 У вас нет задач.")
                return

            status_map = {
                '2': "🆕 Ждет выполнения",
                '3': "🔄 Выполняется",
                '4': "⏳ Ожидает контроля",
                '5': "✅ Завершена",
                '6': "⏸ Отложена"
            }

            message = ["📋 Список задач:\n"]
            for task in tasks:
                task_id = task.get('id')
                title = task.get('title', 'Без названия')

                task_info = (
                    f"Задача <b><a href='https://{BITRIX_DOMAIN}/company/personal/user/{user_id}/tasks/task/view/{task_id}/'>№{task_id}</a></b>",
                    f"📌 Название: {title}",
                    "―――――――――――――――――――――"
                )
                message.extend(task_info)
            message.append(f"\nПоказано {len(tasks)} задач.")

            await m.answer("\n".join(message))

    except httpx.HTTPStatusError as e:
        logging.error(f"HTTP error: {e.response.text}")
        await m.answer("❌ Ошибка подключения к Bitrix24.")
    except ValueError as e:
        await m.answer(f"❌ {str(e)}")
    except Exception as e:
        logging.error(f"Ошибка в /tasks: {str(e)}", exc_info=True)
        await m.answer("⚠️ Ошибка при получении задач.")


@dp.message(Command("deals"))
async def cmd_deals(m: Message):
    """Показать список сделок"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    try:
        domain = user_data['domain']
        user_id = user_data["user_id"]
        is_admin = user_data.get("is_admin", False)

        # Формируем фильтр в зависимости от прав
        filter_params = {} if is_admin else {"ASSIGNED_BY_ID": user_id}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{domain}/rest/crm.deal.list",
                params={"auth": user_data["access_token"]},
                json={
                    "order": {"DATE_CREATE": "DESC"},
                    "filter": filter_params,
                    "select": ["ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID"]
                }
            )
            data = resp.json()

            if 'error' in data:
                error_msg = data.get('error_description', 'Неизвестная ошибка')
                raise ValueError(f"Bitrix API: {error_msg}")

            deals = data.get('result', [])
            if not deals:
                await m.answer("📭 У вас нет сделок.")
                return

            # Маппинг стадий сделок (дополните при необходимости)
            stage_map = {
                'NEW': '🆕 Новая',
                'PREPARATION': '📝 В работе',
                'CLOSED': '✅ Закрыта',
            }

            message = ["🏢 Список сделок:\n"]
            for deal in deals:
                deal_id = deal.get('ID')
                title = deal.get('TITLE', 'Без названия')
                stage = stage_map.get(deal.get('STAGE_ID'), deal.get('STAGE_ID'))

                deal_url = f"https://{domain}/crm/deal/details/{deal_id}/"
                message.append(
                    f"\n🔗 <b><a href='{deal_url}'>Сделка №{deal_id}</a></b>\n"
                    f"🏷 Название: {title}\n"
                    f"📌 Стадия: {stage}\n"
                    "―――――――――――――――――――――"
                )

            message.append(f"\nПоказано {len(deals)} сделок.")
            await m.answer("\n".join(message), parse_mode="HTML")

    except Exception as e:
        logging.error(f"Ошибка в /deals: {str(e)}", exc_info=True)
        await m.answer(f"⚠️ Ошибка: {str(e)}")


@dp.message(Command("settings"))
async def cmd_settings(m: Message):
    """Меню настроек уведомлений"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    await show_settings_menu(m.chat.id)


async def show_settings_menu(chat_id: int):
    # Получаем текущие настройки уведомлений из базы данных
    settings = await get_notification_settings(chat_id)

    # Создаём inline-клавиатуру с кнопками для включения/отключения уведомлений
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"Новые сделки {'🔴' if not settings['new_deals'] else '🟢'}",
                callback_data="toggle_new_deals"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Изменения сделок {'🔴' if not settings['deal_updates'] else '🟢'}",
                callback_data="toggle_deal_updates"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Создание задач {'🔴' if not settings['task_creations'] else '🟢'}",
                callback_data="toggle_task_creations"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Изменения задач {'🔴' if not settings['task_updates'] else '🟢'}",
                callback_data="toggle_task_updates"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Комментарии {'🔴' if not settings['comments'] else '🟢'}",
                callback_data="toggle_comments"
            )
        ]
    ])

    # Отправляем сообщение с меню настроек и кнопками
    await bot.send_message(
        chat_id,
        "⚙️ Настройки уведомлений:\nВыберите тип уведомлений для настройки:",
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data.startswith('toggle_'))
async def process_toggle(callback: CallbackQuery):
    """
        Обрабатывает нажатие на кнопку изменения настроек уведомлений.

        Инвертирует соответствующее значение (вкл/выкл) в таблице notification_settings
        и обновляет меню настроек.

        :param callback: Объект нажатой callback-кнопки от пользователя
        """

    if not pool:
        logging.error("Database pool is not initialized")
        return

    # Извлекаем тип настройки из callback-данных, например: 'new_deals'
    action = callback.data.split('_', 1)[1]
    chat_id = callback.message.chat.id

    async with pool.acquire() as conn:
        # Получаем текущее значение этой настройки из базы данных
        current_value = await conn.fetchval(
            f"SELECT {action} FROM notification_settings WHERE chat_id = $1",
            chat_id
        )

        # Инвертируем (меняем True на False и наоборот)
        new_value = not current_value

        # Сохраняем обновлённое значение в базу
        await conn.execute(
            f"UPDATE notification_settings SET {action} = $1 WHERE chat_id = $2",
            new_value, chat_id
        )

    # Удаляем старое меню и отправляем обновлённое
    await callback.message.delete()
    await show_settings_menu(chat_id)

    # Подтверждаем callback, чтобы Telegram скрыл "часики"
    await callback.answer()


@dp.message(Command("task_history"))
async def cmd_task_history(m: Message, state: FSMContext):
    """Запросить у пользователя ID задачи для истории изменений"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")
    await m.answer("Введите, пожалуйста, ID задачи, историю которой хотите увидеть:")
    await state.set_state(TaskHistoryStates.waiting_for_task_id)

@dp.message(TaskHistoryStates.waiting_for_task_id)
async def process_task_history_id(m: Message, state: FSMContext):
    """Обработка введённого ID задачи и вывод истории изменений"""
    await state.clear()  # сброс состояния, независимо от результата
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    # Проверяем, что введено число
    if not m.text.isdigit():
        return await m.answer("❌ Неверный формат ID.")

    task_id = m.text
    domain = user_data["domain"]
    token = user_data["access_token"]
    user_id = user_data["user_id"]

    # Запрашиваем историю
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{domain}/rest/tasks.task.history.list.json",
            params={"auth": token},
            json={"taskId": int(task_id)}
        )
        data = resp.json()

    if data.get("error"):
        return await m.answer(f"❌ Ошибка Bitrix24: {data.get('error_description')}")

    try:
        history = data.get("result", []).get('list')
    except AttributeError:
        return await m.answer("❌ Ошибка: Данная задача недоступна или удалена.")

    #logging.info(f"History data: {history}")  # Логи

    if not history:
        return await m.answer(f"ℹ️ Для задачи №{task_id} история изменений не найдена.")

    status_map = {
        '2': "🆕 Ждет выполнения",
        '3': "🔄 Выполняется",
        '4': "⏳ Ожидает контроля",
        '5': "✅ Завершена",
        '6': "⏸ Отложена"
    }
    
    priority_map = {
        '0': "Низкий",
        '1': "Средний",
        '2': "Высокий"
    }

    # Форматируем вывод
    messages = [f"🗂 История задачи <b><a href='https://{BITRIX_DOMAIN}/company/personal/user/{user_id}/tasks/task/view/{task_id}/'>№{task_id}</a></b>:"]
    for entry in history:
        date = entry.get("createdDate", "–")
        try:
            date_date = datetime.strptime(date, "%Y-%m-%dT%H:%M:%S%z")
            date = date_date.strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            logging.error(f"Ошибка обработки даты: {date}")
        field = entry.get("field", "–")
        old = entry.get("value").get("from", "")
        new = entry.get("value").get("to", "")
        author = f"{entry.get("user").get("name")} {entry.get("user").get("lastName")}"

        text = "-"
        match field:
            case "NEW":
                text = "Создана задача\n"
            case "TITLE":
                text = (f"Изменено Название\n"
                        f"Изменение: {old} → {new}\n")
            case "DESCRIPTION":
                text = "Изменено Описание\n"
            case "STATUS":
                text = (f"Изменен Статус\n"
                        f"Изменение: {status_map[old]} → {status_map[new]}\n")
            case "PRIORITY":
                text = (f"Изменен Приоритет\n"
                        f"Изменение: {priority_map[old]} → {priority_map[new]}\n")
            case "DEADLINE":
                text = "Изменен Крайний срок\n"
            case "COMMENT":
                text = f"Добавлен комментарий №{new}\n"
        
        text += f"Автор: {author}"
        messages.append(f"\n<b>{date}</b> - {text}")

    # В Телеграм нельзя отправить очень длинное сообщение, разбиваем по 10 записей
    chunk_size = 10
    for i in range(0, len(messages), chunk_size):
        await m.answer("\n".join(messages[i:i+chunk_size]), parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    """Справка о командах бота"""
    help_text = ("""
📚 Доступные команды:
/start - Авторизация в Bitrix24
/tasks - Вывести список задач
/task - Создать задачу (Формат: Название | Описание | [ID_исполнителя] | [Приоритет] | [Срок исполнения])
/comment - Добавить комментарий к задаче (Формат: [ID_задачи] | Комментарий)
/deal - Создать сделку (Формат: Название ЖК | Адрес | [ID_стадии]) ❗Только для админов❗
/deals - Показать список сделок
/employees - Получить список сотрудников
/stages - Получить список доступных стадий для сделок
/task_history - Получить историю изменений задачи
/settings - Настройка уведомлений

/help - Справка о командах
    """)

    await m.answer(help_text)


pool = None  # Глобальная переменная для пула подключений к базе данных


async def main():
    import uvicorn
    global pool

    # Создаём пул подключений к базе данных с параметрами:
    # DATABASE_URL - строка подключения,
    # min_size - минимальное количество соединений в пуле,
    # max_size - максимальное количество,
    # command_timeout - таймаут выполнения команды (секунд)
    pool = await create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
        command_timeout=60
    )

    # Конфигурируем и запускаем uvicorn — ASGI сервер для FastAPI
    config = uvicorn.Config(app=app, host="0.0.0.0", port=5000, log_level="info")
    server = uvicorn.Server(config)

    # Параллельно запускаем:
    # - сервер FastAPI (uvicorn)
    # - и поллинг Telegram-бота (dp.start_polling)
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    try:
        # Запускаем главный асинхронный цикл
        asyncio.run(main())
    finally:
        # При завершении программы аккуратно закрываем пул подключений,
        # чтобы освободить ресурсы
        if pool:
            asyncio.run(pool.close())