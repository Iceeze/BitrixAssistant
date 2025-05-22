import logging
import httpx
import json

from datetime import datetime
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import *
from db import *
from api import *

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# --- Уведомления ---
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


# --- Команды Telegram Bot --- 
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
