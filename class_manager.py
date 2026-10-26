"""
class_manager.py
----------------
គ្រប់គ្រង Class, TimeSlot, Student, Message Template, Telegram Session
សម្រាប់ប្រព័ន្ធគ្រប់គ្រងស្ថាប័ន + ផ្ញើសារ Telegram (Telethon)
គាំទ្រ Batch Import សិស្ស និងទទួលគ្រប់ទម្រង់ Telegram (Phone, Link, Username, ID)

ទិន្នន័យទាំងអស់ត្រូវបានរក្សាទុកក្នុង SQLite (មើល database.py) ជំនួសឯកសារ JSON។
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import database as db

# ============================================================
# Paths (Telethon session files នៅតែជាឯកសារ .session របស់ Telethon)
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", str(BASE_DIR / "sessions"))).resolve()
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# Auth helper (injected from main.py)
# ============================================================
_get_current_inst_username: Optional[Callable[[Request], Optional[str]]] = None
_verify_inst_password: Optional[Callable[[str, str], bool]] = None


def configure(
    get_current_inst_username: Callable[[Request], Optional[str]],
    verify_inst_password: Callable[[str, str], bool],
) -> None:
    global _get_current_inst_username, _verify_inst_password
    _get_current_inst_username = get_current_inst_username
    _verify_inst_password = verify_inst_password


def require_inst(request: Request) -> str:
    if _get_current_inst_username is None:
        raise HTTPException(500, "Class manager not configured")
    username = _get_current_inst_username(request)
    if not username:
        raise HTTPException(401, "Unauthorized")
    return username


def require_inst_password(username: str, password: str) -> None:
    if _verify_inst_password is None:
        raise HTTPException(500, "Class manager password verification not configured")
    if not _verify_inst_password(username, password):
        raise HTTPException(401, "ពាក្យសម្ងាត់មិនត្រឹមត្រូវ")


def _require_owner(request: Request, username: str) -> None:
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")


# ============================================================
# Helpers
# ============================================================
KHMER_DIGITS_MAP = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")


def clean_phone_number(phone: str) -> str:
    if not phone:
        return ""
    p = str(phone).translate(KHMER_DIGITS_MAP)
    digits = re.sub(r"\D", "", p)
    if not digits:
        return ""
    if digits.startswith("855"):
        return digits
    if digits.startswith("0"):
        return "855" + digits[1:]
    return "855" + digits


def clean_guardian_contact(contact: str) -> str:
    if not contact:
        return ""
    value = str(contact).translate(KHMER_DIGITS_MAP).strip()
    return re.sub(r"\s+", "", value)


def _telegram_contact_candidates(contact: str) -> list[dict]:
    value = clean_guardian_contact(contact)
    if not value:
        return []

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, val: str) -> None:
        val = str(val or "").strip()
        key = (kind, val.casefold())
        if val and key not in seen:
            seen.add(key)
            candidates.append({"kind": kind, "value": val})

    lowered = value.lower()
    username = ""
    tg_id = ""

    link_match = re.search(
        r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^/?#]+)",
        value,
        re.IGNORECASE,
    )
    tg_user_match = re.search(r"tg://user\?id=(\d+)", value, re.IGNORECASE)

    if tg_user_match:
        tg_id = tg_user_match.group(1)
    elif link_match:
        path = link_match.group(1).strip()
        if path.lower() not in {"joinchat", "addstickers", "proxy"} and not path.startswith("+"):
            username = path.lstrip("@")
    elif value.startswith("@"):
        username = value[1:]
    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,32}", value):
        username = value

    if username:
        add("username", username)
        add("username", f"@{username}")

    if tg_id:
        add("id", tg_id)

    digits = re.sub(r"\D", "", value)
    starts_like_phone = value.startswith("+") or digits.startswith("0") or digits.startswith("855")

    if digits:
        if starts_like_phone or len(digits) >= 8:
            clean_phone = clean_phone_number(value)
            if clean_phone and len(clean_phone) >= 8:
                add("phone", clean_phone)
        if not starts_like_phone and len(digits) >= 5:
            add("id", digits)

    if lowered.startswith(("http://", "https://", "tg://")) and not username and not tg_id:
        add("entity", value)

    return candidates


# ============================================================
# Database row -> dict (រក្សារូបរាង JSON ដូចដើម ដើម្បីកុំឱ្យ Frontend ប្រែប្រួល)
# ============================================================
def _subject_list(conn: sqlite3.Connection, username: str) -> list[dict]:
    return [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            "SELECT id, name FROM subjects WHERE inst_username = ? ORDER BY id", (username,)
        )
    ]


def _class_list(conn: sqlite3.Connection, username: str, class_id: Optional[int] = None) -> list[dict]:
    sql = "SELECT id, class_name, subject_id, created_at FROM classes WHERE inst_username = ?"
    params: list = [username]
    if class_id is not None:
        sql += " AND id = ?"
        params.append(class_id)
    classes = [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]
    if not classes:
        return []

    slot_sql = """
        SELECT t.class_id, t.id, t.start_time, t.end_time, t.subject_id, s.name AS subject
        FROM time_slots t
        LEFT JOIN subjects s ON s.inst_username = t.inst_username AND s.id = t.subject_id
        WHERE t.inst_username = ?"""
    slot_params: list = [username]
    if class_id is not None:
        slot_sql += " AND t.class_id = ?"
        slot_params.append(class_id)
    by_class: dict[int, list[dict]] = {}
    for r in conn.execute(slot_sql + " ORDER BY t.class_id, t.id", slot_params):
        by_class.setdefault(r["class_id"], []).append(
            {
                "id": r["id"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "subject_id": r["subject_id"],
                "subject": r["subject"] or "",
            }
        )
    for c in classes:
        c["time_slots"] = by_class.get(c["id"], [])
        for slot in c["time_slots"]:
            if not slot["subject"]:
                slot["subject"] = c["class_name"]
    return classes


def _student_dict(r: sqlite3.Row) -> dict:
    d = {
        "id": r["id"],
        "student_name": r["student_name"],
        "phone": r["phone"],
        "class_id": r["class_id"],
        "note": r["note"],
        "created_at": r["created_at"],
    }
    if r["slot_ids"]:
        try:
            d["slot_ids"] = json.loads(r["slot_ids"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def _students_by_ids(conn: sqlite3.Connection, username: str, ids: list[int]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), 500):  # SQLite មានដែនកំណត់ចំនួន parameter
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        out.extend(
            _student_dict(r)
            for r in conn.execute(
                f"SELECT * FROM students WHERE inst_username = ? AND id IN ({marks})",
                [username, *chunk],
            )
        )
    out.sort(key=lambda s: s["id"])
    return out


def _get_student(conn: sqlite3.Connection, username: str, student_id: int) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM students WHERE inst_username = ? AND id = ?", (username, student_id)
    ).fetchone()
    return _student_dict(r) if r else None


# ============================================================
# Session files (Telethon)
# ============================================================
def _session_files(username: str) -> list[Path]:
    """ឯកសារ session របស់ស្ថាប័នមួយ: <username>_tg, <username>_tg.session, ...-journal។"""
    base = f"{username}_tg"
    return [
        p for p in SESSIONS_DIR.iterdir()
        if p.is_file() and (p.name == base or p.name.startswith(base + "."))
    ]


def rename_session_files(old_username: str, new_username: str) -> None:
    """ប្រើពេលប្តូរ username ដើម្បីឱ្យ Telegram session តាមទៅជាមួយ។"""
    for p in _session_files(old_username):
        target = p.with_name(p.name.replace(f"{old_username}_tg", f"{new_username}_tg", 1))
        try:
            if not target.exists():
                p.rename(target)
        except OSError:
            pass


async def delete_institution_data(username: str) -> None:
    """
    លុប Telegram session files របស់ស្ថាប័ន។
    (ទិន្នន័យក្នុង Database ត្រូវបានលុបតាម CASCADE ពេល db.delete_institution() ហើយ។)
    """
    for p in _session_files(username):
        try:
            p.unlink()
        except OSError:
            pass


# ============================================================
# Templates
# ============================================================
DEFAULT_TEMPLATES = [
    {
        "id": 1,
        "name": "រំលឹកម៉ោងរៀន",
        "content": "ជម្រាបសួរ លោក/លោកស្រី ជាអាណាព្យាបាលរបស់ {student_name}!\n\nសូមរំលឹកថា {student_name} មានម៉ោងសិក្សាមុខវិជ្ជា {subject} នៅថ្នាក់ {class_name} នៅថ្ងៃទី {date} ចាប់ពី{start_time} ដល់ {end_time}។\n\nសូមជួយរៀបចំឲ្យសិស្សមកដល់ទាន់ម៉ោង។ សូមអរគុណ!",
        "is_default": True,
    },
    {
        "id": 2,
        "name": "ជូនដំណឹងសិស្សមិនបានមករៀន",
        "content": "ជម្រាបសួរ លោក/លោកស្រី ជាអាណាព្យាបាលរបស់ {student_name}!\n\nសូមជម្រាបថា {student_name} មិនបានមករៀនមុខវិជ្ជា {subject} នៅថ្នាក់ {class_name} នៅថ្ងៃទី {date} {start_time} ដល់ {end_time}។\n\nសូមជួយជម្រាបមូលហេតុ ឬទាក់ទងមកសាលា ប្រសិនបើមានព័ត៌មានបន្ថែម។ សូមអរគុណ!",
        "is_default": True,
    },
]


def _template_rows(conn: sqlite3.Connection, username: str) -> list[dict]:
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "content": r["content"],
            "is_default": bool(r["is_default"]),
        }
        for r in conn.execute(
            "SELECT id, name, content, is_default FROM templates WHERE inst_username = ? ORDER BY id",
            (username,),
        )
    ]


def _seed_default_templates(conn: sqlite3.Connection, username: str) -> None:
    """បើស្ថាប័នមិនមាន template សោះ -> ដាក់ template លំនាំដើមចូល (ដូចអាកប្បកិរិយាចាស់)។"""
    has_any = conn.execute(
        "SELECT 1 FROM templates WHERE inst_username = ? LIMIT 1", (username,)
    ).fetchone()
    if has_any:
        return
    for t in DEFAULT_TEMPLATES:
        conn.execute(
            "INSERT INTO templates (inst_username, id, name, content, is_default) VALUES (?, ?, ?, ?, ?)",
            (username, t["id"], t["name"], t["content"], 1 if t["is_default"] else 0),
        )


def _get_templates(username: str) -> list[dict]:
    with db.read() as conn:
        rows = _template_rows(conn, username)
    if rows:
        return rows
    try:
        with db.transaction() as conn:
            _seed_default_templates(conn, username)
            return _template_rows(conn, username)
    except sqlite3.IntegrityError:
        return [dict(t) for t in DEFAULT_TEMPLATES]


# ============================================================
# Telegram config (ក្នុង table telegram_config)
# ============================================================
def _load_tg(username: str) -> dict:
    with db.read() as conn:
        r = conn.execute(
            "SELECT * FROM telegram_config WHERE inst_username = ?", (username,)
        ).fetchone()
    if not r:
        return {
            "api_id": "",
            "api_hash": "",
            "phone": "",
            "session_string": "",
            "connected": False,
            "account_name": "",
        }
    cfg = {
        "api_id": r["api_id"],
        "api_hash": r["api_hash"],
        "phone": r["phone"],
        "session_string": r["session_string"],
        "connected": bool(r["connected"]),
        "account_name": r["account_name"],
    }
    if r["phone_code_hash"]:
        cfg["_phone_code_hash"] = r["phone_code_hash"]
    return cfg


def _save_tg(username: str, cfg: dict) -> None:
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO telegram_config
               (inst_username, api_id, api_hash, phone, session_string, connected,
                account_name, phone_code_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(inst_username) DO UPDATE SET
                 api_id = excluded.api_id,
                 api_hash = excluded.api_hash,
                 phone = excluded.phone,
                 session_string = excluded.session_string,
                 connected = excluded.connected,
                 account_name = excluded.account_name,
                 phone_code_hash = excluded.phone_code_hash""",
            (
                username,
                str(cfg.get("api_id", "")),
                str(cfg.get("api_hash", "")),
                str(cfg.get("phone", "")),
                str(cfg.get("session_string", "")),
                1 if cfg.get("connected") else 0,
                str(cfg.get("account_name", "")),
                cfg.get("_phone_code_hash"),
            ),
        )


# ============================================================
# Router
# ============================================================
router = APIRouter(prefix="/api/manage", tags=["manage"])
public_router = APIRouter(prefix="/api/public", tags=["public"])


@public_router.get("/{username}/classes/{class_id}/students")
async def get_public_class_students(response: Response, username: str, class_id: int):
    """Return the names and message templates for one publicly accessible class."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    with db.read() as conn:
        classes = _class_list(conn, username, class_id)
        if not classes:
            raise HTTPException(404, "Class not found")
        class_info = classes[0]
        students = [
            {"id": r["id"], "student_name": r["student_name"], "class_id": class_id}
            for r in conn.execute(
                "SELECT id, student_name FROM students WHERE inst_username = ? AND class_id = ? ORDER BY id",
                (username, class_id),
            )
        ]
    templates = _get_templates(username)
    cfg = _load_tg(username)
    return {
        "class": {key: class_info.get(key) for key in ("id", "class_name", "subject_id", "time_slots")},
        "students": students,
        "templates": [
            {"id": item.get("id"), "name": item.get("name", ""), "content": item.get("content", "")}
            for item in templates
        ],
        "connected": bool(cfg.get("connected")),
    }


@public_router.post("/{username}/classes/{class_id}/send")
async def send_public_class_messages(request: Request, response: Response, username: str, class_id: int):
    """Send a message to selected guardians in one class from its public page."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    body = await request.json()
    student_ids = body.get("student_ids") if isinstance(body, dict) else None
    message = body.get("custom_message") if isinstance(body, dict) else None
    if (
        not isinstance(student_ids, list)
        or not student_ids
        or len(student_ids) > 50
        or any(not isinstance(student_id, int) or isinstance(student_id, bool) for student_id in student_ids)
    ):
        raise HTTPException(400, "Select between 1 and 50 students")
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        raise HTTPException(400, "Message is required and must be under 2000 characters")
    with db.read() as conn:
        if not _class_list(conn, username, class_id):
            raise HTTPException(404, "Class not found")
        class_student_ids = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM students WHERE inst_username = ? AND class_id = ?",
                (username, class_id),
            )
        }
    if any(student_id not in class_student_ids for student_id in student_ids):
        raise HTTPException(403, "Selected student is not in this class")
    if not _load_tg(username).get("connected"):
        raise HTTPException(400, "Telegram is not connected")
    body["class_id"] = class_id
    result = await _send_messages(username, body)
    for item in result.get("results", []):
        item.pop("phone", None)
    return result


# ----------------------------------------------------------
# SUBJECTS & CLASSES
# ----------------------------------------------------------
@router.get("/{username}/classes")
async def list_classes(request: Request, username: str):
    _require_owner(request, username)
    with db.read() as conn:
        return {"classes": _class_list(conn, username), "subjects": _subject_list(conn, username)}


def get_public_dashboard_data(username: str) -> dict:
    """Return only school schedule data and aggregate class student counts."""
    with db.read() as conn:
        subjects = _subject_list(conn, username)
        classes = _class_list(conn, username)
        student_counts = {
            r["class_id"]: r["n"]
            for r in conn.execute(
                """SELECT class_id, COUNT(*) AS n FROM students
                   WHERE inst_username = ? AND class_id IS NOT NULL GROUP BY class_id""",
                (username,),
            )
        }

    public_classes = [
        {
            "id": c["id"],
            "class_name": c["class_name"],
            "subject_id": c["subject_id"],
            "time_slots": [
                {key: slot.get(key, "") for key in ("start_time", "end_time", "subject_id", "subject")}
                for slot in c["time_slots"]
            ],
        }
        for c in classes
    ]
    return {
        "classes": public_classes,
        "subjects": subjects,
        "student_counts": student_counts,
    }


@router.post("/{username}/subjects")
async def create_subject(
    request: Request,
    username: str,
    name: str = Form(...),
):
    _require_owner(request, username)
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "សូមបញ្ចូលឈ្មោះមុខវិជ្ជា")
    with db.transaction() as conn:
        if any(s["name"].casefold() == clean_name.casefold() for s in _subject_list(conn, username)):
            raise HTTPException(400, "មុខវិជ្ជានេះមានរួចហើយ")
        subject = {"id": db.next_id(conn, "subjects", username), "name": clean_name}
        conn.execute(
            "INSERT INTO subjects (inst_username, id, name) VALUES (?, ?, ?)",
            (username, subject["id"], clean_name),
        )
    return {"message": "បង្កើតមុខវិជ្ជាបានជោគជ័យ", "data": subject}


@router.put("/{username}/subjects/{subject_id}")
async def update_subject(
    request: Request,
    username: str,
    subject_id: int,
    name: str = Form(...),
):
    _require_owner(request, username)
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "សូមបញ្ចូលឈ្មោះមុខវិជ្ជា")
    with db.transaction() as conn:
        subjects = _subject_list(conn, username)
        if any(s["id"] != subject_id and s["name"].casefold() == clean_name.casefold() for s in subjects):
            raise HTTPException(400, "មុខវិជ្ជានេះមានរួចហើយ")
        subject = next((s for s in subjects if s["id"] == subject_id), None)
        if subject is None:
            raise HTTPException(404, "រកមិនឃើញមុខវិជ្ជា")
        # ឈ្មោះមុខវិជ្ជានៅក្នុងម៉ោងសិក្សា ត្រូវបានយកតាម JOIN ដូច្នេះកែត្រឹម table subjects ល្មម
        conn.execute(
            "UPDATE subjects SET name = ? WHERE inst_username = ? AND id = ?",
            (clean_name, username, subject_id),
        )
        subject["name"] = clean_name
    return {"message": "កែប្រែមុខវិជ្ជាបានជោគជ័យ", "data": subject}


@router.delete("/{username}/subjects/{subject_id}")
async def delete_subject(
    request: Request,
    username: str,
    subject_id: int,
    password: str = Form(...),
):
    _require_owner(request, username)
    require_inst_password(username, password)
    with db.transaction() as conn:
        in_use = conn.execute(
            """SELECT 1 FROM classes WHERE inst_username = ? AND subject_id = ?
               UNION ALL
               SELECT 1 FROM time_slots WHERE inst_username = ? AND subject_id = ? LIMIT 1""",
            (username, subject_id, username, subject_id),
        ).fetchone()
        if in_use:
            raise HTTPException(400, "មិនអាចលុបមុខវិជ្ជាដែលមានថ្នាក់កំពុងប្រើបានទេ")
        cur = conn.execute(
            "DELETE FROM subjects WHERE inst_username = ? AND id = ?", (username, subject_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "រកមិនឃើញមុខវិជ្ជា")
    return {"message": "លុបមុខវិជ្ជាបានជោគជ័យ"}


@router.post("/{username}/classes")
async def create_class(
    request: Request,
    username: str,
    class_name: str = Form(...),
    subject_id: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
):
    _require_owner(request, username)
    clean_class_name = class_name.strip()
    clean_start_time = start_time.strip()
    clean_end_time = end_time.strip()
    with db.transaction() as conn:
        subject = next((s for s in _subject_list(conn, username) if s["id"] == subject_id), None)
        if subject is None:
            raise HTTPException(400, "សូមបង្កើត ឬជ្រើសរើសមុខវិជ្ជាឲ្យបានត្រឹមត្រូវ")
        if not clean_class_name:
            raise HTTPException(400, "សូមបញ្ចូលឈ្មោះថ្នាក់រៀន")
        if not clean_start_time or not clean_end_time:
            raise HTTPException(400, "សូមបញ្ចូលម៉ោងចាប់ផ្ដើម និងម៉ោងបញ្ចប់")
        new_id = db.next_id(conn, "classes", username)
        created_at = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO classes (inst_username, id, class_name, subject_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, new_id, clean_class_name, subject_id, created_at),
        )
        conn.execute(
            """INSERT INTO time_slots (inst_username, class_id, id, start_time, end_time, subject_id)
               VALUES (?, ?, 1, ?, ?, ?)""",
            (username, new_id, clean_start_time, clean_end_time, subject_id),
        )
        new_class = _class_list(conn, username, new_id)[0]
    return {"message": "បង្កើតថ្នាក់រៀនបានជោគជ័យ", "data": new_class}


@router.delete("/{username}/classes/{class_id}")
async def delete_class(
    request: Request,
    username: str,
    class_id: int,
    password: str = Form(...),
):
    _require_owner(request, username)
    require_inst_password(username, password)
    with db.transaction() as conn:
        exists = conn.execute(
            "SELECT 1 FROM classes WHERE inst_username = ? AND id = ?", (username, class_id)
        ).fetchone()
        if not exists:
            raise HTTPException(404, "រកមិនឃើញថ្នាក់រៀន")
        # លុបសិស្ស និងម៉ោងសិក្សារបស់ថ្នាក់នេះ រួចលុបថ្នាក់ (ក្នុង transaction តែមួយ)
        conn.execute(
            "DELETE FROM students WHERE inst_username = ? AND class_id = ?", (username, class_id)
        )
        conn.execute(
            "DELETE FROM time_slots WHERE inst_username = ? AND class_id = ?", (username, class_id)
        )
        conn.execute("DELETE FROM classes WHERE inst_username = ? AND id = ?", (username, class_id))
    return {"message": "លុបថ្នាក់រៀនបានជោគជ័យ"}


# ----------------------------------------------------------
# TIME SLOTS
# ----------------------------------------------------------
@router.put("/{username}/classes/{class_id}/timeslots/{slot_id}")
async def update_timeslot(
    request: Request,
    username: str,
    class_id: int,
    slot_id: int,
    start_time: str = Form(...),
    end_time: str = Form(...),
):
    _require_owner(request, username)
    with db.transaction() as conn:
        classes = _class_list(conn, username, class_id)
        slot = next(
            (s for s in (classes[0]["time_slots"] if classes else []) if s["id"] == slot_id), None
        )
        if not classes or slot is None:
            raise HTTPException(404, "រកមិនឃើញម៉ោងសិក្សា")
        # ម៉ោងសិក្សាប្រើមុខវិជ្ជារបស់ថ្នាក់ (ដូចដើម)
        conn.execute(
            """UPDATE time_slots SET start_time = ?, end_time = ?, subject_id = ?
               WHERE inst_username = ? AND class_id = ? AND id = ?""",
            (start_time.strip(), end_time.strip(), classes[0]["subject_id"],
             username, class_id, slot_id),
        )
        updated = next(s for s in _class_list(conn, username, class_id)[0]["time_slots"] if s["id"] == slot_id)
    return {"message": "កែប្រែម៉ោងសិក្សាបានជោគជ័យ", "data": updated}


# ----------------------------------------------------------
# STUDENTS (CRUD & BATCH IMPORT)
# ----------------------------------------------------------
@router.get("/{username}/students")
async def list_students(
    request: Request,
    username: str,
    class_id: int | None = None,
    slot_id: int | None = None,
):
    _require_owner(request, username)
    sql = "SELECT * FROM students WHERE inst_username = ?"
    params: list = [username]
    if class_id is not None:
        sql += " AND class_id = ?"
        params.append(class_id)
    with db.read() as conn:
        students = [_student_dict(r) for r in conn.execute(sql + " ORDER BY id", params)]
    return {"students": students}


@router.post("/{username}/students")
async def create_student(
    request: Request,
    username: str,
    student_name: str = Form(...),
    phone: str = Form(...),
    class_id: int = Form(...),
    slot_id: int | None = Form(None),
    note: str = Form(""),
):
    _require_owner(request, username)
    with db.transaction() as conn:
        if not conn.execute(
            "SELECT 1 FROM classes WHERE inst_username = ? AND id = ?", (username, class_id)
        ).fetchone():
            raise HTTPException(400, "ថ្នាក់រៀនមិនត្រឹមត្រូវ")
        new_id = db.next_id(conn, "students", username)
        conn.execute(
            """INSERT INTO students
               (inst_username, id, student_name, phone, class_id, note, slot_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                username, new_id, student_name.strip(), clean_guardian_contact(phone),
                class_id, note.strip(),
                json.dumps([slot_id]) if slot_id is not None else None,
                datetime.now().isoformat(),
            ),
        )
        student = _get_student(conn, username, new_id)
    return {"message": "បន្ថែមសិស្សបានជោគជ័យ", "data": student}


@router.post("/{username}/students/batch")
async def batch_create_students(
    request: Request,
    username: str,
):
    """
    បញ្ចូលសិស្សជាកញ្ចប់ (Import from Excel / Multiple Students)
    """
    _require_owner(request, username)

    body = await request.json()
    new_students = body.get("students", []) if isinstance(body, dict) else []
    if not isinstance(new_students, list) or not new_students:
        raise HTTPException(400, "មិនមានទិន្នន័យសិស្សសម្រាប់បញ្ចូលទេ")

    with db.transaction() as conn:
        classes = _class_list(conn, username)
        if not classes:
            raise HTTPException(400, "សូមបង្កើតថ្នាក់រៀនជាមុនសិន មុនពេលបញ្ចូលសិស្ស")
        class_id_set = {c["id"] for c in classes}
        class_name_map = {str(c["class_name"]).strip().casefold(): c["id"] for c in classes}
        default_class_id = classes[0]["id"]

        next_student_id = db.next_id(conn, "students", username)
        created = 0
        for item in new_students:
            if not isinstance(item, dict):
                continue
            name = str(item.get("student_name", "")).strip()
            contact = clean_guardian_contact(str(item.get("phone", "")).strip())
            if not name:
                continue

            c_id = item.get("class_id")
            if c_id not in class_id_set:
                c_name = str(item.get("class_name", "")).strip().casefold()
                c_id = class_name_map.get(c_name, default_class_id)

            slot_ids = item.get("slot_ids")
            conn.execute(
                """INSERT INTO students
                   (inst_username, id, student_name, phone, class_id, note, slot_ids, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    username, next_student_id, name, contact, c_id,
                    str(item.get("note", "")).strip(),
                    json.dumps(slot_ids) if isinstance(slot_ids, list) and slot_ids else None,
                    datetime.now().isoformat(),
                ),
            )
            next_student_id += 1
            created += 1

        if not created:
            raise HTTPException(400, "ពុំមានទិន្នន័យសិស្សត្រឹមត្រូវណាមួយត្រូវបានរកឃើញទេ")

    return {
        "message": f"បានបញ្ចូលសិស្សចំនួន {created} នាក់ដោយជោគជ័យ",
        "count": created,
    }


@router.put("/{username}/students/{student_id}")
async def update_student(
    request: Request,
    username: str,
    student_id: int,
    student_name: str = Form(...),
    phone: str = Form(...),
    class_id: int = Form(...),
    note: str = Form(""),
):
    _require_owner(request, username)
    with db.transaction() as conn:
        if not conn.execute(
            "SELECT 1 FROM classes WHERE inst_username = ? AND id = ?", (username, class_id)
        ).fetchone():
            raise HTTPException(400, "ថ្នាក់រៀនមិនត្រឹមត្រូវ")
        cur = conn.execute(
            """UPDATE students SET student_name = ?, phone = ?, class_id = ?, note = ?
               WHERE inst_username = ? AND id = ?""",
            (student_name.strip(), clean_guardian_contact(phone), class_id, note.strip(),
             username, student_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "រកមិនឃើញសិស្ស")
        student = _get_student(conn, username, student_id)
    return {"message": "កែប្រែសិស្សបានជោគជ័យ", "data": student}


@router.delete("/{username}/students/{student_id}")
async def delete_student(request: Request, username: str, student_id: int):
    _require_owner(request, username)
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM students WHERE inst_username = ? AND id = ?", (username, student_id)
        )
    return {"message": "លុបសិស្សបានជោគជ័យ"}


# ----------------------------------------------------------
# MESSAGE TEMPLATES
# ----------------------------------------------------------
@router.get("/{username}/templates")
async def list_templates(request: Request, username: str):
    _require_owner(request, username)
    return {"templates": _get_templates(username)}


@router.post("/{username}/templates")
async def create_template(
    request: Request,
    username: str,
    name: str = Form(...),
    content: str = Form(...),
):
    _require_owner(request, username)
    with db.transaction() as conn:
        _seed_default_templates(conn, username)  # ដូចដើម: template ថ្មីបន្ថែមពីលើ template លំនាំដើម
        new_id = db.next_id(conn, "templates", username)
        conn.execute(
            "INSERT INTO templates (inst_username, id, name, content, is_default) VALUES (?, ?, ?, ?, 0)",
            (username, new_id, name.strip(), content.strip()),
        )
    t = {"id": new_id, "name": name.strip(), "content": content.strip(), "is_default": False}
    return {"message": "បង្កើត template បានជោគជ័យ", "data": t}


@router.put("/{username}/templates/{template_id}")
async def update_template(
    request: Request,
    username: str,
    template_id: int,
    name: str = Form(...),
    content: str = Form(...),
):
    _require_owner(request, username)
    _get_templates(username)  # ធានាថា template លំនាំដើមមានក្នុង Database (កែបាន)
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE templates SET name = ?, content = ? WHERE inst_username = ? AND id = ?",
            (name.strip(), content.strip(), username, template_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "រកមិនឃើញ template")
        t = next(t for t in _template_rows(conn, username) if t["id"] == template_id)
    return {"message": "កែប្រែ template បានជោគជ័យ", "data": t}


@router.delete("/{username}/templates/{template_id}")
async def delete_template(request: Request, username: str, template_id: int):
    _require_owner(request, username)
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM templates WHERE inst_username = ? AND id = ?", (username, template_id)
        )
    return {"message": "លុប template បានជោគជ័យ"}


# ----------------------------------------------------------
# TELEGRAM CONFIG & SEND (TELETHON ENGINE)
# ----------------------------------------------------------
@router.get("/{username}/telegram/status")
async def tg_status(request: Request, username: str):
    _require_owner(request, username)
    cfg = _load_tg(username)
    return {
        "connected": bool(cfg.get("connected")),
        "phone": cfg.get("phone", ""),
        "account_name": cfg.get("account_name", ""),
        "has_api": bool(cfg.get("api_id") and cfg.get("api_hash")),
    }


@router.post("/{username}/telegram/config")
async def tg_save_config(
    request: Request,
    username: str,
    api_id: str = Form(...),
    api_hash: str = Form(...),
    phone: str = Form(...),
):
    _require_owner(request, username)
    cfg = _load_tg(username)
    cfg["api_id"] = api_id.strip()
    cfg["api_hash"] = api_hash.strip()
    cfg["phone"] = phone.strip()
    cfg["connected"] = False
    _save_tg(username, cfg)
    return {"message": "រក្សាទុកការកំណត់ Telegram បានជោគជ័យ"}


@router.post("/{username}/telegram/send_code")
async def tg_send_code(request: Request, username: str):
    _require_owner(request, username)

    cfg = _load_tg(username)
    if not cfg.get("api_id") or not cfg.get("api_hash") or not cfg.get("phone"):
        raise HTTPException(400, "សូមបញ្ចូល API ID, API Hash និងលេខទូរសព្ទជាមុន")

    try:
        from telethon import TelegramClient

        session_file = str(SESSIONS_DIR / f"{username}_tg")
        client = TelegramClient(
            session_file,
            int(cfg["api_id"]),
            cfg["api_hash"],
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                result = await client.send_code_request(cfg["phone"])
                cfg["_phone_code_hash"] = result.phone_code_hash
                _save_tg(username, cfg)
                return {"message": "លេខកូដត្រូវបានផ្ញើទៅលេខទូរសព្ទរបស់អ្នក", "need_code": True}
            else:
                me = await client.get_me()
                cfg["connected"] = True
                cfg["account_name"] = f"{me.first_name or ''} {me.last_name or ''}".strip()
                _save_tg(username, cfg)
                return {"message": "ភ្ជាប់រួចហើយ", "connected": True}
        finally:
            await client.disconnect()
    except Exception as e:
        raise HTTPException(400, f"មិនអាចផ្ញើលេខកូដបានទេ: {str(e)}")


@router.post("/{username}/telegram/verify")
async def tg_verify(
    request: Request,
    username: str,
    code: str = Form(...),
    password: str = Form(""),
):
    _require_owner(request, username)

    cfg = _load_tg(username)
    if not cfg.get("_phone_code_hash"):
        raise HTTPException(400, "សូមផ្ញើលេខកូដជាមុនសិន")

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

        session_file = str(SESSIONS_DIR / f"{username}_tg")
        client = TelegramClient(
            session_file,
            int(cfg["api_id"]),
            cfg["api_hash"],
        )
        await client.connect()
        try:
            try:
                await client.sign_in(
                    cfg["phone"],
                    code.strip(),
                    phone_code_hash=cfg["_phone_code_hash"],
                )
            except SessionPasswordNeededError:
                if not password:
                    return JSONResponse(
                        status_code=400,
                        content={"error": "ត្រូវការពាក្យសម្ងាត់ 2FA", "need_2fa": True},
                    )
                await client.sign_in(password=password)

            me = await client.get_me()
            cfg["connected"] = True
            cfg["account_name"] = f"{me.first_name or ''} {me.last_name or ''}".strip()
            cfg.pop("_phone_code_hash", None)
            _save_tg(username, cfg)
            return {"message": "ភ្ជាប់ Telegram ជោគជ័យ!", "account_name": cfg["account_name"]}
        finally:
            await client.disconnect()
    except Exception as e:
        raise HTTPException(400, f"ផ្ទៀងផ្ទាត់មិនបាន: {str(e)}")


@router.post("/{username}/telegram/disconnect")
async def tg_disconnect(request: Request, username: str):
    _require_owner(request, username)

    cfg = _load_tg(username)
    cfg["connected"] = False
    cfg["account_name"] = ""
    cfg.pop("_phone_code_hash", None)
    _save_tg(username, cfg)

    for p in _session_files(username):
        try:
            p.unlink()
        except OSError:
            pass
    return {"message": "បានផ្តាច់ Telegram"}


def _render_template(
    content: str,
    student: dict,
    class_info: dict | None,
    slot: dict | None,
    day: str | None = None,
) -> str:
    date_str = ""
    if day:
        try:
            date_str = datetime.strptime(day, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            date_str = day
    if not date_str:
        date_str = datetime.now().strftime("%d/%m/%Y")

    subject_name = (slot or {}).get("subject") or (class_info or {}).get("class_name", "")
    start_time = (slot or {}).get("start_time", "")
    end_time = (slot or {}).get("end_time", "")

    data = {
        "student_name": student.get("student_name", ""),
        "phone": student.get("phone", ""),
        "class_name": (class_info or {}).get("class_name", ""),
        "subject": subject_name,
        "start_time": start_time,
        "end_time": end_time,
        "date": date_str,
        "note": student.get("note", ""),
    }
    result = content
    for k, v in data.items():
        result = result.replace("{" + k + "}", str(v or ""))
    return result


async def _resolve_contact_to_user(client, raw_contact: str, student_name: str):
    candidates = _telegram_contact_candidates(raw_contact)
    if not candidates:
        return None, "ព័ត៌មានទំនាក់ទំនង Telegram មិនត្រឹមត្រូវ"

    last_error = None

    for cand in candidates:
        kind = cand["kind"]
        val = cand["value"]

        if kind in ("username", "entity"):
            try:
                user = await client.get_entity(val)
                if user:
                    return user, None
            except Exception as e:
                last_error = str(e)

        elif kind == "id":
            try:
                user_id = int(val)
                user = await client.get_entity(user_id)
                if user:
                    return user, None
            except Exception as e:
                last_error = str(e)

    phone_cands = [c["value"] for c in candidates if c["kind"] == "phone"]
    for clean_phone in phone_cands:
        phone_with_plus = f"+{clean_phone}"

        for p in (phone_with_plus, clean_phone):
            try:
                user = await client.get_entity(p)
                if user:
                    return user, None
            except Exception:
                pass

        try:
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact

            contact = InputPhoneContact(
                client_id=0,
                phone=phone_with_plus,
                first_name=student_name[:30] or "Parent",
                last_name="",
            )
            imported = await client(ImportContactsRequest([contact]))

            if getattr(imported, "users", None) and len(imported.users) > 0:
                return imported.users[0], None

            for p in (phone_with_plus, clean_phone):
                try:
                    user = await client.get_entity(p)
                    if user:
                        return user, None
                except Exception:
                    pass

        except Exception as e:
            err_text = str(e)
            if "wait of" in err_text.lower():
                return None, f"Telegram កំពុងជាប់ FloodWait: {err_text}"
            last_error = err_text

    return None, f"មិនអាចស្វែងរកគណនី Telegram ឃើញទេ ({last_error or 'ទិន្នន័យមិនត្រឹមត្រូវ'})"


@router.post("/{username}/telegram/send")
async def tg_send_messages(request: Request, username: str):
    _require_owner(request, username)
    return await _send_messages(username, await request.json())


async def _send_messages(username: str, body: dict):
    cfg = _load_tg(username)
    if not cfg.get("connected"):
        raise HTTPException(400, "Telegram មិនទាន់ភ្ជាប់ទេ សូមភ្ជាប់ Telegram ជាមុនសិន")

    student_ids = body.get("student_ids", [])
    template_id = body.get("template_id")
    custom_message = body.get("custom_message")
    slot_id = body.get("slot_id")
    day = str(body.get("date") or datetime.now().strftime("%Y-%m-%d"))

    if not student_ids:
        raise HTTPException(400, "សូមជ្រើសរើសសិស្សយ៉ាងហោចណាស់ម្នាក់")

    # ទាញទិន្នន័យចេញពី Database មុន ហើយបិទ connection ភ្លាម
    # (មិនកាន់ connection ពេលកំពុងផ្ញើសារ Telegram ដែលចំណាយពេលយូរ)
    wanted_ids = [i for i in student_ids if isinstance(i, int) and not isinstance(i, bool)]
    with db.read() as conn:
        classes = _class_list(conn, username)
        selected = _students_by_ids(conn, username, wanted_ids)

    if not selected:
        raise HTTPException(400, "រកមិនឃើញទិន្នន័យសិស្សដែលបានជ្រើស")

    message_tpl = custom_message
    if not message_tpl and template_id:
        tpl = next((t for t in _get_templates(username) if str(t.get("id")) == str(template_id)), None)
        if tpl:
            message_tpl = tpl["content"]
    if not message_tpl:
        raise HTTPException(400, "សូមជ្រើសរើស template ឬបញ្ចូលខ្លឹមសារសារ")

    results = []
    try:
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError, UserPrivacyRestrictedError

        session_file = str(SESSIONS_DIR / f"{username}_tg")
        client = TelegramClient(
            session_file,
            int(cfg["api_id"]),
            cfg["api_hash"],
        )
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            cfg["connected"] = False
            _save_tg(username, cfg)
            raise HTTPException(400, "Telegram Session ផុតកំណត់ សូមចូលទៅភ្ជាប់ Telegram ឡើងវិញ")

        try:
            for student in selected:
                raw_contact = student.get("phone", "").strip()

                if not raw_contact:
                    results.append({
                        "student": student["student_name"],
                        "phone": raw_contact,
                        "ok": False,
                        "error": "មិនមានព័ត៌មានទំនាក់ទំនង Telegram",
                    })
                    continue

                c_info = next((c for c in classes if c.get("id") == student.get("class_id")), None)
                s_info = None
                if c_info:
                    slots = c_info.get("time_slots", [])
                    if slot_id is not None:
                        s_info = next((s for s in slots if s.get("id") == slot_id), None)
                    if not s_info and slots:
                        s_info = slots[0]

                text = _render_template(message_tpl, student, c_info, s_info, day)

                target_user, err_msg = await _resolve_contact_to_user(
                    client, raw_contact, student.get("student_name", "")
                )

                if not target_user:
                    results.append({
                        "student": student["student_name"],
                        "phone": raw_contact,
                        "ok": False,
                        "error": err_msg or "រកមិនឃើញគណនី Telegram",
                    })
                    continue

                try:
                    await client.send_message(target_user, text)
                    results.append({
                        "student": student["student_name"],
                        "phone": raw_contact,
                        "ok": True,
                    })
                except FloodWaitError as e:
                    results.append({
                        "student": student["student_name"],
                        "phone": raw_contact,
                        "ok": False,
                        "error": f"Telegram កំពុងរារាំង សូមរង់ចាំ {e.seconds} វិនាទី",
                    })
                    break
                except UserPrivacyRestrictedError:
                    results.append({
                        "student": student["student_name"],
                        "phone": raw_contact,
                        "ok": False,
                        "error": "អាណាព្យាបាលបានបិទ Privacy មិនឱ្យទទួលសារពីអ្នកដទៃ",
                    })
                except Exception as e:
                    results.append({
                        "student": student["student_name"],
                        "phone": raw_contact,
                        "ok": False,
                        "error": f"បរាជ័យ: {str(e)}",
                    })

                await asyncio.sleep(1.2)
        finally:
            await client.disconnect()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"កំហុសពេលផ្ញើសារ: {str(e)}")

    success_count = sum(1 for r in results if r.get("ok"))
    return {
        "message": f"ផ្ញើបាន {success_count}/{len(results)} សារ",
        "results": results,
    }
