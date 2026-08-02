import datetime
import hashlib
import html
import logging
import os
import re
import threading
import time
from logging.handlers import TimedRotatingFileHandler

import telebot
from telebot.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton)

import ai
import db
import hh_api
from ai import DEFAULT_PROMPT_TEMPLATE
from config import (BOT_TOKEN, OWNER_ID, CHECK_INTERVAL_SECONDS,
                    INITIAL_VACANCIES_COUNT, MAX_NEW_VACANCIES, TELEGRAM_PROXY)
from utils.formatting import format_vacancy, format_filter_info

_log_fmt  = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_handlers = [logging.StreamHandler()]
_log_file  = os.getenv("LOG_FILE", "bot.log")
if _log_file:
    _fh = TimedRotatingFileHandler(_log_file, when="midnight", backupCount=7, encoding="utf-8")
    _fh.setFormatter(_log_fmt)
    _handlers.append(_fh)
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=_handlers,
)
log = logging.getLogger(__name__)

if TELEGRAM_PROXY:
    telebot.apihelper.proxy = {"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY}
    log.info("Telegram proxy: %s", TELEGRAM_PROXY)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def _notify_owner(text: str):
    """Push a status message to the owner (used by ai.py for key rotation)."""
    try:
        bot.send_message(OWNER_ID, text)
    except Exception as e:
        log.warning("notify_owner failed: %s", e)


ai.set_notifier(_notify_owner)

# ── Состояния диалога ─────────────────────────────────────────────────────────
user_state: dict = {}

EXPERIENCE_OPTIONS = [
    ("Без опыта",   "noExperience"),
    ("1–3 года",    "between1And3"),
    ("3–6 лет",     "between3And6"),
    ("Более 6 лет", "moreThan6"),
]
EMPLOYMENT_OPTIONS = [
    ("Полная",      "full"),
    ("Частичная",   "part"),
    ("Проектная",   "project"),
    ("Стажировка",  "probation"),
]
SCHEDULE_OPTIONS = [
    ("Полный день", "fullDay"),
    ("Сменный",     "shift"),
    ("Гибкий",      "flexible"),
    ("Удалённо",    "remote"),
    ("Вахта",       "flyInFlyOut"),
]
WORK_FORMAT_OPTIONS = [
    ("Офис",     "ON_SITE"),
    ("Удалёнка", "REMOTE"),
    ("Гибрид",   "HYBRID"),
]

# ── Keyboards ─────────────────────────────────────────────────────────────────

BTN_FILTERS = "📋 Мои фильтры"
BTN_CREATE  = "➕ Создать фильтр"
BTN_RESUME  = "✏️ Редактировать резюме"
BTN_BLOCKED = "🚫 Скрытые компании"
BTN_PROMPT  = "⚙️ Редактировать промт для сопроводительного"


def kb_menu():
    m = ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(KeyboardButton(BTN_FILTERS), KeyboardButton(BTN_CREATE))
    m.row(KeyboardButton(BTN_RESUME))
    m.row(KeyboardButton(BTN_PROMPT))
    m.row(KeyboardButton(BTN_BLOCKED))
    return m


def kb_skip():
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("Пропустить ⏭", callback_data="skip"))
    return m


def kb_keywords():
    """Keyboard shown when bot asks for keywords — suggest via AI + skip."""
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("💡 Предложить ключевые (ИИ)", callback_data="suggest_kw"))
    m.add(InlineKeyboardButton("Пропустить ⏭", callback_data="skip"))
    return m


def kb_multiselect(options, selected, prefix, done_cb, skip_cb=None, back_cb=None):
    m = InlineKeyboardMarkup()
    for label, val in options:
        mark = "✅" if val in selected else "⬜"
        m.add(InlineKeyboardButton(f"{mark} {label}", callback_data=f"{prefix}{val}"))
    btns = [InlineKeyboardButton("✅ Готово", callback_data=done_cb)]
    if skip_cb:
        btns.append(InlineKeyboardButton("Пропустить ⏭", callback_data=skip_cb))
    if back_cb:
        btns.append(InlineKeyboardButton("« Назад", callback_data=back_cb))
    m.row(*btns)
    return m


def kb_filter_list(filters):
    m = InlineKeyboardMarkup()
    for f in filters:
        icon = "✅" if f["is_active"] else "⏸"
        m.add(InlineKeyboardButton(f"{icon} {f['name']}", callback_data=f"open_{f['id']}"))
    m.add(InlineKeyboardButton("➕ Создать фильтр", callback_data="create_filter"))
    return m


def kb_filter_actions(filter_id, is_active):
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_{filter_id}"),
        InlineKeyboardButton("🗑 Удалить",  callback_data=f"del_{filter_id}"),
    )
    toggle = "⏸ Пауза" if is_active else "▶️ Запустить"
    m.add(InlineKeyboardButton(toggle,      callback_data=f"toggle_{filter_id}"))
    m.add(InlineKeyboardButton("« Назад",   callback_data="menu_filters"))
    return m


def kb_edit_menu():
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("Название",       callback_data="ef_name"),
        InlineKeyboardButton("Ключевые слова", callback_data="ef_text"),
    )
    m.row(
        InlineKeyboardButton("Регион",         callback_data="ef_area"),
        InlineKeyboardButton("Зарплата от",    callback_data="ef_salary_from"),
    )
    m.row(
        InlineKeyboardButton("Зарплата до",    callback_data="ef_salary_to"),
        InlineKeyboardButton("С зарплатой",    callback_data="ef_only_with_salary"),
    )
    m.row(
        InlineKeyboardButton("Опыт",           callback_data="ef_experience"),
        InlineKeyboardButton("Занятость",      callback_data="ef_employment"),
        InlineKeyboardButton("График",         callback_data="ef_schedule"),
    )
    m.add(InlineKeyboardButton("🏠 Формат (офис/удал./гибрид)", callback_data="ef_work_format"))
    m.add(InlineKeyboardButton("✅ Готово",    callback_data="ef_done"))
    return m


def kb_vacancy_actions(vacancy_id: str) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("🤖 ИИ-отклик", callback_data=f"ai_{vacancy_id}"))
    m.add(InlineKeyboardButton("🚫 Скрыть все вакансии компании",
                               callback_data=f"blk_{vacancy_id}"))
    return m


def _emp_token(name_norm: str) -> str:
    """Short stable id for a company name.

    callback_data caps at 64 bytes and Cyrillic costs 2 bytes per char, so the
    name itself will not fit — we send a hash and resolve it back on click.
    """
    return hashlib.md5(name_norm.encode("utf-8")).hexdigest()[:16]


def kb_blocked_list(blocked: list) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    for b in blocked:
        label = b["name"] if len(b["name"]) <= 38 else b["name"][:37] + "…"
        m.add(InlineKeyboardButton(f"✖️ {label}",
                                   callback_data=f"unblk_{_emp_token(b['name_norm'])}"))
    return m


def kb_resume_actions(has_resume: bool) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    label = "✏️ Изменить текст" if has_resume else "➕ Добавить резюме"
    m.add(InlineKeyboardButton(label, callback_data="res_edit"))
    if has_resume:
        m.add(InlineKeyboardButton("🗑 Удалить", callback_data="res_del"))
    return m


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_multi(val) -> list:
    if not val:
        return []
    return [v for v in str(val).split(",") if v]


def _serialize_multi(lst: list):
    clean = [v for v in lst if v]
    return ",".join(clean) if clean else None


def is_owner(message_or_id):
    uid = message_or_id if isinstance(message_or_id, int) else message_or_id.from_user.id
    return uid == OWNER_ID


def _enrich_work_format(vacancies):
    """Best-effort: fill v['work_format'] for each vacancy by reading its page.
    Runs page fetches concurrently; failures are silent."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(v):
        try:
            url = v.get("alternate_url", "")
            if url:
                v["work_format"] = ai.fetch_work_format(url)
        except Exception:
            pass

    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(_one, vacancies))
    except Exception as e:
        log.debug("work_format enrich failed: %s", e)


def _employer_of(v: dict) -> str:
    return (v.get("employer") or {}).get("name", "")


def _employer_from_card(card_html: str) -> str:
    """Pull the company name back out of a sent vacancy card.

    format_vacancy renders it as a '🏢 <name>' line. Anything after the card
    (an appended AI letter) is ignored so its text can't be mistaken for a name.
    """
    base = re.split(r"\n\n<pre>", card_html or "")[0]
    m = re.search(r"^🏢\s*(.+)$", base, re.MULTILINE)
    return html.unescape(m.group(1).strip()) if m else ""


def _dedupe(vacancies):
    """Pick what to actually send: no repeats, nothing already sent, no blocked employers.

    Returns (sendable, skip_ids). seen_vacancies is per-filter, so a vacancy
    matching two filters would otherwise be sent once per filter — the global
    sent-log is the real guard. skip_ids still get marked seen so the checker
    stops re-examining them.
    """
    unique, batch_ids = [], set()
    for v in vacancies:
        vid = v["id"]
        if vid in batch_ids:
            continue
        batch_ids.add(vid)
        unique.append(v)

    already = db.get_sent(batch_ids)
    blocked = db.get_blocked_norms()

    sendable, skip_ids = [], set(already)
    for v in unique:
        if v["id"] in already:
            continue
        if blocked and db.norm_employer(_employer_of(v)) in blocked:
            skip_ids.add(v["id"])
            log.info("Blocked employer, skipping %s (%s)", v["id"], _employer_of(v))
            continue
        sendable.append(v)

    skipped = len(vacancies) - len(sendable)
    if skipped:
        log.info("Filtered out %d vacancies (duplicate / already sent / blocked)", skipped)
    return sendable, skip_ids


def send_vacancies(filter_id, vacancies, header=None):
    """Send vacancy cards to the owner. No noisy header unless explicitly given."""
    vacancies, skip_ids = _dedupe(vacancies)

    # Skipped ones still count as seen for this filter, so we stop re-checking them.
    if skip_ids:
        db.mark_seen(filter_id, list(skip_ids))

    if not vacancies:
        return

    if header:
        try:
            bot.send_message(OWNER_ID, header)
        except Exception as e:
            log.error("Failed to send header for filter %s: %s", filter_id, e)

    _enrich_work_format(vacancies)

    for v in vacancies:
        try:
            bot.send_message(
                OWNER_ID,
                format_vacancy(v),
                disable_web_page_preview=True,
                reply_markup=kb_vacancy_actions(v["id"]),
            )
        except Exception as e:
            log.error("Failed to send vacancy %s: %s", v.get("id"), e)
            continue
        # Record per vacancy, not per batch: a crash mid-batch must not resend.
        db.mark_sent([v["id"]])
        db.mark_seen(filter_id, [v["id"]])


def state_clear(uid):
    user_state.pop(uid, None)


def state_set(uid, step, **data):
    user_state[uid] = {"step": step, "data": data}


def state_get(uid):
    return user_state.get(uid, {})


def show_edit_menu(cid, mid, filter_id, send_new=False):
    f = db.get_filter(filter_id)
    if not f:
        return
    text = f"✏️ Редактирование: <b>{f['name']}</b>\nВыберите, что изменить:"
    if send_new:
        bot.send_message(cid, text, reply_markup=kb_edit_menu())
    else:
        bot.edit_message_text(text, cid, mid, reply_markup=kb_edit_menu())


def _decode_file(data: bytes) -> str:
    """Decode bytes from uploaded .txt file."""
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _show_resume(cid: int, mid: int = None):
    r = db.get_resume()
    if r:
        preview = r["text"][:1500] + "\n…(обрезано)" if len(r["text"]) > 1500 else r["text"]
        text = (f"📄 <b>Моё резюме</b> ({len(r['text'])} символов)\n\n"
                f"<code>{html.escape(preview)}</code>")
    else:
        text = ("📄 <b>Моё резюме</b>\n\nРезюме ещё не добавлено.\n"
                "Оно используется для сопроводительных писем.")
    kb = kb_resume_actions(bool(r))
    if mid:
        try:
            bot.edit_message_text(text, cid, mid, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(cid, text, reply_markup=kb)


def _show_blocked(cid: int):
    blocked = db.get_blocked_employers()
    if not blocked:
        bot.send_message(
            cid,
            "🚫 <b>Скрытые компании</b>\n\nСписок пуст.\n\n"
            "Чтобы скрыть компанию — нажми «🚫 Скрыть все вакансии компании» "
            "под любой её вакансией.",
        )
        return
    bot.send_message(
        cid,
        f"🚫 <b>Скрытые компании</b> ({len(blocked)}):\n\nНажми, чтобы вернуть.",
        reply_markup=kb_blocked_list(blocked),
    )


def _show_prompt_menu(cid: int):
    saved = db.get_setting("groq_prompt")
    if saved:
        preview = saved[:1000] + "\n…(обрезано)" if len(saved) > 1000 else saved
        status = "✏️ <b>Кастомный промпт:</b>"
    else:
        preview = DEFAULT_PROMPT_TEMPLATE[:1000] + "\n…(обрезано)" if len(DEFAULT_PROMPT_TEMPLATE) > 1000 else DEFAULT_PROMPT_TEMPLATE
        status = "📋 <b>Стандартный промпт:</b>"

    text = (
        f"⚙️ <b>Промт для сопроводительного</b>\n\n"
        f"{status}\n"
        f"<code>{preview}</code>\n\n"
        f"<b>Плейсхолдеры:</b>\n"
        f"<code>{{VACANCY_CARD}}</code> — карточка вакансии\n"
        f"<code>{{VACANCY_DESC}}</code> — описание со страницы\n"
        f"<code>{{RESUME_BLOCK}}</code> — резюме (подставляется само)"
    )
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("✏️ Изменить текстом",    callback_data="prom_edit_text"))
    m.add(InlineKeyboardButton("📄 Загрузить .txt файл", callback_data="prom_edit_file"))
    if saved:
        m.add(InlineKeyboardButton("🔄 Сбросить на стандартный", callback_data="prom_reset"))
    else:
        m.add(InlineKeyboardButton("📋 Установить стандартный как основу", callback_data="prom_load_default"))
    bot.send_message(cid, text, reply_markup=m)


def _start_create(uid: int, cid: int):
    state_set(uid, "create_name", nf={})
    bot.send_message(cid, "Введите название фильтра:")


def _show_filters(cid: int):
    filters = db.get_all_filters()
    if not filters:
        bot.send_message(cid, "Фильтров пока нет.", reply_markup=kb_menu())
        return
    bot.send_message(cid, "📋 <b>Ваши фильтры:</b>",
                     reply_markup=kb_filter_list(filters))


# ── Commands ──────────────────────────────────────────────────────────────────

GREETING = """\
👋 <b>Привет! Я слежу за вакансиями на hh.ru.</b>

Ты описываешь, что ищешь — я каждые 5 минут проверяю новые вакансии и присылаю подходящие. Одна вакансия приходит только один раз.

Сначала создай фильтр - новые вакансии буду присылаться сразу

<b>Что ещё умею</b>
🤖 ИИ-отклик — кнопка под каждой вакансией. Напишу сопроводительное по твоему резюме. Для этого добавь резюме в соотв. разделе
⚙️ Промпт ИИ — переписать инструкцию, по которой пишутся письма
📋 Мои фильтры — менять, ставить на паузу, удалять

<b>Чтобы ИИ-отклик работал</b>
Нужен бесплатный ключ Groq (console.groq.com/keys) — вписать его в переменную окружения <code>GROQ_API_KEY</code>.
Ключей можно добавить до пяти: <code>GROQ_API_KEY_1</code> … <code>GROQ_API_KEY_5</code>. Когда лимит одного заканчивается, я сам переключаюсь на следующий.

Бот создан @dsf637"""


@bot.message_handler(commands=["start", "help"])
def cmd_start(msg):
    if not is_owner(msg):
        return
    state_clear(msg.from_user.id)
    bot.send_message(msg.chat.id, GREETING, disable_web_page_preview=True,
                     reply_markup=kb_menu())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(msg):
    if not is_owner(msg):
        return
    state_clear(msg.from_user.id)
    bot.send_message(msg.chat.id, "❌ Отменено.", reply_markup=kb_menu())


@bot.message_handler(commands=["filters"])
def cmd_filters(msg):
    if not is_owner(msg):
        return
    state_clear(msg.from_user.id)
    _show_filters(msg.chat.id)


@bot.message_handler(commands=["newfilter"])
def cmd_newfilter(msg):
    if not is_owner(msg):
        return
    _start_create(msg.from_user.id, msg.chat.id)


@bot.message_handler(commands=["blocked"])
def cmd_blocked(msg):
    if not is_owner(msg):
        return
    state_clear(msg.from_user.id)
    _show_blocked(msg.chat.id)


# ── Document handler (for .txt resume / prompt uploads) ───────────────────────

@bot.message_handler(content_types=["document"])
def handle_document(msg):
    if not is_owner(msg):
        return
    uid = msg.from_user.id
    cid = msg.chat.id
    st  = state_get(uid)
    step = st.get("step")

    allowed_steps = ("resume_edit", "prompt_edit_file")
    if step not in allowed_steps:
        bot.send_message(cid, "📄 Файл получен, но сейчас файл не ожидается. "
                              "Используй /cancel для сброса состояния.")
        return

    if not msg.document.file_name.lower().endswith(".txt"):
        bot.send_message(cid, "❌ Нужен файл в формате .txt")
        return

    try:
        file_info = bot.get_file(msg.document.file_id)
        raw = bot.download_file(file_info.file_path)
        text = _decode_file(raw).strip()
    except Exception as e:
        bot.send_message(cid, f"❌ Не удалось прочитать файл: {e}")
        return

    if not text:
        bot.send_message(cid, "❌ Файл пустой.")
        return

    _apply_resume_or_prompt_text(uid, cid, text, step, st)


def _apply_resume_or_prompt_text(uid, cid, text, step, st):
    """Shared logic for saving text whether it came from message or file."""
    if step == "resume_edit":
        db.save_resume(text)
        state_clear(uid)
        bot.send_message(cid, f"✅ Резюме сохранено ({len(text)} символов).")
        _show_resume(cid)

    elif step == "prompt_edit_file":
        # Validate placeholders
        for ph in ("{VACANCY_CARD}", "{VACANCY_DESC}"):
            if ph not in text:
                bot.send_message(
                    cid,
                    f"⚠️ В промпте нет обязательного плейсхолдера <code>{ph}</code>. "
                    f"Добавь его и загрузи снова.",
                )
                return
        db.set_setting("groq_prompt", text)
        state_clear(uid)
        bot.send_message(cid, f"✅ Промпт обновлён ({len(text)} символов).")


# ── Text handler ──────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(msg):
    if msg.text and msg.text.startswith("/"):
        return
    if not is_owner(msg):
        return

    uid  = msg.from_user.id
    cid  = msg.chat.id
    text = msg.text.strip() if msg.text else ""
    st   = state_get(uid)
    step = st.get("step")

    log.info("TEXT uid=%s step=%s text=%r", uid, step, text[:40])

    # ── Persistent menu buttons ───────────────────────────────────────────────
    if text == BTN_FILTERS:
        state_clear(uid)
        _show_filters(cid)
        return
    if text == BTN_CREATE:
        state_clear(uid)
        _start_create(uid, cid)
        return
    if text == BTN_RESUME:
        state_clear(uid)
        _show_resume(cid)
        return
    if text == BTN_BLOCKED:
        state_clear(uid)
        _show_blocked(cid)
        return
    if text == BTN_PROMPT:
        state_clear(uid)
        _show_prompt_menu(cid)
        return

    if not step:
        # No active dialog — if it's an hh.ru vacancy link, write a cover letter.
        url = _normalize_vacancy_url(text)
        if url:
            _cover_from_link(cid, url)
        return

    try:
        _handle_text_inner(uid, cid, text, step, st)
    except Exception as e:
        log.error("handle_text error step=%s: %s", step, e, exc_info=True)
        try:
            bot.send_message(cid, "⚠️ Что-то пошло не так, попробуй ещё раз или /cancel")
        except Exception:
            pass


def _handle_text_inner(uid, cid, text, step, st):
    d  = st.get("data", {})
    nf = d.get("nf", {})

    # ── Resume ────────────────────────────────────────────────────────────────

    if step == "resume_edit":
        _apply_resume_or_prompt_text(uid, cid, text, step, st)
        return

    # ── Prompt edit ───────────────────────────────────────────────────────────

    if step == "prompt_edit_text":
        for ph in ("{VACANCY_CARD}", "{VACANCY_DESC}"):
            if ph not in text:
                bot.send_message(
                    cid,
                    f"⚠️ В промпте нет обязательного плейсхолдера <code>{ph}</code>. "
                    f"Добавь его и отправь снова (или /cancel).",
                )
                return
        db.set_setting("groq_prompt", text)
        state_clear(uid)
        bot.send_message(cid, f"✅ Промпт обновлён ({len(text)} символов).")
        return

    # ── Filter CREATE flow ────────────────────────────────────────────────────

    if step == "create_name":
        nf["name"] = text
        state_set(uid, "create_text", nf=nf)
        bot.send_message(
            cid,
            "Ключевые слова для поиска:\n\n"
            "• Одна группа: <code>Python разработчик</code>\n"
            "• Несколько групп через запятую — каждая ищется отдельно:\n"
            "  <code>product analyst, продуктовый аналитик, data analyst</code>\n"
            "• Точная фраза в кавычках: <code>\"product manager\"</code>",
            reply_markup=kb_keywords(),
        )

    elif step == "create_text":
        # Keep commas — they separate independent keyword groups
        nf["text"] = text.strip()
        state_set(uid, "create_area", nf=nf)
        bot.send_message(cid, "Введите название города или региона:", reply_markup=kb_skip())

    elif step == "create_area":
        areas = hh_api.search_areas(text)
        if not areas:
            bot.send_message(cid, "Регион не найден. Попробуйте ещё раз или пропустите:",
                             reply_markup=kb_skip())
            return
        state_set(uid, "create_area_select", nf=nf, areas=areas)
        m = InlineKeyboardMarkup()
        for i, a in enumerate(areas):
            m.add(InlineKeyboardButton(a["text"], callback_data=f"area_{i}"))
        m.add(InlineKeyboardButton("Пропустить ⏭", callback_data="skip"))
        bot.send_message(cid, "Выберите регион:", reply_markup=m)

    elif step == "create_salary_from":
        clean = text.replace(" ", "").replace("\xa0", "")
        if not clean.isdigit():
            bot.send_message(cid, "Введите целое число или пропустите:", reply_markup=kb_skip())
            return
        nf["salary_from"] = int(clean)
        state_set(uid, "create_salary_to", nf=nf)
        bot.send_message(cid, "Зарплата до (₽):", reply_markup=kb_skip())

    elif step == "create_salary_to":
        clean = text.replace(" ", "").replace("\xa0", "")
        if not clean.isdigit():
            bot.send_message(cid, "Введите целое число или пропустите:", reply_markup=kb_skip())
            return
        nf["salary_to"] = int(clean)
        state_set(uid, "create_only_with_salary", nf=nf)
        m = InlineKeyboardMarkup()
        m.row(InlineKeyboardButton("Да",               callback_data="yes"),
              InlineKeyboardButton("Нет / Пропустить", callback_data="skip"))
        bot.send_message(cid, "Только вакансии с указанной зарплатой?", reply_markup=m)

    # ── Filter EDIT text input ────────────────────────────────────────────────

    elif step == "edit_text_input":
        filter_id = d["filter_id"]
        field     = d["field"]

        if field in ("salary_from", "salary_to"):
            clean = text.replace(" ", "").replace("\xa0", "")
            if text != "-" and not clean.isdigit():
                bot.send_message(cid, "Введите число или «-» для очистки:")
                return
            val = None if text == "-" else int(clean)
        elif field == "text":
            val = None if text == "-" else text.strip()
        else:
            val = None if text == "-" else text

        db.update_filter(filter_id, **{field: val})
        state_set(uid, "edit_menu", filter_id=filter_id)
        show_edit_menu(cid, None, filter_id, send_new=True)

    elif step == "edit_area_search":
        filter_id = d["filter_id"]
        if text == "-":
            db.update_filter(filter_id, area_id=None, area_name=None)
            state_set(uid, "edit_menu", filter_id=filter_id)
            show_edit_menu(cid, None, filter_id, send_new=True)
            return
        areas = hh_api.search_areas(text)
        if not areas:
            bot.send_message(cid, "Регион не найден. Ещё раз или «-» для очистки:")
            return
        state_set(uid, "edit_area_select", filter_id=filter_id, areas=areas)
        m = InlineKeyboardMarkup()
        for i, a in enumerate(areas):
            m.add(InlineKeyboardButton(a["text"], callback_data=f"ea_{i}"))
        m.add(InlineKeyboardButton("« Отмена", callback_data="ea_back"))
        bot.send_message(cid, "Выберите регион:", reply_markup=m)


# ── Callbacks ─────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: True)
def handle_callback(call):
    if not is_owner(call.from_user.id):
        return
    try:
        _cb(call)
    except Exception as e:
        log.error("Callback error (data=%s): %s", call.data, e, exc_info=True)
        try:
            bot.answer_callback_query(call.id, "⚠️ Ошибка, попробуй ещё раз")
        except Exception:
            pass


def _cb(call):
    uid  = call.from_user.id
    cid  = call.message.chat.id
    mid  = call.message.message_id
    data = call.data
    st   = state_get(uid)
    step = st.get("step")
    d    = st.get("data", {})
    nf   = d.get("nf", {})

    log.info("CB uid=%s step=%s data=%s", uid, step, data)
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        log.warning("answer_callback_query failed: %s", e)

    # ── noop ──────────────────────────────────────────────────────────────────
    if data == "noop":
        return

    # ── Filters menu ──────────────────────────────────────────────────────────

    if data == "menu_filters":
        state_clear(uid)
        filters = db.get_all_filters()
        if not filters:
            bot.edit_message_text("Фильтров пока нет.", cid, mid)
            return
        bot.edit_message_text("📋 <b>Ваши фильтры:</b>", cid, mid,
                              reply_markup=kb_filter_list(filters))
        return

    if data == "create_filter":
        state_set(uid, "create_name", nf={})
        bot.edit_message_text("Введите название фильтра:", cid, mid)
        return

    if data.startswith("open_"):
        state_clear(uid)
        filter_id = int(data[5:])
        f = db.get_filter(filter_id)
        if not f:
            bot.edit_message_text("Фильтр не найден.", cid, mid)
            return
        bot.edit_message_text(format_filter_info(f), cid, mid,
                              reply_markup=kb_filter_actions(filter_id, bool(f["is_active"])))
        return

    if data.startswith("toggle_"):
        filter_id  = int(data[7:])
        new_active = db.toggle_filter(filter_id)
        f = db.get_filter(filter_id)
        if f:
            bot.edit_message_text(format_filter_info(f), cid, mid,
                                  reply_markup=kb_filter_actions(filter_id, new_active))
        return

    if data.startswith("del_") and not data.startswith("delok_"):
        filter_id = int(data[4:])
        f = db.get_filter(filter_id)
        name = f["name"] if f else "?"
        m = InlineKeyboardMarkup()
        m.row(
            InlineKeyboardButton("🗑 Да, удалить", callback_data=f"delok_{filter_id}"),
            InlineKeyboardButton("Отмена",         callback_data=f"open_{filter_id}"),
        )
        bot.edit_message_text(f"Удалить фильтр «{name}»?", cid, mid, reply_markup=m)
        return

    if data.startswith("delok_"):
        filter_id = int(data[6:])
        db.delete_filter(filter_id)
        state_clear(uid)
        filters = db.get_all_filters()
        if filters:
            bot.edit_message_text("🗑 Удалён.\n\n📋 <b>Ваши фильтры:</b>", cid, mid,
                                  reply_markup=kb_filter_list(filters))
        else:
            bot.edit_message_text("🗑 Удалён. Фильтров больше нет.", cid, mid)
        return

    if data.startswith("edit_"):
        filter_id = int(data[5:])
        f = db.get_filter(filter_id)
        if not f:
            bot.edit_message_text("Фильтр не найден.", cid, mid)
            return
        state_set(uid, "edit_menu", filter_id=filter_id)
        bot.edit_message_text(
            f"✏️ Редактирование: <b>{f['name']}</b>\nВыберите, что изменить:",
            cid, mid, reply_markup=kb_edit_menu()
        )
        return

    if data.startswith("ef_") and step == "edit_menu":
        filter_id = d["filter_id"]
        field = data[3:]

        if field == "done":
            state_clear(uid)
            f = db.get_filter(filter_id)
            if f:
                bot.edit_message_text(
                    f"✅ Сохранено!\n\n{format_filter_info(f)}", cid, mid,
                    reply_markup=kb_filter_actions(filter_id, bool(f["is_active"]))
                )
            return

        if field in ("name", "text", "salary_from", "salary_to"):
            prompts = {
                "name":        "Введите новое название:",
                "text":        "Ключевые слова через пробел (или «-» для очистки):",
                "salary_from": "Зарплата от (или «-» для очистки):",
                "salary_to":   "Зарплата до (или «-» для очистки):",
            }
            state_set(uid, "edit_text_input", filter_id=filter_id, field=field)
            bot.edit_message_text(prompts[field], cid, mid)
            return

        if field == "area":
            state_set(uid, "edit_area_search", filter_id=filter_id)
            bot.edit_message_text("Введите город/регион (или «-» для очистки):", cid, mid)
            return

        if field == "only_with_salary":
            m = InlineKeyboardMarkup()
            m.row(InlineKeyboardButton("Да",  callback_data="ev_ows_yes"),
                  InlineKeyboardButton("Нет", callback_data="ev_ows_no"))
            m.add(InlineKeyboardButton("« Назад", callback_data="ev_back"))
            bot.edit_message_text("Только вакансии с зарплатой?", cid, mid, reply_markup=m)
            return

        if field in ("experience", "employment", "schedule", "work_format"):
            f_data = db.get_filter(filter_id)
            sel = _parse_multi(f_data.get(field)) if f_data else []
            state_set(uid, "edit_ms", filter_id=filter_id, field=field, sel=sel)
            labels  = {"experience": "Опыт работы:", "employment": "Тип занятости:",
                       "schedule": "График работы:", "work_format": "Формат работы:"}
            opts    = {"experience": EXPERIENCE_OPTIONS, "employment": EMPLOYMENT_OPTIONS,
                       "schedule": SCHEDULE_OPTIONS, "work_format": WORK_FORMAT_OPTIONS}
            bot.edit_message_text(labels[field], cid, mid,
                                  reply_markup=kb_multiselect(opts[field], sel, "ems_", "ems_done", back_cb="ems_back"))
            return

    if data.startswith("ev_") and step in ("edit_menu", "edit_text_input", "edit_area_search"):
        filter_id = d.get("filter_id")

        if data == "ev_back":
            state_set(uid, "edit_menu", filter_id=filter_id)
            show_edit_menu(cid, mid, filter_id)
            return

        if data in ("ev_ows_yes", "ev_ows_no"):
            db.update_filter(filter_id, only_with_salary=1 if data == "ev_ows_yes" else 0)

        state_set(uid, "edit_menu", filter_id=filter_id)
        show_edit_menu(cid, mid, filter_id)
        return

    if data.startswith("ea_") and step == "edit_area_select":
        filter_id = d["filter_id"]
        if data == "ea_back":
            state_set(uid, "edit_menu", filter_id=filter_id)
            show_edit_menu(cid, mid, filter_id)
            return
        idx   = int(data[3:])
        areas = d.get("areas", [])
        if idx < len(areas):
            db.update_filter(filter_id, area_id=areas[idx]["id"], area_name=areas[idx]["text"])
        state_set(uid, "edit_menu", filter_id=filter_id)
        show_edit_menu(cid, mid, filter_id)
        return

    # ── CREATE flow callbacks ─────────────────────────────────────────────────

    if step == "create_text":
        if data == "skip":
            nf["text"] = None
            state_set(uid, "create_area", nf=nf)
            bot.edit_message_text("Введите название города или региона:", cid, mid,
                                  reply_markup=kb_skip())
            return
        if data == "suggest_kw":
            topic = nf.get("name", "")
            # Show spinner
            bot.edit_message_reply_markup(cid, mid, reply_markup=InlineKeyboardMarkup())
            def _suggest():
                result = ai.suggest_keywords(topic)
                bot.send_message(
                    cid,
                    f"💡 <b>Предложенные ключевые слова:</b>\n\n<code>{result}</code>\n\n"
                    f"Скопируй нужные и отправь своё сообщение с ключевыми словами "
                    f"(или измени список):",
                    reply_markup=kb_keywords(),
                )
            threading.Thread(target=_suggest, daemon=True).start()
            return
        return

    if step == "create_area":
        if data == "skip":
            nf["area_id"] = None
            nf["area_name"] = None
            state_set(uid, "create_salary_from", nf=nf)
            bot.edit_message_text("Зарплата от (₽):", cid, mid, reply_markup=kb_skip())
        return

    if step == "create_area_select":
        areas = d.get("areas", [])
        if data == "skip":
            nf["area_id"] = None
            nf["area_name"] = None
        elif data.startswith("area_"):
            idx = int(data[5:])
            if idx < len(areas):
                nf["area_id"]   = areas[idx]["id"]
                nf["area_name"] = areas[idx]["text"]
        state_set(uid, "create_salary_from", nf=nf)
        bot.edit_message_text("Зарплата от (₽):", cid, mid, reply_markup=kb_skip())
        return

    if step == "create_salary_from":
        if data == "skip":
            nf["salary_from"] = None
            state_set(uid, "create_salary_to", nf=nf)
            bot.edit_message_text("Зарплата до (₽):", cid, mid, reply_markup=kb_skip())
        return

    if step == "create_salary_to":
        if data == "skip":
            nf["salary_to"] = None
            state_set(uid, "create_only_with_salary", nf=nf)
            m = InlineKeyboardMarkup()
            m.row(InlineKeyboardButton("Да",               callback_data="yes"),
                  InlineKeyboardButton("Нет / Пропустить", callback_data="skip"))
            bot.edit_message_text("Только вакансии с указанной зарплатой?", cid, mid, reply_markup=m)
        return

    if step == "create_only_with_salary":
        nf["only_with_salary"] = (data == "yes")
        state_set(uid, "create_experience", nf=nf, sel=[])
        bot.edit_message_text("Опыт работы:", cid, mid,
                              reply_markup=kb_multiselect(EXPERIENCE_OPTIONS, [], "cxp_", "cxp_done", "cxp_skip"))
        return

    if step == "create_experience":
        sel = list(d.get("sel", []))
        if data in ("cxp_skip", "cxp_done"):
            nf["experience"] = None if data == "cxp_skip" else _serialize_multi(sel)
            state_set(uid, "create_employment", nf=nf, sel=[])
            bot.edit_message_text("Тип занятости:", cid, mid,
                                  reply_markup=kb_multiselect(EMPLOYMENT_OPTIONS, [], "cmp_", "cmp_done", "cmp_skip"))
        elif data.startswith("cxp_"):
            val = data[4:]
            if val in sel: sel.remove(val)
            else: sel.append(val)
            state_set(uid, "create_experience", nf=nf, sel=sel)
            bot.edit_message_reply_markup(cid, mid,
                reply_markup=kb_multiselect(EXPERIENCE_OPTIONS, sel, "cxp_", "cxp_done", "cxp_skip"))
        return

    if step == "create_employment":
        sel = list(d.get("sel", []))
        if data in ("cmp_skip", "cmp_done"):
            nf["employment"] = None if data == "cmp_skip" else _serialize_multi(sel)
            state_set(uid, "create_schedule", nf=nf, sel=[])
            bot.edit_message_text("График работы:", cid, mid,
                                  reply_markup=kb_multiselect(SCHEDULE_OPTIONS, [], "csp_", "csp_done", "csp_skip"))
        elif data.startswith("cmp_"):
            val = data[4:]
            if val in sel: sel.remove(val)
            else: sel.append(val)
            state_set(uid, "create_employment", nf=nf, sel=sel)
            bot.edit_message_reply_markup(cid, mid,
                reply_markup=kb_multiselect(EMPLOYMENT_OPTIONS, sel, "cmp_", "cmp_done", "cmp_skip"))
        return

    if step == "create_schedule":
        sel = list(d.get("sel", []))
        if data.startswith("csp_") and data not in ("csp_skip", "csp_done"):
            val = data[4:]
            if val in sel: sel.remove(val)
            else: sel.append(val)
            state_set(uid, "create_schedule", nf=nf, sel=sel)
            bot.edit_message_reply_markup(cid, mid,
                reply_markup=kb_multiselect(SCHEDULE_OPTIONS, sel, "csp_", "csp_done", "csp_skip"))
            return
        nf["schedule"] = None if data == "csp_skip" else _serialize_multi(sel)
        state_set(uid, "create_work_format", nf=nf, sel=[])
        bot.edit_message_text("Формат работы (офис / удалёнка / гибрид):", cid, mid,
                              reply_markup=kb_multiselect(WORK_FORMAT_OPTIONS, [], "cwf_", "cwf_done", "cwf_skip"))
        return

    if step == "create_work_format":
        sel = list(d.get("sel", []))
        if data.startswith("cwf_") and data not in ("cwf_skip", "cwf_done"):
            val = data[4:]
            if val in sel: sel.remove(val)
            else: sel.append(val)
            state_set(uid, "create_work_format", nf=nf, sel=sel)
            bot.edit_message_reply_markup(cid, mid,
                reply_markup=kb_multiselect(WORK_FORMAT_OPTIONS, sel, "cwf_", "cwf_done", "cwf_skip"))
            return
        nf["work_format"] = None if data == "cwf_skip" else _serialize_multi(sel)
        state_set(uid, "create_confirm", nf=nf)
        summary = format_filter_info({**nf, "is_active": 1})
        m = InlineKeyboardMarkup()
        m.row(InlineKeyboardButton("✅ Создать", callback_data="confirm"),
              InlineKeyboardButton("❌ Отмена",  callback_data="cancel_create"))
        bot.edit_message_text(f"Проверьте фильтр:\n\n{summary}", cid, mid, reply_markup=m)
        return

    if step == "create_confirm":
        if data == "cancel_create":
            state_clear(uid)
            bot.edit_message_text("❌ Создание отменено.", cid, mid)
            bot.send_message(cid, "Используй кнопки ниже ↓", reply_markup=kb_menu())
            return
        if data == "confirm":
            state_clear(uid)
            filter_id = db.create_filter(**nf)
            bot.edit_message_text(f"✅ Фильтр <b>{nf['name']}</b> создан!", cid, mid)
            bot.send_message(cid, "⏳ Ищу свежие вакансии...", reply_markup=kb_menu())

            # Пометить ВСЕ вакансии за 30 дней как просмотренные,
            # чтобы периодический чекер не присылал старьё
            old = hh_api.search_vacancies(nf, per_page=100)
            if old:
                db.mark_seen(filter_id, [v["id"] for v in old])

            # Показать только свежие вакансии (за последний день)
            fresh = hh_api.search_vacancies(nf, per_page=INITIAL_VACANCIES_COUNT, periods=[1])
            if fresh:
                send_vacancies(filter_id, fresh[:INITIAL_VACANCIES_COUNT],
                               f"🔍 Свежие вакансии по фильтру <b>{nf['name']}</b>:")
            else:
                bot.send_message(cid, "Свежих вакансий за сегодня нет. Буду проверять каждые 5 минут.")
        return

    # ── Edit multi-select (experience / employment / schedule) ────────────────

    if step == "edit_ms" and data.startswith("ems_"):
        filter_id = d["filter_id"]
        field     = d["field"]
        sel       = list(d.get("sel", []))
        opts_map  = {"experience": EXPERIENCE_OPTIONS, "employment": EMPLOYMENT_OPTIONS,
                     "schedule": SCHEDULE_OPTIONS, "work_format": WORK_FORMAT_OPTIONS}

        if data == "ems_done":
            db.update_filter(filter_id, **{field: _serialize_multi(sel)})
            state_set(uid, "edit_menu", filter_id=filter_id)
            show_edit_menu(cid, mid, filter_id)
        elif data == "ems_back":
            state_set(uid, "edit_menu", filter_id=filter_id)
            show_edit_menu(cid, mid, filter_id)
        else:
            val = data[4:]
            if val in sel: sel.remove(val)
            else: sel.append(val)
            state_set(uid, "edit_ms", filter_id=filter_id, field=field, sel=sel)
            bot.edit_message_reply_markup(cid, mid,
                reply_markup=kb_multiselect(opts_map[field], sel, "ems_", "ems_done", back_cb="ems_back"))
        return

    # ── Vacancy actions ───────────────────────────────────────────────────────

    if data.startswith("blk_"):
        employer = _employer_from_card(
            call.message.html_text if hasattr(call.message, "html_text")
            else (call.message.text or "")
        )
        if not employer:
            bot.answer_callback_query(call.id, "Не смог определить компанию 🤷")
            return

        db.block_employer(employer)
        try:
            bot.delete_message(cid, mid)
        except Exception as e:
            log.warning("Could not delete blocked card: %s", e)
            bot.edit_message_reply_markup(cid, mid, reply_markup=None)
        bot.send_message(
            cid,
            f"🚫 Компания <b>{html.escape(employer)}</b> скрыта.\n"
            f"Её вакансии больше не придут. Список: /blocked",
        )
        return

    if data.startswith("unblk_"):
        token = data[6:]
        match = next((b for b in db.get_blocked_employers()
                      if _emp_token(b["name_norm"]) == token), None)
        if not match:
            bot.answer_callback_query(call.id, "Компания не найдена")
            return
        db.unblock_employer(match["name_norm"])
        blocked = db.get_blocked_employers()
        if blocked:
            bot.edit_message_text(
                f"✅ <b>{html.escape(match['name'])}</b> разблокирована.\n\n"
                f"🚫 <b>Скрытые компании</b> ({len(blocked)}):",
                cid, mid, reply_markup=kb_blocked_list(blocked),
            )
        else:
            bot.edit_message_text(
                f"✅ <b>{html.escape(match['name'])}</b> разблокирована.\n\n"
                f"Скрытых компаний больше нет.", cid, mid,
            )
        return

    # ── AI cover letter (from a vacancy card) ─────────────────────────────────

    if data.startswith("ai_"):
        # User clicked "🤖 ИИ-отклик" — generate using saved settings, append below card
        vacancy_id = data[3:]
        msg_html = call.message.html_text if hasattr(call.message, "html_text") else (call.message.text or "")
        url_m = re.search(r'href="(https://[^"]*hh\.ru/vacancy/\d+)', msg_html)
        vacancy_url = url_m.group(1) if url_m else f"https://hh.ru/vacancy/{vacancy_id}"
        _cover_from_card(cid, mid, vacancy_url, vacancy_id, msg_html)
        return

    # ── Resume ────────────────────────────────────────────────────────────────

    if data == "res_edit":
        state_set(uid, "resume_edit")
        bot.edit_message_text(
            "📝 Отправь текст резюме — сообщением или .txt файлом.\n\n"
            "Он полностью заменит текущий. /cancel — отмена.",
            cid, mid,
        )
        return

    if data == "res_del":
        m = InlineKeyboardMarkup()
        m.row(
            InlineKeyboardButton("🗑 Да, удалить", callback_data="res_delok"),
            InlineKeyboardButton("Отмена",         callback_data="res_back"),
        )
        bot.edit_message_text("Удалить резюме?", cid, mid, reply_markup=m)
        return

    if data == "res_delok":
        db.delete_resume()
        state_clear(uid)
        bot.edit_message_text("🗑 Резюме удалено.", cid, mid,
                              reply_markup=kb_resume_actions(False))
        return

    if data == "res_back":
        _show_resume(cid, mid)
        return

    # ── Prompt management callbacks ───────────────────────────────────────────

    if data == "prom_edit_text":
        state_set(uid, "prompt_edit_text")
        bot.edit_message_text(
            "✏️ Отправь новый текст промпта.\n\n"
            "Обязательные плейсхолдеры: <code>{VACANCY_CARD}</code>, <code>{VACANCY_DESC}</code>\n"
            "Необязательные: <code>{RESUME_BLOCK}</code>, <code>{LIE_BLOCK}</code>\n\n"
            "Или /cancel для отмены.",
            cid, mid,
        )
        return

    if data == "prom_edit_file":
        state_set(uid, "prompt_edit_file")
        bot.edit_message_text(
            "📄 Загрузи промпт как .txt файл.\n\n"
            "Обязательные плейсхолдеры: <code>{VACANCY_CARD}</code>, <code>{VACANCY_DESC}</code>\n"
            "Необязательные: <code>{RESUME_BLOCK}</code>, <code>{LIE_BLOCK}</code>\n\n"
            "Или /cancel для отмены.",
            cid, mid,
        )
        return

    if data == "prom_reset":
        db.del_setting("groq_prompt")
        bot.edit_message_text("✅ Промпт сброшен на стандартный.", cid, mid)
        return

    if data == "prom_load_default":
        db.set_setting("groq_prompt", DEFAULT_PROMPT_TEMPLATE)
        bot.edit_message_text(
            "✅ Стандартный промпт скопирован как кастомный.\n\n"
            "Теперь открой ⚙️ Промпт ИИ и отредактируй его под себя.",
            cid, mid,
        )
        return


# ── AI generation (background thread) ────────────────────────────────────────

def _normalize_vacancy_url(raw: str) -> str | None:
    """Extract / normalize an hh.ru vacancy URL (or bare id) from user input."""
    raw = (raw or "").strip()
    m = re.search(r"((?:https?://)?[\w.-]*hh\.ru/vacancy/\d+)", raw)
    if m:
        url = m.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return url
    if raw.isdigit() and len(raw) >= 5:
        return f"https://hh.ru/vacancy/{raw}"
    return None


def _build_letter(vacancy_url: str) -> str:
    """Synchronously fetch the vacancy page and produce the cover letter text."""
    prompt_template = db.get_setting("groq_prompt") or None
    page_text = ai.fetch_vacancy_page(vacancy_url)
    return ai.generate_cover_letter(
        vacancy_card_text="",
        vacancy_page_text=page_text,
        resume_text=db.get_resume_text() or None,
        prompt_template=prompt_template,
    )


def _cover_from_link(cid: int, url: str):
    """Plain link pasted in chat → reply with ONLY the cover letter (copyable)."""
    host = bot.send_message(cid, "✉️ Пишу сопроводительное…")
    mid = host.message_id

    def _worker():
        letter = _build_letter(url)
        safe = html.escape(letter)
        try:
            # Only the letter, inside a code block → tap to copy
            bot.edit_message_text(f"<pre>{safe}</pre>", cid, mid,
                                  disable_web_page_preview=True)
        except Exception:
            try:
                bot.send_message(cid, f"<pre>{safe}</pre>", disable_web_page_preview=True)
            except Exception as e:
                log.error("Failed to send cover letter: %s", e)

    threading.Thread(target=_worker, daemon=True).start()


def _cover_from_card(cid: int, card_mid: int, vacancy_url: str,
                     vacancy_id: str, card_html: str):
    """ИИ-отклик button → append the letter to the vacancy message in a code block."""
    # Spinner on the button
    try:
        spinner = InlineKeyboardMarkup()
        spinner.add(InlineKeyboardButton("⏳ Генерирую…", callback_data="noop"))
        bot.edit_message_reply_markup(cid, card_mid, reply_markup=spinner)
    except Exception:
        pass

    # Keep only the original card (strip any previously appended letter)
    base_html = re.split(r"\n\n<pre>", card_html)[0]

    def _worker():
        letter = _build_letter(vacancy_url)
        safe = html.escape(letter)
        new_html = f"{base_html}\n\n<pre>{safe}</pre>"
        try:
            bot.edit_message_text(new_html, cid, card_mid,
                                  disable_web_page_preview=True,
                                  reply_markup=kb_vacancy_actions(vacancy_id))
        except Exception as e:
            log.error("Failed to append cover letter to card: %s", e)
            try:
                bot.send_message(cid, f"<pre>{safe}</pre>",
                                 reply_to_message_id=card_mid,
                                 disable_web_page_preview=True)
                bot.edit_message_reply_markup(cid, card_mid,
                                              reply_markup=kb_vacancy_actions(vacancy_id))
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


# ── Планировщик ───────────────────────────────────────────────────────────────

def vacancy_checker():
    log.info("Vacancy checker started (interval=%ds)", CHECK_INTERVAL_SECONDS)
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        try:
            _run_check()
        except Exception as e:
            log.error("Checker error: %s", e)


def _is_recent(v: dict, max_days: int = 7) -> bool:
    """Return True if vacancy was published within last max_days days."""
    pub = v.get("published_at", "")
    if not pub:
        return True  # дата неизвестна — показываем
    try:
        d = datetime.datetime.strptime(pub, "%d.%m.%Y").date()
        return (datetime.date.today() - d).days <= max_days
    except Exception:
        return True


def _run_check():
    filters = db.get_active_filters()
    for f in filters:
        try:
            vacancies = hh_api.search_vacancies(f, per_page=100, periods=[1])
            new_ones  = db.filter_new_vacancies(f["id"], vacancies)
        except Exception as e:
            log.error("Error fetching filter %s: %s", f["id"], e)
            continue

        if not new_ones:
            continue

        # Старые вакансии (поднятые работодателем) — пометить как просмотренные,
        # но не показывать пользователю
        recent = [v for v in new_ones if _is_recent(v, max_days=7)]
        stale  = [v for v in new_ones if not _is_recent(v, max_days=7)]
        if stale:
            log.info("Filter '%s': silently marking %d stale vacancies as seen",
                     f["name"], len(stale))
            db.mark_seen(f["id"], [v["id"] for v in stale])

        if not recent:
            continue

        limited = recent[:MAX_NEW_VACANCIES]
        log.info("Filter '%s': %d new vacancies", f["name"], len(limited))
        send_vacancies(f["id"], limited)


# ── Запуск ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not OWNER_ID:
        raise SystemExit("OWNER_ID is not set — add it to .env / Railway variables.")

    db.init_db()
    log.info("DB ready.")

    t = threading.Thread(target=vacancy_checker, daemon=True)
    t.start()

    log.info("Bot polling started.")
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        allowed_updates=["message", "callback_query"],
    )
