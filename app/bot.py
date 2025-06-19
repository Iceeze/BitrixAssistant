import logging
import httpx
import json

from datetime import datetime
from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import *
from db import *
from utils import *

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

            stage_map = {}

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://{user_data['domain']}/rest/crm.dealcategory.stage.list",
                    params={"auth": user_data["access_token"]}
                )
                stages = resp.json().get('result', [])

                for stage in stages:
                    stage_map[stage['STATUS_ID']] = stage['NAME']

            # Формирование сообщения
            deal_url = f"https://{domain}/crm/deal/details/{deal_id}/"
            title = deal.get('TITLE', 'Без названия')
            address = deal.get('COMMENTS', 'Не указано')
            stage = stage_map.get(deal.get('STAGE_ID'), deal.get('STAGE_ID'))

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
        logging.info(f"Comment data: {comment_data}") # Логи

        if not comment_data:
            logging.warning("No FIELDS_AFTER in webhook data for comment")
            return

        comment_id = comment_data.get('ID')
        task_id = comment_data.get('TASK_ID')
        if not comment_id or not task_id:
            logging.warning(f"Invalid comment webhook payload: {comment_data}")
            return

        message = ""
        responsible_id = None
        user_id = user_data["user_id"]

        try:
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
        except Exception as e:
            logging.error(f"Failed to fetch comment {comment_id} for task {task_id}: {e}")
            return

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
            if not task:
                logging.warning(f"Task {task_id} not found when resolving responsible for comment")
                return
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


@dp.message(F.text == "/cancel")
async def cmd_cancel(m: Message, state: FSMContext):
    """Отмена действия"""
    current_state = await state.get_state()
    if current_state is None:
        return

    await state.clear()
    await m.answer("❌ Действие отменено.")

@dp.message(Command("task"))
async def cmd_task(m: Message, state: FSMContext):
    """Начало создания задачи"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    await m.answer("Вы можете ввести /cancel для отмены.\n\nВведите название задачи:")
    await state.set_state(TaskCreationStates.waiting_for_title)


@dp.message(TaskCreationStates.waiting_for_title)
async def process_task_title(m: Message, state: FSMContext):
    if len(m.text) > 255:
        return await m.answer("❌ Слишком длинное название. Максимум 255 символов. Введите снова:")

    await state.update_data(title=m.text)
    await m.answer("Введите описание задачи (или 'нет' чтобы пропустить):")
    await state.set_state(TaskCreationStates.waiting_for_description)


@dp.message(TaskCreationStates.waiting_for_description)
async def process_task_description(m: Message, state: FSMContext):
    description = m.text if m.text.lower() != "нет" else ""
    await state.update_data(description=description)

    await m.answer("Введите ID ответственного пользователя (или 'нет' чтобы назначить себя):")
    await state.set_state(TaskCreationStates.waiting_for_responsible)


@dp.message(TaskCreationStates.waiting_for_responsible)
async def process_task_responsible(m: Message, state: FSMContext):

    user_data = await get_user(m.from_user.id)
    data = await state.get_data()

    if m.text.lower() == "нет":
        responsible_id = user_data["user_id"]
    else:
        if not m.text.isdigit():
            return await m.answer("❌ ID должен быть числом. Введите снова:")

        responsible_id = int(m.text)
        if not await check_user_exists(user_data["domain"], user_data["access_token"], responsible_id):
            return await m.answer("❌ Пользователь не найден. Введите снова:")

    await state.update_data(responsible_id=responsible_id)
    await m.answer("Введите приоритет (1-низкий, 2-средний, 3-высокий или 'нет'):")
    await state.set_state(TaskCreationStates.waiting_for_priority)


@dp.message(TaskCreationStates.waiting_for_priority)
async def process_task_priority(m: Message, state: FSMContext):
    priority_map = {"1": 0, "2": 1, "3": 2}
    priority = None

    if m.text.lower() != "нет":
        if m.text not in priority_map:
            return await m.answer("❌ Неверный приоритет. Используйте 1, 2 или 3. Введите снова:")
        priority = priority_map[m.text]

    await state.update_data(priority=priority)
    await m.answer("Введите крайний срок в формате ГГГГ-ММ-ДД (или 'нет'):")
    await state.set_state(TaskCreationStates.waiting_for_deadline)


@dp.message(TaskCreationStates.waiting_for_deadline)
async def process_task_deadline(m: Message, state: FSMContext):
    user_data = await get_user(m.from_user.id)
    data = await state.get_data()
    deadline = None

    if m.text.lower() != "нет":
        try:
            deadline = datetime.strptime(m.text, "%Y-%m-%d").strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return await m.answer("❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД. Введите снова:")

    # Сбор всех данных
    task_data = {
        "TITLE": data["title"],
        "DESCRIPTION": data.get("description", ""),
        "RESPONSIBLE_ID": data["responsible_id"],
        "PRIORITY": data.get("priority", 2),
        "DEADLINE": deadline
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{user_data['domain']}/rest/tasks.task.add.json",
                params={"auth": user_data["access_token"]},
                json={"fields": task_data}
            )
            data = resp.json()

            if data.get('error'):
                error_msg = data.get('error_description', 'Неизвестная ошибка Bitrix')
                raise ValueError(error_msg)

            task_id = data['result']['task']['id']
            await m.answer(f"✅ Задача создана! ID: {task_id}")

    except Exception as e:
        await m.answer(f"❌ Ошибка при создании задачи.")

    await state.clear()


@dp.message(Command("deal"))
async def cmd_deal(m: Message, state: FSMContext):
    """Начало создания сделки"""
    user_data = await get_user(m.from_user.id)
    if not user_data or not user_data.get("is_admin"):
        return await m.answer("❗ Требуются права администратора. Авторизуйтесь через /start")

    await m.answer("Вы можете ввести /cancel для отмены.\n\nВведите название сделки (Название ЖК):")
    await state.set_state(DealCreationStates.waiting_for_title)


@dp.message(DealCreationStates.waiting_for_title)
async def process_deal_title(m: Message, state: FSMContext):
    if len(m.text.strip()) == 0:
        return await m.answer("❌ Название не может быть пустым. Введите снова:")

    if len(m.text) > 255:
        return await m.answer("❌ Слишком длинное название. Максимум 255 символов. Введите снова:")

    await state.update_data(title=m.text)
    await m.answer("Введите адрес:")
    await state.set_state(DealCreationStates.waiting_for_address)


@dp.message(DealCreationStates.waiting_for_address)
async def process_deal_address(m: Message, state: FSMContext):
    if len(m.text.strip()) == 0:
        return await m.answer("❌ Адрес не может быть пустым. Введите снова:")

    user_data = await get_user(m.from_user.id)
    message = await show_stage_list(m.chat.id, user_data["domain"], user_data["access_token"])

    await state.update_data(address=m.text)
    await m.answer(f"{message}\nВведите ID стадии сделки (или 'нет' чтобы пропустить):")

    await state.set_state(DealCreationStates.waiting_for_stage_id)


@dp.message(DealCreationStates.waiting_for_stage_id)
async def process_deal_stage(m: Message, state: FSMContext):
    user_data = await get_user(m.from_user.id)
    data = await state.get_data()
    stage_id = None

    if m.text.lower() != "нет":
        # Проверка существования стадии
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://{user_data['domain']}/rest/crm.dealcategory.stage.list",
                    params={"auth": user_data["access_token"]}
                )
                stages = resp.json().get('result', [])
                stage_ids = {stage['STATUS_ID'] for stage in stages}

                if m.text not in stage_ids:
                    return await m.answer("❌ Неверный ID стадии. Введите корректный ID или 'нет':")

                stage_id = m.text
        except Exception as e:
            return await m.answer(f"❌ Ошибка проверки стадии: {str(e)}")

    # Сбор всех данных
    deal_data = {
        "TITLE": data["title"],
        "COMMENTS": data["address"],
        "ASSIGNED_BY_ID": user_data["user_id"]
    }

    if stage_id:
        deal_data["STAGE_ID"] = stage_id

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{user_data['domain']}/rest/crm.deal.add.json",
                params={"auth": user_data["access_token"]},
                json={"fields": deal_data}
            )
            deal_data = resp.json()

            if deal_data.get('error'):
                error_msg = deal_data.get('error_description', 'Неизвестная ошибка Bitrix')
                raise ValueError(error_msg)

            deal_id = deal_data.get('result')
            await m.answer(f"✅ Сделка создана! ID: {deal_id}")

    except Exception as e:
        await m.answer(f"❌ Ошибка при создании сделки: {str(e)}")

    await state.clear()


@dp.message(Command("comment"))
async def cmd_comment(m: Message, state: FSMContext):
    """Начало добавления комментария"""
    user_data = await get_user(m.from_user.id)
    if not user_data:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    await m.answer("Вы можете ввести /cancel для отмены.\n\nВведите ID задачи:")
    await state.set_state(CommentCreationStates.waiting_for_task_id)


@dp.message(CommentCreationStates.waiting_for_task_id)
async def process_comment_task_id(m: Message, state: FSMContext):
    user_data = await get_user(m.from_user.id)

    # Проверка ID задачи
    if not m.text.isdigit():
        return await m.answer("❌ ID задачи должен быть числом. Введите снова:")

    task_id = int(m.text)

    # Проверка существования задачи
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{user_data['domain']}/rest/tasks.task.get.json",
                params={
                    "taskId": task_id,
                    "auth": user_data["access_token"]
                }
            )
            task_data = resp.json()
            #logging.info(f"Task Data: {task_data}")  # Логи

            if task_data.get('result') == []:
                return await m.answer("❌ Задача не найдена или нет доступа. Введите другой ID:")

            await state.update_data(task_id=task_id)
            await m.answer("Введите текст комментария:")
            await state.set_state(CommentCreationStates.waiting_for_comment_text)

    except Exception as e:
        await m.answer(f"❌ Ошибка проверки доступа к задаче.")
        await state.clear()


@dp.message(CommentCreationStates.waiting_for_comment_text)
async def process_comment_text(m: Message, state: FSMContext):
    user_data = await get_user(m.from_user.id)
    data = await state.get_data()
    task_id = data['task_id']

    if len(m.text.strip()) == 0:
        return await m.answer("❌ Комментарий не может быть пустым. Введите снова:")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{user_data['domain']}/rest/task.commentitem.add.json",
                params={"auth": user_data["access_token"]},
                json={
                    "TASK_ID": task_id,
                    "fields": {
                        "AUTHOR_ID": user_data["user_id"],
                        "POST_MESSAGE": m.text
                    }
                }
            )
            comment_data = resp.json()

            if 'error' in comment_data:
                error_msg = comment_data.get('error_description', 'Ошибка добавления комментария')
                raise ValueError(error_msg)

            await m.answer(f"💬 Комментарий добавлен к задаче {task_id}")

    except Exception as e:
        await m.answer(f"❌ Ошибка добавления комментария.")

    await state.clear()


async def show_stage_list(chat_id: int, domain: str, token: str):
    """Получает список стадий сделок"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://{domain}/rest/crm.dealcategory.stage.list",
                params={"auth": token}
            )
            stages = resp.json().get('result', [])

            if not stages:
                await bot.send_message(chat_id, "❗ Стадии сделок не найдены.")
                return

            message = "📊 Доступные стадии сделок:\n"
            for stage in stages:
                message += f"{stage['NAME']} (ID: {stage['STATUS_ID']})\n"

            return message

    except Exception as e:
        logging.error(f"Ошибка при получении стадий: {e}")
        await bot.send_message(chat_id, f"❌ Ошибка при получении стадий: {e}")


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

            stage_map = {}

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://{user_data['domain']}/rest/crm.dealcategory.stage.list",
                    params={"auth": user_data["access_token"]}
                )
                stages = resp.json().get('result', [])

                for stage in stages:
                    stage_map[stage['STATUS_ID']] = stage['NAME']

            message = ["🏢 Список сделок:\n"]
            for deal in deals:
                deal_id = deal.get('ID')
                title = deal.get('TITLE', 'Без названия')
                stage = stage_map.get(deal.get('STAGE_ID'), deal.get('STAGE_ID'))

                deal_url = f"https://{domain}/crm/deal/details/{deal_id}/"
                message.append(
                    f"🔗 <b><a href='{deal_url}'>Сделка №{deal_id}</a></b>\n"
                    f"🏷 Название: {title}\n"
                    f"📌 Стадия: {stage}\n"
                    "―――――――――――――――――――――"
                )

            message.append(f"\nПоказано {len(deals)} сделок.")
            await m.answer("\n".join(message), parse_mode="HTML")

    except Exception as e:
        logging.error(f"Ошибка в /deals: {str(e)}", exc_info=True)
        await m.answer(f"⚠️ Ошибка вывода.")


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

    pool = await Database.get_pool()

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
        author = f"{entry.get('user').get('name')} {entry.get('user').get('lastName')}"

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
            case "RESPONSIBLE_ID":
                old_resp_name = await get_user_name(domain, token, int(old))
                new_resp_name = await get_user_name(domain, token, int(new))
                text = (f"Сменен Исполнитель\n"
                        f"Изменение: {old_resp_name} → {new_resp_name}\n")
        
        text += f"Автор: {author}"
        messages.append(f"\n<b>{date}</b> - {text}")

    # В Телеграм нельзя отправить очень длинное сообщение, разбиваем по 10 записей
    chunk_size = 10
    for i in range(0, len(messages), chunk_size):
        await m.answer("\n".join(messages[i:i+chunk_size]), parse_mode="HTML")


def edit_fields_keyboard(changed: dict = None) -> InlineKeyboardMarkup:
    """Клавиатура полей для редактирования задачи"""
    changed = changed or {}

    def mark(label, field):
        return f"✅ {label}" if changed.get(field) else label

    keyboard = [
        [InlineKeyboardButton(text=mark("✏️ Название", "title"), callback_data="edit_field_title")],
        [InlineKeyboardButton(text=mark("📝 Описание", "description"), callback_data="edit_field_description")],
        [InlineKeyboardButton(text=mark("🚨 Приоритет", "priority"), callback_data="edit_field_priority")],
        [InlineKeyboardButton(text=mark("👤 Ответственный", "responsible_id"), callback_data="edit_field_responsible_id")],
        [InlineKeyboardButton(text=mark("📈 Статус", "status"), callback_data="edit_field_status")],
        [InlineKeyboardButton(text=mark("⏰ Срок", "deadline"), callback_data="edit_field_deadline")],
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="edit_save")]
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("edit_task"))
async def cmd_edit_task(m: Message, state: FSMContext):
    """Редактирование задачи по ID"""
    await m.answer("Введите ID задачи для редактирования:")
    await state.set_state(TaskEditStates.waiting_for_task_id)

@dp.message(TaskEditStates.waiting_for_task_id)
async def process_edit_task_id(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("❌ ID должен быть числом. Попробуйте ещё раз:")

    task_id = int(m.text)
    user = await get_user(m.from_user.id)
    if not user:
        return await m.answer("❗ Сначала авторизуйтесь через /start")

    # проверяем права:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{user['domain']}/rest/tasks.task.get",
            params={"taskId": task_id, "auth": user["access_token"]}
        )
    data = resp.json().get("result", {})
    task = data.get("task") or {}
    if not task:
        return await m.answer("❌ Задача не найдена.")

    is_admin   = user["is_admin"]
    is_creator = str(task.get("creatorId")) == str(user["user_id"])
    if not (is_admin or is_creator):
        return await m.answer("🚫 У вас нет прав редактировать эту задачу.")

    # запомним task_id и пустой словарь изменений
    await state.update_data(task_id=task_id, changes={})
    kb = edit_fields_keyboard(changed={})
    await m.answer("Выберите поле для редактирования:", reply_markup=kb)
    await state.set_state(TaskEditStates.choosing_field)

@dp.callback_query(TaskEditStates.choosing_field, F.data.startswith("edit_field_"))
async def callback_choose_field(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    field = callback.data.replace("edit_field_", "")

    names = {
        "title": "новое название",
        "description": "новое описание",
        "priority": "новый приоритет (0 — низкий, 1 — средний, 2 — высокий)",
        "deadline": "новый срок (ГГГГ-ММ-ДД)",
        "responsible_id": "ID ответственного сотрудника",
        "status": "статус задачи (2 — Новая, 3 — В работе, 4 — Ожидает контроля, 5 — Завершена, 6 — Отложена)"
    }

    if field not in names:
        await callback.message.answer("⚠️ Неизвестное поле для редактирования.")
        return

    await state.update_data(current_field=field)
    await state.set_state(TaskEditStates.editing_field)

    await callback.message.edit_text(
        f"✏️ Введите {names[field]} (или 'нет' чтобы пропустить):"
    )


@dp.message(TaskEditStates.editing_field)
async def process_editing_field(m: Message, state: FSMContext):
    data = await state.get_data()
    field = data["current_field"]
    val = m.text.strip()
    user = await get_user(m.from_user.id)

    # обработка значений
    if val.lower() != "нет":
        if field == "deadline":
            try:
                datetime.strptime(val, "%Y-%m-%d")
                val = val + "T00:00:00"
            except:
                return await m.answer("❌ Неверный формат даты. Повторите: ГГГГ-ММ-ДД")

        elif field == "priority":
            if val not in ("0", "1", "2"):
                return await m.answer("❌ Приоритет должен быть 0, 1 или 2. Повторите:")

        elif field == "status":
            if val not in ("2", "3", "4", "5", "6"):
                return await m.answer("❌ Статус должен быть от 2 до 6. Повторите:")

        elif field == "responsible_id":
            if not val.isdigit():
                return await m.answer("❌ ID должен быть числом. Повторите:")
            # Проверка существования пользователя
            exists = await check_user_exists(user["domain"], user["access_token"], int(val))
            if not exists:
                return await m.answer("❌ Пользователь с таким ID не найден. Повторите:")

    changes = data.get("changes", {})
    if val.lower() != "нет":
        changes[field.upper()] = int(val) if val.isdigit() else val
    await state.update_data(changes=changes)

    kb = edit_fields_keyboard(changed=changes)
    await m.answer("Что ещё хотите изменить? Или нажмите «Сохранить»", reply_markup=kb)
    await state.set_state(TaskEditStates.choosing_field)

@dp.callback_query(F.data == "edit_save", TaskEditStates.choosing_field)
async def callback_save(c: CallbackQuery, state: FSMContext):
    data    = await state.get_data()
    task_id = data["task_id"]
    changes = data["changes"]
    user    = await get_user(c.from_user.id)

    if not changes:
        await c.answer("⚠️ Нет изменений для сохранения.", show_alert=True)
        return

    params = {"auth": user["access_token"]}
    body   = {"taskId": task_id, "fields": changes}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{user['domain']}/rest/tasks.task.update",
            params=params,
            json=body
        )
    result = resp.json()
    if result.get("error"):
        text = result.get("error_description", "Неизвестная ошибка")
        await c.message.edit_text(f"❌ Ошибка: {text}")
    else:
        await c.message.edit_text(f"✅ Задача №{task_id} успешно обновлена!")

    await state.clear()
    await c.answer()

@dp.callback_query(F.data == "edit_cancel", TaskEditStates.choosing_field)
async def callback_cancel(c: CallbackQuery, state: FSMContext):
    await c.message.edit_text("❌ Редактирование отменено.")
    await state.clear()
    await c.answer()


@dp.message(Command("help"))
async def cmd_help(m: Message):
    """Справка о командах бота"""
    help_text = ("""
📚 Доступные команды:
/start - Авторизация в Bitrix24
/tasks - Вывести список задач
/task - Создать задачу
/edit_task - Редактировать задачу
/comment - Добавить комментарий к задаче
/deal - Создать сделку (❗Только для админов❗)
/deals - Показать список сделок
/employees - Получить список сотрудников
/task_history - Получить историю изменений задачи
/settings - Настройка уведомлений

/help - Справка о командах
    """)

    await m.answer(help_text)
