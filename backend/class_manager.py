"""
class_manager.py
----------------
គ្រប់គ្រង Class, TimeSlot, Student, Message Template, Telegram Session
សម្រាប់ប្រព័ន្ធគ្រប់គ្រងស្ថាប័ន + ផ្ញើសារ Telegram (Telethon)
គាំទ្រ Batch Import សិស្ស និងទទួលគ្រប់ទម្រង់ Telegram (Phone, Link, Username, ID)
ភ្ជាប់ជាមួយ Supabase Cloud Database ដោយស្វ័យប្រវត្តិ
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from db_supabase import load_data, save_data, is_supabase_enabled, delete_prefix_data

# ============================================================
# Paths
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", str(BASE_DIR / "sessions"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
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


# ============================================================
# Data helpers (Supabase + Local JSON fallback)
# ============================================================
def _inst_dir(username: str) -> Path:
    d = DATA_DIR / username
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(username: str, name: str, default: Any = None) -> Any:
    storage_key = f"{username}:{name}"
    if is_supabase_enabled():
        val = load_data(storage_key, default=None)
        if val is not None:
            return val

    # Fallback to local JSON file
    path = _inst_dir(username) / f"{name}.json"
    if not path.exists():
        return default if default is not None else []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Auto-migrate local JSON file to Supabase
            if is_supabase_enabled() and data:
                save_data(storage_key, data)
            return data
    except (json.JSONDecodeError, OSError):
        return default if default is not None else []


def _save(username: str, name: str, data: Any) -> None:
    storage_key = f"{username}:{name}"
    # 1. Save directly to Supabase Cloud
    if is_supabase_enabled():
        save_data(storage_key, data)

    # 2. Keep local backup copy
    try:
        path = _inst_dir(username) / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception:
        pass


def _next_id(items: list) -> int:
    return max((i.get("id", 0) for i in items), default=0) + 1


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
# Delete all institution data
# ============================================================
async def delete_institution_data(username: str) -> None:
    import shutil
    # Delete from Supabase
    if is_supabase_enabled():
        delete_prefix_data(f"{username}:")
        
    d = DATA_DIR / username
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    for f in SESSIONS_DIR.glob(f"{username}_*"):
        try:
            f.unlink()
        except OSError:
            pass


# ============================================================
# Router
# ============================================================
router = APIRouter(prefix="/api/manage", tags=["manage"])
public_router = APIRouter(prefix="/api/public", tags=["public"])


@public_router.get("/{username}/classes/{class_id}/students")
async def get_public_class_students(response: Response, username: str, class_id: int):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    class_info = next(
        (c for c in _load(username, "classes", []) if c.get("id") == class_id),
        None,
    )
    if not class_info:
        raise HTTPException(404, "Class not found")
    students = [
        {"id": student.get("id"), "student_name": student.get("student_name", ""), "class_id": class_id}
        for student in _load(username, "students", [])
        if student.get("class_id") == class_id
    ]
    templates = _load(username, "templates", DEFAULT_TEMPLATES) or DEFAULT_TEMPLATES
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
    class_info = next(
        (c for c in _load(username, "classes", []) if c.get("id") == class_id),
        None,
    )
    if not class_info:
        raise HTTPException(404, "Class not found")
    class_student_ids = {
        student.get("id")
        for student in _load(username, "students", [])
        if student.get("class_id") == class_id
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


def _ensure_subjects(username: str) -> tuple[list[dict], list[dict]]:
    subjects = _load(username, "subjects", []) or []
    classes = _load(username, "classes", [])
    subjects_by_id = {subject.get("id"): subject for subject in subjects}
    subjects_by_name = {
        str(subject.get("name", "")).strip().casefold(): subject
        for subject in subjects
    }
    subjects_changed = False
    classes_changed = False

    def get_or_create_subject(name: str) -> dict:
        nonlocal subjects_changed
        clean_name = name.strip() or "មុខវិជ្ជាទូទៅ"
        key = clean_name.casefold()
        subject = subjects_by_name.get(key)
        if subject is None:
            subject = {"id": _next_id(subjects), "name": clean_name}
            subjects.append(subject)
            subjects_by_id[subject["id"]] = subject
            subjects_by_name[key] = subject
            subjects_changed = True
        return subject

    for class_info in classes:
        class_subject = subjects_by_id.get(class_info.get("subject_id"))
        slot_subject_ids = set()
        for slot in class_info.get("time_slots", []):
            subject = subjects_by_id.get(slot.get("subject_id")) or class_subject
            if subject is None:
                subject = get_or_create_subject(
                    slot.get("subject") or class_info.get("class_name", "")
                )
            slot_subject_ids.add(subject["id"])
            if slot.get("subject_id") != subject["id"] or slot.get("subject") != subject["name"]:
                slot["subject_id"] = subject["id"]
                slot["subject"] = subject["name"]
                classes_changed = True

        if class_subject is None:
            if len(slot_subject_ids) == 1:
                class_info["subject_id"] = next(iter(slot_subject_ids))
                classes_changed = True
            elif not slot_subject_ids:
                subject = get_or_create_subject(class_info.get("class_name", ""))
                class_info["subject_id"] = subject["id"]
                classes_changed = True

    if subjects_changed:
        _save(username, "subjects", subjects)
    if classes_changed:
        _save(username, "classes", classes)
    return subjects, classes


@router.get("/{username}/classes")
async def list_classes(request: Request, username: str):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")
    subjects, classes = _ensure_subjects(username)
    return {"classes": classes, "subjects": subjects}


def get_public_dashboard_data(username: str) -> dict:
    subjects = _load(username, "subjects", [])
    classes = _load(username, "classes", [])
    students = _load(username, "students", [])
    student_counts: dict[int, int] = {}

    for student in students:
        class_id = student.get("class_id")
        if isinstance(class_id, int):
            student_counts[class_id] = student_counts.get(class_id, 0) + 1

    public_classes = []
    for class_info in classes:
        public_class = {
            "id": class_info.get("id"),
            "class_name": class_info.get("class_name", ""),
            "subject_id": class_info.get("subject_id"),
            "time_slots": [
                {
                    key: slot.get(key, "")
                    for key in ("start_time", "end_time", "subject_id", "subject")
                }
                for slot in class_info.get("time_slots", [])
            ],
        }
        public_classes.append(public_class)

    public_subjects = [
        {"id": subject.get("id"), "name": subject.get("name", "")}
        for subject in subjects
    ]
    return {
        "classes": public_classes,
        "subjects": public_subjects,
        "student_counts": student_counts,
    }


@router.post("/{username}/subjects")
async def create_subject(
    request: Request,
    username: str,
    name: str = Form(...),
):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    subjects, _ = _ensure_subjects(username)
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "សូមបញ្ចូលឈ្មោះមុខវិជ្ជា")
    if any(subject.get("name", "").casefold() == clean_name.casefold() for subject in subjects):
        raise HTTPException(400, "មុខវិជ្ជានេះមានរួចហើយ")
    subject = {"id": _next_id(subjects), "name": clean_name}
    subjects.append(subject)
    _save(username, "subjects", subjects)
    return {"message": "បង្កើតមុខវិជ្ជាបានជោគជ័យ", "data": subject}


@router.put("/{username}/subjects/{subject_id}")
async def update_subject(
    request: Request,
    username: str,
    subject_id: int,
    name: str = Form(...),
):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    subjects, classes = _ensure_subjects(username)
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "សូមបញ្ចូលឈ្មោះមុខវិជ្ជា")
    if any(
        subject.get("id") != subject_id
        and subject.get("name", "").casefold() == clean_name.casefold()
        for subject in subjects
    ):
        raise HTTPException(400, "មុខវិជ្ជានេះមានរួចហើយ")
    subject = next((item for item in subjects if item.get("id") == subject_id), None)
    if subject is None:
        raise HTTPException(404, "រកមិនឃើញមុខវិជ្ជា")
    subject["name"] = clean_name
    for class_info in classes:
        for slot in class_info.get("time_slots", []):
            if slot.get("subject_id") == subject_id:
                slot["subject"] = clean_name
    _save(username, "subjects", subjects)
    _save(username, "classes", classes)
    return {"message": "កែប្រែមុខវិជ្ជាបានជោគជ័យ", "data": subject}


@router.delete("/{username}/subjects/{subject_id}")
async def delete_subject(
    request: Request,
    username: str,
    subject_id: int,
    password: str = Form(...),
):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")
    require_inst_password(username, password)

    subjects, classes = _ensure_subjects(username)
    if any(
        class_info.get("subject_id") == subject_id
        or any(slot.get("subject_id") == subject_id for slot in class_info.get("time_slots", []))
        for class_info in classes
    ):
        raise HTTPException(400, "មិនអាចលុបមុខវិជ្ជាដែលមានថ្នាក់កំពុងប្រើបានទេ")
    remaining = [subject for subject in subjects if subject.get("id") != subject_id]
    if len(remaining) == len(subjects):
        raise HTTPException(404, "រកមិនឃើញមុខវិជ្ជា")
    _save(username, "subjects", remaining)
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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    subjects, classes = _ensure_subjects(username)
    subject = next((item for item in subjects if item.get("id") == subject_id), None)
    if subject is None:
        raise HTTPException(400, "សូមបង្កើត ឬជ្រើសរើសមុខវិជ្ជាឲ្យបានត្រឹមត្រូវ")
    clean_class_name = class_name.strip()
    clean_start_time = start_time.strip()
    clean_end_time = end_time.strip()
    if not clean_class_name:
        raise HTTPException(400, "សូមបញ្ចូលឈ្មោះថ្នាក់រៀន")
    if not clean_start_time or not clean_end_time:
        raise HTTPException(400, "សូមបញ្ចូលម៉ោងចាប់ផ្ដើម និងម៉ោងបញ្ចប់")
    new_id = _next_id(classes)
    new_class = {
        "id": new_id,
        "class_name": clean_class_name,
        "subject_id": subject_id,
        "time_slots": [{
            "id": 1,
            "start_time": clean_start_time,
            "end_time": clean_end_time,
            "subject_id": subject_id,
            "subject": subject["name"],
        }],
        "created_at": datetime.now().isoformat(),
    }
    classes.append(new_class)
    _save(username, "classes", classes)
    return {"message": "បង្កើតថ្នាក់រៀនបានជោគជ័យ", "data": new_class}


@router.delete("/{username}/classes/{class_id}")
async def delete_class(
    request: Request,
    username: str,
    class_id: int,
    password: str = Form(...),
):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")
    require_inst_password(username, password)

    classes = _load(username, "classes", [])
    if not any(class_info.get("id") == class_id for class_info in classes):
        raise HTTPException(404, "រកមិនឃើញថ្នាក់រៀន")
    classes = [c for c in classes if c.get("id") != class_id]
    _save(username, "classes", classes)

    students = _load(username, "students", [])
    students = [s for s in students if s.get("class_id") != class_id]
    _save(username, "students", students)

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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    subjects, classes = _ensure_subjects(username)
    for c in classes:
        if c.get("id") == class_id:
            for s in c.get("time_slots", []):
                if s.get("id") == slot_id:
                    s["start_time"] = start_time.strip()
                    s["end_time"] = end_time.strip()
                    subject_info = next(
                        (item for item in subjects if item.get("id") == c.get("subject_id")),
                        None,
                    )
                    s["subject_id"] = c.get("subject_id")
                    s["subject"] = subject_info.get("name") if subject_info else c.get("class_name", "")
                    _save(username, "classes", classes)
                    return {"message": "កែប្រែម៉ោងសិក្សាបានជោគជ័យ", "data": s}
    raise HTTPException(404, "រកមិនឃើញម៉ោងសិក្សា")


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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    students = _load(username, "students", [])
    if class_id is not None:
        students = [s for s in students if s.get("class_id") == class_id]
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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    classes = _load(username, "classes", [])
    if not any(c.get("id") == class_id for c in classes):
        raise HTTPException(400, "ថ្នាក់រៀនមិនត្រឹមត្រូវ")

    students = _load(username, "students", [])
    new_id = _next_id(students)
    student = {
        "id": new_id,
        "student_name": student_name.strip(),
        "phone": clean_guardian_contact(phone),
        "class_id": class_id,
        "note": note.strip(),
        "created_at": datetime.now().isoformat(),
    }
    if slot_id is not None:
        student["slot_ids"] = [slot_id]
    students.append(student)
    _save(username, "students", students)
    return {"message": "បន្ថែមសិស្សបានជោគជ័យ", "data": student}


@router.post("/{username}/students/batch")
async def batch_create_students(
    request: Request,
    username: str,
):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    body = await request.json()
    new_students = body.get("students", [])
    if not isinstance(new_students, list) or not new_students:
        raise HTTPException(400, "មិនមានទិន្នន័យសិស្សសម្រាប់បញ្ចូលទេ")

    classes = _load(username, "classes", [])
    class_id_set = {c.get("id") for c in classes}
    class_name_map = {
        str(c.get("class_name", "")).strip().casefold(): c.get("id")
        for c in classes
    }
    default_class_id = classes[0]["id"] if classes else None

    students = _load(username, "students", [])
    created = []
    next_student_id = _next_id(students)

    for item in new_students:
        name = str(item.get("student_name", "")).strip()
        contact = clean_guardian_contact(str(item.get("phone", "")).strip())
        if not name:
            continue

        c_id = item.get("class_id")
        if c_id not in class_id_set:
            c_name = str(item.get("class_name", "")).strip().casefold()
            c_id = class_name_map.get(c_name, default_class_id)

        student_rec = {
            "id": next_student_id,
            "student_name": name,
            "phone": contact,
            "class_id": c_id,
            "note": str(item.get("note", "")).strip(),
            "created_at": datetime.now().isoformat(),
        }
        next_student_id += 1
        if item.get("slot_ids"):
            student_rec["slot_ids"] = item.get("slot_ids")

        students.append(student_rec)
        created.append(student_rec)

    if not created:
        raise HTTPException(400, "ពុំមានទិន្នន័យសិស្សត្រឹមត្រូវណាមួយត្រូវបានរកឃើញទេ")

    _save(username, "students", students)
    return {
        "message": f"បានបញ្ចូលសិស្សចំនួន {len(created)} នាក់ដោយជោគជ័យ",
        "count": len(created),
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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    classes = _load(username, "classes", [])
    if not any(class_info.get("id") == class_id for class_info in classes):
        raise HTTPException(400, "ថ្នាក់រៀនមិនត្រឹមត្រូវ")
    students = _load(username, "students", [])
    for s in students:
        if s.get("id") == student_id:
            s["student_name"] = student_name.strip()
            s["phone"] = clean_guardian_contact(phone)
            s["class_id"] = class_id
            s["note"] = note.strip()
            _save(username, "students", students)
            return {"message": "កែប្រែសិស្សបានជោគជ័យ", "data": s}
    raise HTTPException(404, "រកមិនឃើញសិស្ស")


@router.delete("/{username}/students/{student_id}")
async def delete_student(request: Request, username: str, student_id: int):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    students = _load(username, "students", [])
    students = [s for s in students if s.get("id") != student_id]
    _save(username, "students", students)
    return {"message": "លុបសិស្សបានជោគជ័យ"}


# ----------------------------------------------------------
# MESSAGE TEMPLATES
# ----------------------------------------------------------
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

@router.get("/{username}/templates")
async def list_templates(request: Request, username: str):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")
    templates = _load(username, "templates", DEFAULT_TEMPLATES) or DEFAULT_TEMPLATES
    return {"templates": templates}


@router.post("/{username}/templates")
async def create_template(
    request: Request,
    username: str,
    name: str = Form(...),
    content: str = Form(...),
):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    templates = _load(username, "templates", DEFAULT_TEMPLATES.copy())
    new_id = _next_id(templates)
    t = {
        "id": new_id,
        "name": name.strip(),
        "content": content.strip(),
        "is_default": False,
    }
    templates.append(t)
    _save(username, "templates", templates)
    return {"message": "បង្កើត template បានជោគជ័យ", "data": t}


@router.put("/{username}/templates/{template_id}")
async def update_template(
    request: Request,
    username: str,
    template_id: int,
    name: str = Form(...),
    content: str = Form(...),
):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    templates = _load(username, "templates", [])
    for t in templates:
        if t.get("id") == template_id:
            t["name"] = name.strip()
            t["content"] = content.strip()
            _save(username, "templates", templates)
            return {"message": "កែប្រែ template បានជោគជ័យ", "data": t}
    raise HTTPException(404, "រកមិនឃើញ template")


@router.delete("/{username}/templates/{template_id}")
async def delete_template(request: Request, username: str, template_id: int):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    templates = _load(username, "templates", [])
    templates = [t for t in templates if t.get("id") != template_id]
    _save(username, "templates", templates)
    return {"message": "លុប template បានជោគជ័យ"}


# ----------------------------------------------------------
# TELEGRAM CONFIG & SEND (TELETHON ENGINE)
# ----------------------------------------------------------
def _tg_config_path(username: str) -> Path:
    return _inst_dir(username) / "telegram.json"


def _load_tg(username: str) -> dict:
    storage_key = f"{username}:telegram"
    if is_supabase_enabled():
        val = load_data(storage_key, default=None)
        if val is not None and isinstance(val, dict):
            return val

    path = _tg_config_path(username)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if is_supabase_enabled() and data:
                    save_data(storage_key, data)
                return data
        except Exception:
            pass
    return {
        "api_id": "",
        "api_hash": "",
        "phone": "",
        "session_string": "",
        "connected": False,
        "account_name": "",
    }


def _save_tg(username: str, data: dict) -> None:
    storage_key = f"{username}:telegram"
    if is_supabase_enabled():
        save_data(storage_key, data)

    path = _tg_config_path(username)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


@router.get("/{username}/telegram/status")
async def tg_status(request: Request, username: str):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    cfg = _load_tg(username)
    cfg["api_id"] = api_id.strip()
    cfg["api_hash"] = api_hash.strip()
    cfg["phone"] = phone.strip()
    cfg["connected"] = False
    _save_tg(username, cfg)
    return {"message": "រក្សាទុកការកំណត់ Telegram បានជោគជ័យ"}


@router.post("/{username}/telegram/send_code")
async def tg_send_code(request: Request, username: str):
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    cfg = _load_tg(username)
    cfg["connected"] = False
    cfg["account_name"] = ""
    cfg.pop("_phone_code_hash", None)
    _save_tg(username, cfg)

    for p in SESSIONS_DIR.glob(f"{username}_tg*"):
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
    if require_inst(request) != username:
        raise HTTPException(403, "Forbidden")

    return await _send_messages(username, await request.json())


async def _send_messages(username: str, body: dict):
    cfg = _load_tg(username)
    if not cfg.get("connected"):
        raise HTTPException(400, "Telegram មិនទាន់ភ្ជាប់ទេ សូមភ្ជាប់ Telegram ជាមុនសិន")

    student_ids = body.get("student_ids", [])
    template_id = body.get("template_id")
    custom_message = body.get("custom_message")
    class_id = body.get("class_id")
    slot_id = body.get("slot_id")
    day = str(body.get("date") or datetime.now().strftime("%Y-%m-%d"))

    if not student_ids:
        raise HTTPException(400, "សូមជ្រើសរើសសិស្សយ៉ាងហោចណាស់ម្នាក់")

    classes = _load(username, "classes", [])
    students = _load(username, "students", [])

    requested_ids = set(student_ids)
    selected = [s for s in students if s.get("id") in requested_ids]

    if not selected:
        raise HTTPException(400, "រកមិនឃើញទិន្នន័យសិស្សដែលបានជ្រើស")

    message_tpl = custom_message
    if not message_tpl and template_id:
        templates = _load(username, "templates", DEFAULT_TEMPLATES)
        tpl = next((t for t in templates if str(t.get("id")) == str(template_id)), None)
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
