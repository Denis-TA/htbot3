import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = "hrbot.db"
CHECK_INTERVAL_SECONDS = 300  # 5 minutes
MAX_NEW_VACANCIES = 50
INITIAL_VACANCIES_COUNT = 5
HH_BASE_URL = "https://api.hh.ru"
HH_USER_AGENT = "hrbot3/1.0 (personal vacancy monitor; tg:@hrbot3)"

# hh.ru OAuth (зарегистрируй приложение на https://dev.hh.ru/)
HH_CLIENT_ID     = os.getenv("HH_CLIENT_ID", "")
HH_CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET", "")

# Groq AI — up to 5 keys for round-robin on rate limits (429).
# Railway requires every referenced var to exist, so a key set to "123"
# is treated as a placeholder and ignored.
# The first slot also accepts the older unsuffixed GROQ_API_KEY, so setups
# created before the keys were renamed keep working.
def _collect_groq_keys() -> list:
    raw = [
        os.getenv("GROQ_API_KEY_1", "") or os.getenv("GROQ_API_KEY", ""),
        os.getenv("GROQ_API_KEY_2", ""),
        os.getenv("GROQ_API_KEY_3", ""),
        os.getenv("GROQ_API_KEY_4", ""),
        os.getenv("GROQ_API_KEY_5", ""),
    ]
    keys = []
    for k in raw:
        k = (k or "").strip()
        if k and k != "123":          # "123" = Railway placeholder
            keys.append(k)
    return keys

GROQ_API_KEYS = _collect_groq_keys()
# Backwards-compat: first valid key (or empty)
GROQ_API_KEY = GROQ_API_KEYS[0] if GROQ_API_KEYS else ""

# Прокси для Telegram (задаётся в .env; на сервере оставить пустым)
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")
