"""
hh.ru parser via RSS feed — no OAuth, no extra dependencies.

Strategy: 5 requests with increasing periods (1/3/7/14/30 days).
Each period returns the freshest 20 vacancies within that window.
Deduplication by URL keeps only unique results across all periods.
"""
import logging
import re

import requests

from config import HH_USER_AGENT

log = logging.getLogger(__name__)

RSS_URL   = "https://hh.ru/search/vacancy/rss"
AREAS_URL = "https://api.hh.ru/suggests/areas"   # not behind DDoS-Guard
PERIODS   = [1, 3, 7, 14, 30]

_HEADERS = {
    "User-Agent": HH_USER_AGENT,
    "Accept":     "application/rss+xml, application/xml",
}


# ── Salary parser ─────────────────────────────────────────────────────────────

def _parse_salary(text: str) -> dict | None:
    """Parse 'от 100 000 до 200 000 ₽' → {"from": 100000, "to": 200000, "currency": "RUR"}"""
    if not text:
        return None
    text = text.replace("\xa0", " ").replace(" ", " ").strip()

    currency = "RUR"
    for sym, code in (("₽", "RUR"), ("$", "USD"), ("€", "EUR")):
        if sym in text:
            currency = code
            break

    sal_from = sal_to = None
    mf = re.search(r"от\s+([\d\s]+)", text)
    mt = re.search(r"до\s+([\d\s]+)", text)
    if mf:
        sal_from = int(re.sub(r"\s+", "", mf.group(1)))
    if mt:
        sal_to = int(re.sub(r"\s+", "", mt.group(1)))

    # Plain number without от/до
    if sal_from is None and sal_to is None:
        nums = re.findall(r"\d[\d\s]+\d", text)
        if nums:
            sal_from = int(re.sub(r"\s+", "", nums[0]))

    if sal_from is None and sal_to is None:
        return None
    return {"from": sal_from, "to": sal_to, "currency": currency}


# ── RSS item parser ───────────────────────────────────────────────────────────

def _parse_item(raw: str) -> dict | None:
    """Parse one raw <item>...</item> block into a vacancy dict."""
    try:
        # Link / ID
        link_m = re.search(r"<link>(.*?)</link>", raw)
        guid_m = re.search(r"<guid[^>]*>(.*?)</guid>", raw)
        link   = (link_m or guid_m)
        if not link:
            return None
        url = link.group(1).strip()

        vid_m = re.search(r"/vacancy/(\d+)", url)
        vid   = vid_m.group(1) if vid_m else url

        # Publication date  e.g. "2026-05-04T12:09:22.592+03:00" → "04.05.2026"
        pub_m   = re.search(r"<pubDate>(.*?)</pubDate>", raw)
        pub_raw = pub_m.group(1).strip() if pub_m else ""
        pub_date = ""
        if pub_raw:
            dm = re.match(r"(\d{4})-(\d{2})-(\d{2})", pub_raw)
            if dm:
                pub_date = f"{dm.group(3)}.{dm.group(2)}.{dm.group(1)}"

        # Title
        title_m = re.search(r"<title>(.*?)</title>", raw)
        title   = title_m.group(1).strip() if title_m else ""

        # Description CDATA
        desc_m = re.search(r"<!\[CDATA\[(.*?)\]\]>", raw, re.DOTALL)
        desc   = desc_m.group(1) if desc_m else ""

        employer_m = re.search(r"Вакансия компании:\s*([^<\n]+)", desc)
        employer   = employer_m.group(1).strip() if employer_m else ""

        area_m = re.search(r"Регион:\s*([^<\n]+)", desc)
        area   = area_m.group(1).strip() if area_m else ""

        sal_m    = re.search(r"уровень[^:]*:\s*([^<\n]+)", desc)
        sal_text = sal_m.group(1).strip() if sal_m else ""
        salary   = _parse_salary(sal_text)

        return {
            "id":            vid,
            "name":          title,
            "alternate_url": url,
            "employer":      {"name": employer},
            "area":          {"name": area},
            "salary":        salary,
            "published_at":  pub_date,
            "experience":    {},
            "schedule":      {},
        }
    except Exception as e:
        log.debug("Item parse error: %s", e)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def search_vacancies(f: dict, per_page: int = 100,
                     periods: list | None = None) -> list:
    """
    Search vacancies via RSS feed.

    Supports multiple keyword groups separated by commas in f["text"].
    Each group is searched independently; results are merged and deduplicated.

    periods — list of day-windows to query (default: all five PERIODS).
              Pass [1] for fast monitoring checks.
    Returns at most `per_page` unique vacancies, deduplicated by URL.
    """
    if periods is None:
        periods = PERIODS

    raw_text = f.get("text") or f.get("name") or ""

    # Split by comma → multiple keyword groups, each searched separately
    groups = [g.strip() for g in raw_text.split(",") if g.strip()]
    if not groups:
        groups = [""]   # empty query = no text filter

    # Base params without text
    base_params: dict = {
        "per_page":     20,         # RSS hard-cap is 20 per request
        "search_field": "name",     # search in vacancy TITLE only
    }
    def _multi(val):
        return [v for v in str(val).split(",") if v] if val else []

    if f.get("area_id"):          base_params["area"]             = f["area_id"]
    if f.get("salary_from"):      base_params["salary_from"]      = f["salary_from"]
    if f.get("only_with_salary"): base_params["only_with_salary"] = 1
    exp = _multi(f.get("experience"))
    emp = _multi(f.get("employment"))
    sch = _multi(f.get("schedule"))
    wf  = _multi(f.get("work_format"))
    if exp: base_params["experience"]  = exp
    if emp: base_params["employment"]  = emp
    if sch: base_params["schedule"]    = sch
    if wf:  base_params["work_format"] = wf

    seen:    set  = set()
    results: list = []

    for group_text in groups:
        if len(results) >= per_page:
            break

        params = dict(base_params)
        if group_text:
            params["text"] = group_text

        for period in periods:
            if len(results) >= per_page:
                break
            try:
                r = requests.get(
                    RSS_URL,
                    params={**params, "period": period},
                    headers=_HEADERS,
                    timeout=15,
                )
                if r.status_code != 200:
                    log.warning("RSS group=%r period=%d status=%d", group_text, period, r.status_code)
                    continue

                raw_items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
                new_count = 0
                for raw in raw_items:
                    v = _parse_item(raw)
                    if v is None:
                        continue
                    url = v["alternate_url"]
                    if url in seen:
                        continue
                    seen.add(url)
                    results.append(v)
                    new_count += 1

                log.info("RSS group=%r period=%dd: %d new (total: %d)",
                         group_text or "(any)", period, new_count, len(results))

            except Exception as e:
                log.error("RSS group=%r period=%d failed: %s", group_text, period, e)

    return results[:per_page]


def search_areas(query: str) -> list:
    """Area autocomplete — this endpoint is NOT behind DDoS-Guard."""
    try:
        r = requests.get(
            AREAS_URL,
            params={"text": query},
            headers={**_HEADERS, "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("items", [])[:10]
        log.warning("Areas status=%d", r.status_code)
    except Exception as e:
        log.error("Areas request failed: %s", e)
    return []
