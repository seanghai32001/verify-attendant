"""
database.py
-----------
ស្រទាប់រក្សាទុកទិន្នន័យ (SQLite) ជំនួសឯកសារ JSON ទាំងអស់។

- ប្រើ sqlite3 ក្នុង Python (មិនត្រូវការ install អ្វីបន្ថែម និងមិនត្រូវការ Database Server)
- ឯកសារ Database: $DATABASE_PATH ឬ $DATA_DIR/app.db
- ពេលចាប់ផ្តើមលើកដំបូង ប្រសិនបើមាន JSON ចាស់ (institutions.json និង data/<username>/*.json)
  វានឹងនាំចូលទៅ Database ដោយស្វ័យប្រវត្តិ ម្តងគត់ (JSON ដើមមិនត្រូវបានលុប ឬកែប្រែទេ)
- អាចរត់ដោយដៃបានដែរ: python database.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(DATA_DIR / "app.db"))).resolve()

MIGRATION_KEY = "json_import_v1"

# ============================================================
# Schema
# ============================================================
# ទិន្នន័យទាំងអស់ភ្ជាប់ទៅ inst_username (ON UPDATE CASCADE: ប្តូរ username -> ទិន្នន័យតាមទៅជាមួយ,
# ON DELETE CASCADE: លុបស្ថាប័ន -> ទិន្នន័យទាំងអស់របស់ស្ថាប័នត្រូវលុបតាម)
# id នៃ subject/class/student/template រក្សាលេខដដែលដូច JSON ចាស់ (រាប់ចាប់ពី 1 ក្នុងមួយស្ថាប័ន)
# ដើម្បីកុំឱ្យ link សាធារណៈ និង Frontend ដែលមានស្រាប់ខូច។
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS institutions (
    id                      INTEGER PRIMARY KEY,
    institution_name        TEXT NOT NULL DEFAULT '',
    teacher_name            TEXT NOT NULL DEFAULT '',
    inst_username           TEXT NOT NULL UNIQUE,
    inst_password           TEXT NOT NULL DEFAULT '',
    inst_password_encrypted TEXT,
    profile_image_url       TEXT,
    created_at              TEXT
);

CREATE TABLE IF NOT EXISTS subjects (
    inst_username TEXT NOT NULL
        REFERENCES institutions(inst_username) ON UPDATE CASCADE ON DELETE CASCADE,
    id            INTEGER NOT NULL,
    name          TEXT NOT NULL,
    PRIMARY KEY (inst_username, id)
);

CREATE TABLE IF NOT EXISTS classes (
    inst_username TEXT NOT NULL
        REFERENCES institutions(inst_username) ON UPDATE CASCADE ON DELETE CASCADE,
    id            INTEGER NOT NULL,
    class_name    TEXT NOT NULL,
    subject_id    INTEGER,
    created_at    TEXT,
    PRIMARY KEY (inst_username, id)
);

CREATE TABLE IF NOT EXISTS time_slots (
    inst_username TEXT NOT NULL,
    class_id      INTEGER NOT NULL,
    id            INTEGER NOT NULL,
    start_time    TEXT NOT NULL DEFAULT '',
    end_time      TEXT NOT NULL DEFAULT '',
    subject_id    INTEGER,
    PRIMARY KEY (inst_username, class_id, id),
    FOREIGN KEY (inst_username, class_id)
        REFERENCES classes(inst_username, id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS students (
    inst_username TEXT NOT NULL
        REFERENCES institutions(inst_username) ON UPDATE CASCADE ON DELETE CASCADE,
    id            INTEGER NOT NULL,
    student_name  TEXT NOT NULL,
    phone         TEXT NOT NULL DEFAULT '',
    class_id      INTEGER,
    note          TEXT NOT NULL DEFAULT '',
    slot_ids      TEXT,
    created_at    TEXT,
    PRIMARY KEY (inst_username, id),
    FOREIGN KEY (inst_username, class_id)
        REFERENCES classes(inst_username, id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_students_class ON students(inst_username, class_id);

CREATE TABLE IF NOT EXISTS templates (
    inst_username TEXT NOT NULL
        REFERENCES institutions(inst_username) ON UPDATE CASCADE ON DELETE CASCADE,
    id            INTEGER NOT NULL,
    name          TEXT NOT NULL,
    content       TEXT NOT NULL,
    is_default    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (inst_username, id)
);

CREATE TABLE IF NOT EXISTS telegram_config (
    inst_username   TEXT PRIMARY KEY
        REFERENCES institutions(inst_username) ON UPDATE CASCADE ON DELETE CASCADE,
    api_id          TEXT NOT NULL DEFAULT '',
    api_hash        TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    session_string  TEXT NOT NULL DEFAULT '',
    connected       INTEGER NOT NULL DEFAULT 0,
    account_name    TEXT NOT NULL DEFAULT '',
    phone_code_hash TEXT
);
"""


# ============================================================
# Connections
# ============================================================
def _connect() -> sqlite3.Connection:
    # isolation_level=None -> យើងគ្រប់គ្រង BEGIN/COMMIT ដោយខ្លួនឯង
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """Connection សម្រាប់អានតែប៉ុណ្ណោះ។"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """
    Transaction សម្រាប់សរសេរ: ជោគជ័យទាំងអស់ ឬ rollback ទាំងអស់។
    (HTTPException ឬ error ណាមួយខាងក្នុង -> rollback ស្វ័យប្រវត្តិ)
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def next_id(conn: sqlite3.Connection, table: str, username: str) -> int:
    """id បន្ទាប់ក្នុងមួយស្ថាប័ន (max+1) ដូចការរាប់ចាស់។ table ជា constant ក្នុងកូដ មិនមកពី user។"""
    row = conn.execute(
        f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table} WHERE inst_username = ?",
        (username,),
    ).fetchone()
    return int(row["n"])


def init_db() -> Optional[dict]:
    """បង្កើត table (បើមិនទាន់មាន) ហើយនាំចូល JSON ចាស់ម្តងគត់។"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass  # file system ខ្លះមិនគាំទ្រ WAL -> ប្រើ mode ធម្មតា
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    summary = migrate_json_if_needed()
    if summary:
        print(f"[database] នាំចូល JSON ចាស់ទៅ {DB_PATH.name} រួចរាល់: {summary}")
    return summary


# ============================================================
# Institutions (ប្រើដោយ main.py)
# ============================================================
_UPDATABLE = {
    "institution_name",
    "teacher_name",
    "inst_username",
    "inst_password",
    "inst_password_encrypted",
    "profile_image_url",
}


def list_institutions() -> list[dict]:
    with read() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM institutions ORDER BY id")]


def get_institution_by_username(username: str) -> Optional[dict]:
    with read() as conn:
        row = conn.execute(
            "SELECT * FROM institutions WHERE inst_username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_institution_by_id(inst_id: int) -> Optional[dict]:
    with read() as conn:
        row = conn.execute("SELECT * FROM institutions WHERE id = ?", (inst_id,)).fetchone()
    return dict(row) if row else None


def get_next_institution_id() -> int:
    with read() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM institutions").fetchone()
    return int(row["n"])


def insert_institution(inst: dict) -> None:
    """បង្កើតស្ថាប័នថ្មី។ raise sqlite3.IntegrityError ប្រសិនបើ id ឬ username ជាន់គ្នា។"""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO institutions
               (id, institution_name, teacher_name, inst_username, inst_password,
                inst_password_encrypted, profile_image_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                inst["id"],
                inst.get("institution_name", ""),
                inst.get("teacher_name", ""),
                inst["inst_username"],
                inst.get("inst_password", ""),
                inst.get("inst_password_encrypted"),
                inst.get("profile_image_url"),
                inst.get("created_at"),
            ),
        )


def update_institution(inst_id: int, fields: dict) -> Optional[dict]:
    """កែស្ថាប័ន (ទិន្នន័យថ្នាក់/សិស្សតាមទៅជាមួយ ពេលប្តូរ username)។"""
    cols = [k for k in fields if k in _UPDATABLE]
    with transaction() as conn:
        if cols:
            conn.execute(
                f"UPDATE institutions SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                [fields[c] for c in cols] + [inst_id],
            )
        row = conn.execute("SELECT * FROM institutions WHERE id = ?", (inst_id,)).fetchone()
    return dict(row) if row else None


def update_institution_credentials(inst_id: int, password_hash: str, encrypted: str) -> None:
    update_institution(
        inst_id, {"inst_password": password_hash, "inst_password_encrypted": encrypted}
    )


def delete_institution(inst_id: int) -> bool:
    """លុបស្ថាប័ន + ទិន្នន័យទាំងអស់ (subjects, classes, students, templates, telegram) តាម CASCADE។"""
    with transaction() as conn:
        cur = conn.execute("DELETE FROM institutions WHERE id = ?", (inst_id,))
        return cur.rowcount > 0


# ============================================================
# One-time import from the old JSON files
# ============================================================
def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"[database] ⚠ អានមិនបាន (រំលង): {path}")
        return default


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _unique_ids(items: list[dict]) -> None:
    """ធានាថា item ទាំងអស់មាន id ជាចំនួនគត់ មិនជាន់គ្នា (រក្សា id ដើមបើត្រឹមត្រូវ)។"""
    seen: set[int] = set()
    for item in items:
        i = _as_int(item.get("id"))
        if i is not None and i not in seen:
            seen.add(i)
            item["id"] = i
        else:
            item["id"] = None
    top = max(seen, default=0)
    for item in items:
        if item["id"] is None:
            top += 1
            item["id"] = top


def _repair_subjects(subjects: list[dict], classes: list[dict]) -> None:
    """
    ដូចអ្វីដែល _ensure_subjects ចាស់ធ្លាប់ធ្វើគ្រប់ពេល GET: ធានាថាគ្រប់ថ្នាក់/ម៉ោងមាន subject_id ត្រឹមត្រូវ។
    ឥឡូវធ្វើម្តងគត់ពេលនាំចូល។
    """
    by_id = {s["id"]: s for s in subjects}
    by_name = {str(s.get("name", "")).strip().casefold(): s for s in subjects}

    def get_or_create(name: str) -> dict:
        clean = (name or "").strip() or "មុខវិជ្ជាទូទៅ"
        subj = by_name.get(clean.casefold())
        if subj is None:
            subj = {"id": max(by_id, default=0) + 1, "name": clean}
            subjects.append(subj)
            by_id[subj["id"]] = subj
            by_name[clean.casefold()] = subj
        return subj

    for c in classes:
        class_subject = by_id.get(c.get("subject_id"))
        slot_subject_ids: set[int] = set()
        for slot in c.get("time_slots", []):
            subj = by_id.get(slot.get("subject_id")) or class_subject
            if subj is None:
                subj = get_or_create(slot.get("subject") or c.get("class_name", ""))
            slot_subject_ids.add(subj["id"])
            slot["subject_id"] = subj["id"]
        if class_subject is None:
            if len(slot_subject_ids) == 1:
                c["subject_id"] = next(iter(slot_subject_ids))
            elif not slot_subject_ids:
                c["subject_id"] = get_or_create(c.get("class_name", ""))["id"]
            else:
                c["subject_id"] = None


def _import_institution_data(conn: sqlite3.Connection, username: str) -> dict:
    folder = DATA_DIR / username
    counts = {"subjects": 0, "classes": 0, "time_slots": 0, "students": 0, "templates": 0}

    subjects = [s for s in _read_json(folder / "subjects.json", []) if isinstance(s, dict)]
    classes = [c for c in _read_json(folder / "classes.json", []) if isinstance(c, dict)]
    students = [s for s in _read_json(folder / "students.json", []) if isinstance(s, dict)]
    templates = [t for t in _read_json(folder / "templates.json", []) if isinstance(t, dict)]
    for c in classes:
        if not isinstance(c.get("time_slots"), list):
            c["time_slots"] = []
        c["time_slots"] = [s for s in c["time_slots"] if isinstance(s, dict)]

    _unique_ids(subjects)
    _unique_ids(classes)
    _unique_ids(students)
    _unique_ids(templates)
    for c in classes:
        _unique_ids(c["time_slots"])
    _repair_subjects(subjects, classes)

    for s in subjects:
        conn.execute(
            "INSERT OR IGNORE INTO subjects (inst_username, id, name) VALUES (?, ?, ?)",
            (username, s["id"], str(s.get("name", "")).strip()),
        )
        counts["subjects"] += 1

    class_ids = set()
    for c in classes:
        class_ids.add(c["id"])
        conn.execute(
            """INSERT OR IGNORE INTO classes (inst_username, id, class_name, subject_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, c["id"], str(c.get("class_name", "")), c.get("subject_id"), c.get("created_at")),
        )
        counts["classes"] += 1
        for slot in c["time_slots"]:
            conn.execute(
                """INSERT OR IGNORE INTO time_slots
                   (inst_username, class_id, id, start_time, end_time, subject_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    username, c["id"], slot["id"],
                    str(slot.get("start_time", "")), str(slot.get("end_time", "")),
                    slot.get("subject_id"),
                ),
            )
            counts["time_slots"] += 1

    for s in students:
        class_id = s.get("class_id")
        if class_id not in class_ids:
            class_id = None  # រក្សាសិស្សទុក ទោះថ្នាក់បាត់ (កុំឱ្យបាត់ទិន្នន័យ)
        slot_ids = s.get("slot_ids")
        conn.execute(
            """INSERT OR IGNORE INTO students
               (inst_username, id, student_name, phone, class_id, note, slot_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                username, s["id"],
                str(s.get("student_name", "")), str(s.get("phone", "") or ""),
                class_id, str(s.get("note", "") or ""),
                json.dumps(slot_ids) if isinstance(slot_ids, list) else None,
                s.get("created_at"),
            ),
        )
        counts["students"] += 1

    for t in templates:
        conn.execute(
            """INSERT OR IGNORE INTO templates (inst_username, id, name, content, is_default)
               VALUES (?, ?, ?, ?, ?)""",
            (username, t["id"], str(t.get("name", "")), str(t.get("content", "")),
             1 if t.get("is_default") else 0),
        )
        counts["templates"] += 1

    tg = _read_json(folder / "telegram.json", None)
    if isinstance(tg, dict):
        conn.execute(
            """INSERT OR IGNORE INTO telegram_config
               (inst_username, api_id, api_hash, phone, session_string, connected,
                account_name, phone_code_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                username,
                str(tg.get("api_id", "")), str(tg.get("api_hash", "")), str(tg.get("phone", "")),
                str(tg.get("session_string", "")), 1 if tg.get("connected") else 0,
                str(tg.get("account_name", "")), tg.get("_phone_code_hash"),
            ),
        )
    return counts


def migrate_json_if_needed() -> Optional[dict]:
    """
    នាំចូល institutions.json + data/<username>/*.json ទៅ Database ម្តងគត់។
    JSON ដើមនៅដដែល (ជា backup) តែលែងត្រូវបានអានទៀតហើយ។
    """
    inst_file = DATA_DIR / "institutions.json"
    if not inst_file.is_file():
        return None

    with transaction() as conn:
        done = conn.execute("SELECT 1 FROM meta WHERE key = ?", (MIGRATION_KEY,)).fetchone()
        if done:
            return None

        raw = _read_json(inst_file, [])
        institutions = [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []
        _unique_ids(institutions)

        total = {"institutions": 0, "subjects": 0, "classes": 0, "time_slots": 0,
                 "students": 0, "templates": 0}
        usernames: set[str] = set()
        for inst in institutions:
            username = str(inst.get("inst_username", "")).strip()
            if not username or username in usernames:
                continue
            usernames.add(username)
            conn.execute(
                """INSERT OR IGNORE INTO institutions
                   (id, institution_name, teacher_name, inst_username, inst_password,
                    inst_password_encrypted, profile_image_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    inst["id"],
                    str(inst.get("institution_name", "")), str(inst.get("teacher_name", "")),
                    username, str(inst.get("inst_password", "")),
                    inst.get("inst_password_encrypted"), inst.get("profile_image_url"),
                    inst.get("created_at"),
                ),
            )
            total["institutions"] += 1
            if Path(username).name != username:  # ការពារ path traversal ពេលអាន folder
                print(f"[database] ⚠ username មិនសុវត្ថិភាព រំលងទិន្នន័យថ្នាក់/សិស្ស: {username!r}")
                continue
            for key, n in _import_institution_data(conn, username).items():
                total[key] += n

        orphans = sorted(
            p.name for p in DATA_DIR.iterdir()
            if p.is_dir() and p.name not in usernames
            and any(p.glob("*.json"))
        )
        if orphans:
            total["folders_not_imported"] = orphans  # folder ដែលគ្មានស្ថាប័នត្រូវគ្នា

        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (MIGRATION_KEY, datetime.now().isoformat()),
        )
    return total


if __name__ == "__main__":
    result = init_db()
    print(f"Database: {DB_PATH}")
    print("សង្ខេបការនាំចូល:" if result else "គ្មានអ្វីត្រូវនាំចូល (បានធ្វើរួចហើយ ឬគ្មាន JSON ចាស់)។", result or "")
