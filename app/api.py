import asyncpg
import httpx
import logging
import json

from time import time
from typing import Dict
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

from config import *
from db import *
from bot import *

app = FastAPI()


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
                    # logging.info(f"Unbound {event} → {handler_url}")  # Логи
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
                # logging.info(f"Bound {event} → {WEBHOOK_DOMAIN}/callback")  # Логи
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
