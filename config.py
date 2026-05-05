import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = 965040732
DB_PATH = "hrbot.db"
CHECK_INTERVAL_SECONDS = 300  # 5 minutes
MAX_NEW_VACANCIES = 50
INITIAL_VACANCIES_COUNT = 5
HH_BASE_URL = "https://api.hh.ru"
HH_USER_AGENT = "hrbot3/1.0 (personal vacancy monitor; tg:@hrbot3)"

# Группа куда пересылаются вакансии при нажатии "Откликнулся"
# Получи ID командой /chatid прямо в группе
APPLIED_GROUP_ID = int(os.getenv("APPLIED_GROUP_ID", "0"))

# Цель по откликам
APPLY_GOAL = 1000

# hh.ru OAuth (зарегистрируй приложение на https://dev.hh.ru/)
HH_CLIENT_ID     = os.getenv("HH_CLIENT_ID", "")
HH_CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET", "")

# Groq AI
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Прокси для Telegram (задаётся в .env; на сервере оставить пустым)
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")
