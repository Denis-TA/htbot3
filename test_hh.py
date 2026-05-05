"""
Test hh.ru API - comparison of approaches.
Run via test_hh.bat or: python test_hh.py [search query]
"""
import os
import re
import sys
import time
import requests
from dotenv import load_dotenv

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

HH_CLIENT_ID     = os.getenv("HH_CLIENT_ID", "")
HH_CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET", "")
QUERY = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "python разработчик"
SEP   = "─" * 65

def ok(msg):   print(f"  [OK]   {msg}")
def fail(msg): print(f"  [FAIL] {msg}")
def warn(msg): print(f"  [WARN] {msg}")
def step(msg): print(f"\n{SEP}\n{msg}\n{SEP}")


# ─────────────────────────────────────────────────────────────────────────────
# ПОДХОД А: JSON API  (api.hh.ru/vacancies)
# ─────────────────────────────────────────────────────────────────────────────
step("ПОДХОД А: JSON API  api.hh.ru/vacancies")
print("  Официальный API. Требует OAuth-токен — иначе DDoS-Guard даёт 403.")

t0 = time.time()
try:
    headers_a = {"User-Agent": "hrbot3/1.0", "Accept": "application/json"}
    if HH_CLIENT_ID and HH_CLIENT_SECRET:
        try:
            rt = requests.post("https://hh.ru/oauth/token",
                data={"grant_type":"client_credentials","client_id":HH_CLIENT_ID,"client_secret":HH_CLIENT_SECRET},
                headers=headers_a, timeout=10)
            if rt.status_code == 200:
                token = rt.json()["access_token"]
                headers_a["Authorization"] = f"Bearer {token}"
                ok(f"OAuth токен получен: {token[:12]}...")
        except Exception as e:
            warn(f"OAuth failed: {e}")

    ra = requests.get("https://api.hh.ru/vacancies",
        params={"per_page": 5, "text": QUERY, "order_by": "publication_time"},
        headers=headers_a, timeout=15)
    elapsed_a = time.time() - t0
    print(f"  HTTP {ra.status_code}  ({elapsed_a:.1f}s)")
    if ra.status_code == 200:
        da = ra.json()
        items_a = da.get("items", [])
        ok(f"Найдено всего: {da.get('found')}  |  Получено: {len(items_a)}")
        for v in items_a[:2]:
            sal = v.get("salary")
            sal_str = "—"
            if sal:
                parts = []
                if sal.get("from"): parts.append(f"от {sal['from']:,}".replace(",", " "))
                if sal.get("to"):   parts.append(f"до {sal['to']:,}".replace(",", " "))
                sal_str = " ".join(parts) + f" {sal.get('currency','')}"
            print(f"       [{v.get('id')}] {v.get('name')}")
            print(f"        {(v.get('employer') or {}).get('name')}  |  {(v.get('area') or {}).get('name')}  |  {sal_str}")
    else:
        fail(f"HTTP {ra.status_code}: {ra.text[:150]}")
        if ra.status_code == 403 and not HH_CLIENT_ID:
            warn("Нет HH_CLIENT_ID в .env — токен не используется, DDoS-Guard блокирует")
except Exception as e:
    elapsed_a = time.time() - t0
    fail(f"Исключение: {e}")

print()
print("  Плюсы  А:")
print("   + Богатые данные: зарплата, опыт, занятость, график, ссылка на лого")
print("   + Нативная пагинация (per_page до 100, page=0..19 → 2000 вакансий)")
print("   + Все фильтры работают точно")
print("  Минусы А:")
print("   - Нужна регистрация приложения на dev.hh.ru")
print("   - Без токена — 403 (DDoS-Guard)")


# ─────────────────────────────────────────────────────────────────────────────
# ПОДХОД Б: HTML-скрапинг  hh.ru/search/vacancy
# ─────────────────────────────────────────────────────────────────────────────
step("ПОДХОД Б: HTML-скрапинг  hh.ru/search/vacancy")
print("  Парсим страницу сайта через curl_cffi (Chrome TLS fingerprint) + BeautifulSoup.")

try:
    from curl_cffi import requests as cf
    from bs4 import BeautifulSoup

    t0 = time.time()
    sess_b = cf.Session(impersonate="chrome120")
    sess_b.get("https://hh.ru/", timeout=10)
    rb = sess_b.get("https://hh.ru/search/vacancy",
        params={"text": QUERY, "per_page": 5}, timeout=15)
    elapsed_b = time.time() - t0
    print(f"  HTTP {rb.status_code}  ({elapsed_b:.1f}s)")

    if rb.status_code == 200:
        soup  = BeautifulSoup(rb.text, "html.parser")
        cards = soup.find_all(attrs={"data-qa": re.compile(r"vacancy-serp__vacancy")})
        ok(f"Карточек в HTML: {len(cards)}")
        count = 0
        for card in cards[:10]:
            t_tag = card.find(attrs={"data-qa": "serp-item__title"})
            if not t_tag: continue
            title = t_tag.get_text(strip=True)
            href  = t_tag.get("href","")
            emp   = (card.find(attrs={"data-qa": "vacancy-serp__vacancy-employer-text"}) or "")
            emp   = emp.get_text(strip=True) if emp else "—"
            area  = (card.find(attrs={"data-qa": "vacancy-serp__vacancy-address"}) or "")
            area  = area.get_text(strip=True) if area else "—"
            real  = re.search(r'(https://[^/]*hh\.ru/vacancy/\d+)', href)
            url   = real.group(1) if real else href[:60]
            vid   = re.search(r'/vacancy/(\d+)', url)
            vid   = vid.group(1) if vid else "advert"
            if vid == "advert": continue
            print(f"       [{vid}] {title}")
            print(f"        {emp}  |  {area}")
            count += 1
            if count >= 2: break
    else:
        fail(f"HTTP {rb.status_code}")
except ImportError as e:
    fail(f"Модуль не установлен: {e}")
except Exception as e:
    fail(f"Исключение: {e}")

print()
print("  Плюсы  Б:")
print("   + Не нужен OAuth")
print("   + Зарплата, работодатель, регион доступны")
print("  Минусы Б:")
print("   - Нужен curl_cffi (увесистая C-зависимость)")
print("   - CSS-классы могут измениться без предупреждения")
print("   - Рекламные карточки мешаются, нет нормального ID")
print("   - Максимум 20 за запрос, нет нативной пагинации через params")


# ─────────────────────────────────────────────────────────────────────────────
# ПОДХОД В: RSS  hh.ru/search/vacancy/rss  ← ВЫБРАННЫЙ
# ─────────────────────────────────────────────────────────────────────────────
step("ПОДХОД В: RSS  hh.ru/search/vacancy/rss  [ВЫБРАННЫЙ]")
print("  Официальная RSS-лента. Обычный requests, XML, без авторизации.")
print(f"  Делаем 5 запросов (периоды 1/3/7/14/30 дней), дедупликация по URL.\n")

def parse_salary(text):
    if not text: return None
    text = text.replace("\xa0", " ").replace(" ", " ")
    cur  = "RUR"
    for sym, code in (("₽","RUR"),("$","USD"),("€","EUR")):
        if sym in text: cur = code; break
    sal_from = sal_to = None
    mf = re.search(r"от\s+([\d\s]+)", text)
    mt = re.search(r"до\s+([\d\s]+)", text)
    if mf: sal_from = int(re.sub(r"\s","",mf.group(1)))
    if mt: sal_to   = int(re.sub(r"\s","",mt.group(1)))
    if sal_from is None and sal_to is None:
        nums = re.findall(r"\d[\d\s]+\d", text)
        if nums: sal_from = int(re.sub(r"\s","",nums[0]))
    if sal_from is None and sal_to is None: return None
    return {"from": sal_from, "to": sal_to, "currency": cur}

PERIODS = [1, 3, 7, 14, 30]
seen_urls: set = set()
all_items: list = []
params_base = {"text": QUERY, "per_page": 20}

t0 = time.time()
for period in PERIODS:
    params = {**params_base, "period": period}
    try:
        rc = requests.get("https://hh.ru/search/vacancy/rss",
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml,application/xml"},
            timeout=15)
        if rc.status_code != 200:
            warn(f"  period={period}: HTTP {rc.status_code}")
            continue

        raw_items = re.findall(r"<item>(.*?)</item>", rc.text, re.DOTALL)
        new_count  = 0
        for raw in raw_items:
            link = (re.search(r"<link>(.*?)</link>", raw) or re.search(r"<guid[^>]*>(.*?)</guid>", raw))
            link = link.group(1).strip() if link else ""
            if not link or link in seen_urls: continue
            seen_urls.add(link)

            title = re.search(r"<title>(.*?)</title>", raw)
            title = title.group(1).strip() if title else ""

            desc = re.search(r"<!\[CDATA\[(.*?)\]\]>", raw, re.DOTALL)
            desc = desc.group(1) if desc else ""

            employer = re.search(r"Вакансия компании:\s*([^<]+)", desc)
            employer = employer.group(1).strip() if employer else ""

            area = re.search(r"Регион:\s*([^<]+)", desc)
            area = area.group(1).strip() if area else ""

            sal_text = re.search(r"уровень[^:]*:\s*([^<]+)", desc)
            sal_text = sal_text.group(1).strip() if sal_text else ""
            salary   = parse_salary(sal_text)

            vid = re.search(r"/vacancy/(\d+)", link)
            vid = vid.group(1) if vid else link

            all_items.append({
                "id": vid, "name": title, "alternate_url": link,
                "employer": {"name": employer}, "area": {"name": area},
                "salary": salary,
            })
            new_count += 1

        ok(f"period={period:2d}д → {new_count:2d} новых  (всего уник: {len(all_items)})")
    except Exception as e:
        fail(f"period={period}: {e}")

elapsed_c = time.time() - t0
print(f"\n  Итого: {len(all_items)} уникальных вакансий за {elapsed_c:.1f}с")
print()
for v in all_items[:3]:
    sal = v["salary"]
    sal_str = "—"
    if sal:
        parts = []
        if sal.get("from"): parts.append(f"от {sal['from']:,}".replace(",", " "))
        if sal.get("to"):   parts.append(f"до {sal['to']:,}".replace(",", " "))
        sal_str = " ".join(parts) + f" {sal['currency']}"
    print(f"  [{v['id']}] {v['name']}")
    print(f"   {v['employer']['name']}  |  {v['area']['name']}  |  {sal_str}")
    print(f"   {v['alternate_url']}")
    print()

print("  Плюсы  В:")
print("   + Не нужен OAuth и никакие спец-зависимости")
print("   + Официальная лента — стабильный XML-формат")
print("   + 5 периодов × 20 = до 100 уникальных вакансий")
print("   + Все нужные фильтры поддерживаются (area, experience, employment, schedule)")
print("   + Зарплата, работодатель, регион, ссылка — всё есть")
print("  Минусы В:")
print("   - Нет ID вакансии отдельным полем (берём из URL)")
print("   - Нет фото работодателя / логотипа")
print("   - Немного медленнее (5 запросов)")


# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("ИТОГ: используем ПОДХОД В (RSS)")
print(SEP)
