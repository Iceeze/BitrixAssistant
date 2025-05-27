import httpx
import logging
import json

from time import time
from typing import Dict

from config import *
from db import *


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
