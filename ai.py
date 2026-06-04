"""
AI module: fetch vacancy page from hh.ru, call Groq, return cover letter.
"""
import logging

import requests
from bs4 import BeautifulSoup

from config import GROQ_API_KEY, TELEGRAM_PROXY

_PROXIES = {"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY} if TELEGRAM_PROXY else {}

log = logging.getLogger(__name__)

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

_session: requests.Session | None = None

# ── Default prompt template ───────────────────────────────────────────────────
# Placeholders: {VACANCY_CARD}, {VACANCY_DESC}, {RESUME_BLOCK}, {LIE_BLOCK}

DEFAULT_PROMPT_TEMPLATE = """\
Ты помогаешь кандидату написать отклик на вакансию с hh.ru.

=== Карточка вакансии ===
{VACANCY_CARD}

=== Полное описание вакансии ===
{VACANCY_DESC}

{RESUME_BLOCK}

=== Задача ===
Напиши сопроводительное письмо для отклика на эту вакансию.
Требования к тексту:
- На русском языке
- 4-6 предложений, без воды
- Покажи что внимательно прочитал требования — упомяни 2-3 конкретных пункта из описания
- Не используй шаблонные фразы: "я ответственный", "быстро обучаюсь", "работаю в команде"
- Только текст письма, без заголовков и пояснений

{LIE_BLOCK}"""

LIE_INSTRUCTIONS = {
    1: ("Уровень честности 1 — СТРОГО по резюме: "
        "используй только то, что написано в резюме. "
        "Не добавляй навыки или опыт, которых нет."),
    2: ("Уровень честности 2 — НЕМНОГО приукрасить: "
        "можно слегка преувеличить достижения, добавить смежные навыки которые логично вытекают из указанных. "
        "Не придумывай принципиально новый опыт."),
    3: ("Уровень честности 3 — СВОБОДНО дополнять: "
        "можно добавлять опыт, навыки и достижения которых нет в резюме, если они релевантны вакансии. "
        "Главное — убедительность письма."),
}


# ── Keyword suggester ────────────────────────────────────────────────────────

def suggest_keywords(topic: str) -> str:
    """
    Ask Groq to suggest hh.ru search keywords for the given job topic.
    Returns a comma-separated string of keyword groups, or error message.
    """
    if not GROQ_API_KEY:
        return "❌ GROQ_API_KEY не задан в .env"

    prompt = (
        f"Ты помогаешь с поиском работы на hh.ru.\n\n"
        f"Для поиска вакансий по теме «{topic}» предложи 5–8 вариантов "
        f"ключевых слов и фраз — названий должностей и ролей, на русском и английском.\n\n"
        f"Формат ответа: только список через запятую, без нумерации и пояснений.\n"
        f"Пример: product manager, продуктовый менеджер, PM, product owner, владелец продукта"
    )

    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  200,
        "temperature": 0.5,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }

    attempts = [("proxy", _PROXIES), ("direct", {})]
    last_err: Exception | None = None

    for label, proxies in attempts:
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload,
                              proxies=proxies, timeout=20)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            else:
                return f"❌ Groq API error {r.status_code}"
        except Exception as e:
            log.warning("suggest_keywords %s failed: %s", label, e)
            last_err = e

    return f"❌ Не удалось подключиться к Groq: {last_err}"


# ── Session ───────────────────────────────────────────────────────────────────

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })
        try:
            _session.get("https://hh.ru/", timeout=10)
        except Exception:
            pass
    return _session


# ── Vacancy page parser ───────────────────────────────────────────────────────

def fetch_vacancy_page(url: str) -> str:
    """Open hh.ru vacancy URL and return plain text of the description."""
    try:
        r = _get_session().get(url, timeout=15)
        log.info("Vacancy page %s → HTTP %s", url, r.status_code)
        if r.status_code != 200:
            return ""

        soup = BeautifulSoup(r.text, "html.parser")
        parts: list[str] = []

        title = soup.find(attrs={"data-qa": "vacancy-title"})
        if not title:
            title = soup.find("h1")
        if title:
            parts.append(title.get_text(strip=True))

        salary = soup.find(attrs={"data-qa": "vacancy-salary"})
        if salary:
            parts.append("Зарплата: " + salary.get_text(strip=True))

        company = soup.find(attrs={"data-qa": "vacancy-company-name"})
        if company:
            parts.append("Компания: " + company.get_text(strip=True))

        desc = soup.find(attrs={"data-qa": "vacancy-description"})
        if desc:
            parts.append(desc.get_text(separator="\n", strip=True))

        text = "\n\n".join(parts)
        log.info("Fetched vacancy text: %d chars", len(text))
        return text[:6000]

    except Exception as e:
        log.error("fetch_vacancy_page failed: %s", e)
        return ""


def fetch_work_format(url: str) -> str:
    """
    Best-effort: open hh.ru vacancy page and return a short work-format label
    like "Удалёнка", "Офис", "Гибрид" (or a combo "Офис/Гибрид").
    Returns "" if unavailable.
    """
    try:
        r = _get_session().get(url, timeout=12)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        el = soup.find(attrs={"data-qa": "work-formats-text"})
        if not el:
            return ""
        raw = el.get_text(" ", strip=True).replace("\xa0", " ").lower()
        labels = []
        if "удал" in raw:
            labels.append("Удалёнка")
        if "гибрид" in raw:
            labels.append("Гибрид")
        if "на месте" in raw or "офис" in raw:
            labels.append("Офис")
        return "/".join(labels)
    except Exception as e:
        log.debug("fetch_work_format failed: %s", e)
        return ""


# ── Groq call ─────────────────────────────────────────────────────────────────

def generate_cover_letter(
    vacancy_card_text: str,
    vacancy_page_text: str,
    resume_text: str | None = None,
    lie_level: int = 1,
    prompt_template: str | None = None,
) -> str:
    """
    Call Groq and return a cover letter.

    Args:
        vacancy_card_text:  formatted text from Telegram message (title, company, salary)
        vacancy_page_text:  full description scraped from hh.ru
        resume_text:        candidate resume, optional
        lie_level:          1 = honest, 2 = slight embellishment, 3 = creative
        prompt_template:    custom template with placeholders; uses DEFAULT if None
    """
    if not GROQ_API_KEY:
        return "❌ GROQ_API_KEY не задан в .env"

    description = vacancy_page_text or "(не удалось загрузить страницу вакансии)"
    template    = prompt_template or DEFAULT_PROMPT_TEMPLATE

    # Build blocks
    if resume_text and resume_text.strip():
        resume_block = f"=== Моё резюме ===\n{resume_text.strip()}"
        lie_block    = LIE_INSTRUCTIONS.get(lie_level, "")
    else:
        resume_block = ""
        lie_block    = ""

    try:
        prompt = template.format(
            VACANCY_CARD=vacancy_card_text,
            VACANCY_DESC=description,
            RESUME_BLOCK=resume_block,
            LIE_BLOCK=lie_block,
        )
    except KeyError as e:
        return f"❌ Ошибка в шаблоне промпта: неизвестный плейсхолдер {e}"

    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  700,
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }

    # Try with proxy first, then direct
    attempts = [("proxy", _PROXIES), ("direct", {})]
    last_err: Exception | None = None

    for label, proxies in attempts:
        try:
            log.info("Groq: trying %s route…", label)
            r = requests.post(GROQ_URL, headers=headers, json=payload,
                              proxies=proxies, timeout=30)
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"].strip()
                log.info("Groq response via %s: %d chars", label, len(text))
                return text
            else:
                log.error("Groq API %s (%s): %s", r.status_code, label, r.text[:300])
                return f"❌ Groq API error {r.status_code}"
        except Exception as e:
            log.warning("Groq %s attempt failed: %s", label, e)
            last_err = e

    log.error("Groq: all routes failed. Last error: %s", last_err)
    return (
        "❌ Не удалось подключиться к Groq API.\n"
        "Добавь api.groq.com в правила прокси или проверь VPN."
    )
