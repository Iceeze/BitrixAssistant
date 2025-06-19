import os
import logging
from dotenv import load_dotenv
from typing import Dict, Optional
from collections import defaultdict
from aiogram.fsm.state import State, StatesGroup

load_dotenv()

# Настройки Bitrix
BITRIX_CLIENT_ID = os.getenv("BITRIX_CLIENT_ID")  # client_id Ильгиза
# BITRIX_CLIENT_ID = "local.68187191a08683.25172914"  # client_id Данила

BITRIX_CLIENT_SECRET = os.getenv("BITRIX_CLIENT_SECRET")  # client_secret Ильгиза
# BITRIX_CLIENT_SECRET = "46wPWoUU1YLv5d86ozDh7FbhODOi2L2mlmNBWweaA6jNxV2xX1"  # client_secret Данила

BITRIX_DOMAIN = os.getenv("BITRIX_DOMAIN")  # Домен портала Битрикс24 Ильгиза
# BITRIX_DOMAIN = "b24-rqyyhh.bitrix24.ru"  # Домен портала Битрикс24 Данила

REDIRECT_URI = os.getenv("REDIRECT_URI")
WEBHOOK_DOMAIN = os.getenv("WEBHOOK_DOMAIN")

# Настройки Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# База данных
DATABASE_URL = os.getenv("DATABASE_URL")

# Глобальные мапы
is_registered_events: Dict[str, bool] = {}
member_map: Dict[str, set[str]] = defaultdict(set)  # ключ — это member_id портала, а значение — set чат‑ID

# Базовая конфигурация логирования для всего приложения
logging.basicConfig(level=logging.INFO)

# Состояния
class NotificationSettings(StatesGroup):
    waiting_action = State()

class TaskHistoryStates(StatesGroup):
    waiting_for_task_id = State()

class TaskCreationStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_responsible = State()
    waiting_for_priority = State()
    waiting_for_deadline = State()

class DealCreationStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_address = State()
    waiting_for_stage_id = State()

class CommentCreationStates(StatesGroup):
    waiting_for_task_id = State()
    waiting_for_comment_text = State()

class TaskEditStates(StatesGroup):
    waiting_for_task_id = State()
    choosing_field = State()
    editing_field = State()