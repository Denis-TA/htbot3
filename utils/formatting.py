_EXP = {
    "noExperience": "Без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6":    "Более 6 лет",
}
_EMP = {
    "full":      "Полная",
    "part":      "Частичная",
    "project":   "Проектная",
    "volunteer": "Волонтёрство",
    "probation": "Стажировка",
}
_SCH = {
    "fullDay":    "Полный день",
    "shift":      "Сменный",
    "flexible":   "Гибкий",
    "remote":     "Удалённо",
    "flyInFlyOut":"Вахта",
}


def format_vacancy(v: dict) -> str:
    title    = v.get("name", "Без названия")
    employer = (v.get("employer") or {}).get("name", "")
    area     = (v.get("area") or {}).get("name", "")
    url      = v.get("alternate_url", "")
    exp      = (v.get("experience") or {}).get("name", "")
    sch      = (v.get("schedule") or {}).get("name", "")
    emp_type = (v.get("employment") or {}).get("name", "")
    pub_date = v.get("published_at", "")

    salary = v.get("salary")
    if salary:
        parts = []
        if salary.get("from"): parts.append(f"от {salary['from']:,}".replace(",", " "))
        if salary.get("to"):   parts.append(f"до {salary['to']:,}".replace(",", " "))
        cur = salary.get("currency", "")
        cur_sym = {"RUR": "₽", "USD": "$", "EUR": "€"}.get(cur, cur)
        salary_str = " ".join(parts) + f" {cur_sym}"
    else:
        salary_str = "не указана"

    lines = [f"<b>{title}</b>"]
    if employer: lines.append(f"🏢 {employer}")
    if area:     lines.append(f"📍 {area}")
    lines.append(f"💰 {salary_str}")
    if exp:      lines.append(f"🎓 {exp}")
    if emp_type: lines.append(f"💼 {emp_type}")
    if sch:      lines.append(f"🕐 {sch}")
    if pub_date: lines.append(f"📅 {pub_date}")
    if url:      lines.append(f'\n<a href="{url}">Открыть вакансию ↗</a>')
    return "\n".join(lines)


def _fmt_multi(val, lookup: dict) -> str:
    if not val:
        return ""
    return ", ".join(lookup.get(v, v) for v in str(val).split(",") if v)


def format_filter_info(f: dict) -> str:
    lines = [f"<b>{f['name']}</b>"]
    if f.get("text"):      lines.append(f"🔍 {f['text']}")
    if f.get("area_name"): lines.append(f"📍 {f['area_name']}")

    sp = []
    if f.get("salary_from"): sp.append(f"от {f['salary_from']:,}".replace(",", " "))
    if f.get("salary_to"):   sp.append(f"до {f['salary_to']:,}".replace(",", " "))
    if sp: lines.append(f"💰 {' '.join(sp)} ₽")
    if f.get("only_with_salary"): lines.append("✓ Только с зарплатой")

    exp = _fmt_multi(f.get("experience"), _EXP)
    emp = _fmt_multi(f.get("employment"), _EMP)
    sch = _fmt_multi(f.get("schedule"),   _SCH)
    if exp: lines.append(f"🎓 {exp}")
    if emp: lines.append(f"💼 {emp}")
    if sch: lines.append(f"🕐 {sch}")

    lines.append("✅ Активен" if f.get("is_active") else "⏸ На паузе")
    return "\n".join(lines)
