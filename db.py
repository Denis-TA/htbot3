import os
import sqlite3

from config import DB_PATH

# If DATABASE_URL is set (Scalingo/Heroku PostgreSQL addon), use PostgreSQL.
# Otherwise, fall back to local SQLite.
DATABASE_URL = os.getenv("DATABASE_URL", "")
_PG = bool(DATABASE_URL)

if _PG:
    import psycopg2
    import psycopg2.extras


# ── Connection wrapper ────────────────────────────────────────────────────────

class _Conn:
    """Uniform execute interface for both SQLite and PostgreSQL."""

    def __init__(self):
        if _PG:
            self._c = psycopg2.connect(DATABASE_URL)
        else:
            self._c = sqlite3.connect(DB_PATH)
            self._c.row_factory = sqlite3.Row

    def execute(self, sql: str, params=()):
        if _PG:
            cur = self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), list(params) if params else None)
            return cur
        return self._c.execute(sql, params)

    def executemany(self, sql: str, rows):
        if _PG:
            cur = self._c.cursor()
            psycopg2.extras.execute_batch(cur, sql.replace("?", "%s"), rows)
            return cur
        return self._c.executemany(sql, rows)

    def _insert_id(self, sql: str, params) -> int:
        """INSERT and return generated id. Uses RETURNING id for PostgreSQL."""
        if _PG:
            cur = self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s") + " RETURNING id", list(params))
            return cur.fetchone()["id"]
        cur = self._c.execute(sql, params)
        return cur.lastrowid

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._c.rollback()
        else:
            self._c.commit()
        self._c.close()


def get_conn() -> _Conn:
    return _Conn()


# ── Schema ────────────────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS filters (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT    NOT NULL,
        text             TEXT,
        area_id          TEXT,
        area_name        TEXT,
        salary_from      INTEGER,
        salary_to        INTEGER,
        only_with_salary INTEGER DEFAULT 0,
        experience       TEXT,
        employment       TEXT,
        schedule         TEXT,
        work_format      TEXT,
        is_active        INTEGER DEFAULT 1,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS seen_vacancies (
        vacancy_id TEXT    NOT NULL,
        filter_id  INTEGER NOT NULL,
        PRIMARY KEY (vacancy_id, filter_id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS resumes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL,
        text       TEXT    NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
"""

_PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS filters (
        id               SERIAL PRIMARY KEY,
        name             TEXT    NOT NULL,
        text             TEXT,
        area_id          TEXT,
        area_name        TEXT,
        salary_from      INTEGER,
        salary_to        INTEGER,
        only_with_salary INTEGER DEFAULT 0,
        experience       TEXT,
        employment       TEXT,
        schedule         TEXT,
        work_format      TEXT,
        is_active        INTEGER DEFAULT 1,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS seen_vacancies (
        vacancy_id TEXT    NOT NULL,
        filter_id  INTEGER NOT NULL,
        PRIMARY KEY (vacancy_id, filter_id)
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS resumes (
        id         SERIAL PRIMARY KEY,
        name       TEXT    NOT NULL,
        text       TEXT    NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]


def init_db():
    with get_conn() as conn:
        if _PG:
            cur = conn._c.cursor()
            for stmt in _PG_SCHEMA:
                cur.execute(stmt)
        else:
            conn._c.executescript(_SQLITE_SCHEMA)
    _migrate()


def _migrate():
    """Idempotent, non-destructive migrations for existing databases.

    Only ADDs columns — never drops or recreates — so existing resumes,
    filters, progress, seen vacancies and settings are preserved.
    """
    _safe_add_column("filters", "work_format", "TEXT")


def _safe_add_column(table: str, column: str, decl: str):
    with get_conn() as conn:
        if _PG:
            # PostgreSQL supports IF NOT EXISTS natively
            conn._c.cursor().execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {decl}"
            )
        else:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ── Filters ───────────────────────────────────────────────────────────────────

def create_filter(name, text=None, area_id=None, area_name=None,
                  salary_from=None, salary_to=None, only_with_salary=False,
                  experience=None, employment=None, schedule=None,
                  work_format=None):
    sql = """INSERT INTO filters
               (name,text,area_id,area_name,salary_from,salary_to,
                only_with_salary,experience,employment,schedule,work_format)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
    params = (name, text, area_id, area_name, salary_from, salary_to,
              1 if only_with_salary else 0, experience, employment, schedule,
              work_format)
    with get_conn() as conn:
        return conn._insert_id(sql, params)


def get_all_filters():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM filters ORDER BY created_at DESC"
        ).fetchall()]


def get_active_filters():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM filters WHERE is_active=1"
        ).fetchall()]


def get_filter(filter_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM filters WHERE id=?", (filter_id,)
        ).fetchone()
        return dict(row) if row else None


def update_filter(filter_id, **kwargs):
    if not kwargs:
        return
    set_clause = ", ".join(f"{k}=?" for k in kwargs)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE filters SET {set_clause} WHERE id=?",
            list(kwargs.values()) + [filter_id]
        )


def delete_filter(filter_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM seen_vacancies WHERE filter_id=?", (filter_id,))
        conn.execute("DELETE FROM filters WHERE id=?", (filter_id,))


def toggle_filter(filter_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE filters SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
            (filter_id,)
        )
        row = conn.execute("SELECT is_active FROM filters WHERE id=?", (filter_id,)).fetchone()
        return bool(row[0]) if row else False


def filter_new_vacancies(filter_id, vacancies):
    if not vacancies:
        return []
    ids = [v["id"] for v in vacancies]
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        seen = {r[0] if not _PG else r["vacancy_id"] for r in conn.execute(
            f"SELECT vacancy_id FROM seen_vacancies WHERE filter_id=? AND vacancy_id IN ({placeholders})",
            [filter_id] + ids
        ).fetchall()}
    return [v for v in vacancies if v["id"] not in seen]


def mark_seen(filter_id, vacancy_ids):
    if not vacancy_ids:
        return
    if _PG:
        sql = "INSERT INTO seen_vacancies (vacancy_id,filter_id) VALUES (?,?) ON CONFLICT DO NOTHING"
    else:
        sql = "INSERT OR IGNORE INTO seen_vacancies (vacancy_id,filter_id) VALUES (?,?)"
    with get_conn() as conn:
        conn.executemany(sql, [(vid, filter_id) for vid in vacancy_ids])


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return (row["value"] if _PG else row[0]) if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )


def del_setting(key: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


# ── Progress ──────────────────────────────────────────────────────────────────

def get_progress() -> int:
    return int(get_setting("applied_count", "0"))


def set_progress(value: int):
    set_setting("applied_count", str(max(0, value)))


def inc_progress() -> int:
    current = get_progress()
    new_val = current + 1
    set_progress(new_val)
    return new_val


# ── Resumes ───────────────────────────────────────────────────────────────────

def add_resume(name: str, text: str) -> int:
    sql = "INSERT INTO resumes (name, text) VALUES (?, ?)"
    with get_conn() as conn:
        return conn._insert_id(sql, (name, text))


def get_resumes() -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM resumes ORDER BY created_at DESC"
        ).fetchall()]


def get_resume(resume_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
        return dict(row) if row else None


def update_resume(resume_id: int, **kwargs):
    if not kwargs:
        return
    set_clause = ", ".join(f"{k}=?" for k in kwargs)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE resumes SET {set_clause} WHERE id=?",
            list(kwargs.values()) + [resume_id]
        )


def delete_resume(resume_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM resumes WHERE id=?", (resume_id,))
