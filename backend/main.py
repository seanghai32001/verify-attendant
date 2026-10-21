"""API backend for the split Institution/Class Management application.

Run with: uvicorn backend:app --host 0.0.0.0 --port ${PORT:-8001}
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import qrcode
import qrcode.image.svg
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from cryptography.fernet import Fernet, InvalidToken

import class_manager as class_manager_module
from class_manager import (
    configure as configure_class_manager,
    delete_institution_data,
    get_public_dashboard_data,
    public_router,
    router as class_router,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "static" / "uploads"))).resolve()
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", str(BASE_DIR / "sessions"))).resolve()
DB_FILE = DATA_DIR / "institutions.json"


def seed_missing_files(source: Path, destination: Path) -> None:
    """Copy bundled starting data to an empty external persistent-disk path only once."""
    if not source.exists() or source.resolve() == destination.resolve():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for src in source.rglob("*"):
        relative = src.relative_to(source)
        dst = destination / relative
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


seed_missing_files(BASE_DIR / "data", DATA_DIR)
seed_missing_files(BASE_DIR / "sessions", SESSIONS_DIR)
seed_missing_files(BASE_DIR / "static" / "uploads", UPLOAD_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Institution Management Backend",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
INSTITUTION_PASSWORD_ENCRYPTION_KEY = os.environ.get(
    "INSTITUTION_PASSWORD_ENCRYPTION_KEY", ""
).strip()

SESSION_HOURS = 8
INST_SESSION_HOURS = 4
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1200
MAX_IMAGE_PIXELS = 40_000_000
SESSIONS: dict[str, dict] = {}
INST_SESSIONS: dict[str, dict] = {}


def get_base_url(request: Request) -> str:
    """Use the public frontend host forwarded by the UI service."""
    forwarded_host = request.headers.get("x-forwarded-host")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_host:
        host = forwarded_host.split(",")[0].strip()
        scheme = (forwarded_proto or request.url.scheme or "http").split(",")[0].strip()
    else:
        host = request.headers.get("host", "127.0.0.1:8000")
        scheme = request.url.scheme
    return f"{scheme}://{host}"


def get_manage_next_path(username: str, requested_path: str | None) -> str:
    mobile_path = f"/manage/{username}/mobile-students"
    if requested_path == mobile_path:
        return mobile_path
    prefix = f"{mobile_path}?class_id="
    if requested_path and requested_path.startswith(prefix):
        class_id = requested_path[len(prefix):]
        if class_id.isdigit():
            return f"{prefix}{class_id}"
    return f"/manage/{username}/dashboard"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def encrypt_institution_password(password: str) -> str:
    if not INSTITUTION_PASSWORD_ENCRYPTION_KEY:
        raise HTTPException(
            503, "សូមកំណត់ INSTITUTION_PASSWORD_ENCRYPTION_KEY នៅក្នុង Environment Variables"
        )
    try:
        cipher = Fernet(INSTITUTION_PASSWORD_ENCRYPTION_KEY.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise HTTPException(503, "INSTITUTION_PASSWORD_ENCRYPTION_KEY មិនត្រឹមត្រូវ") from None
    return cipher.encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_institution_password(encrypted_password: str) -> str | None:
    if not INSTITUTION_PASSWORD_ENCRYPTION_KEY:
        return None
    try:
        cipher = Fernet(INSTITUTION_PASSWORD_ENCRYPTION_KEY.encode("ascii"))
        return cipher.decrypt(encrypted_password.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeEncodeError):
        return None


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """Return (valid, legacy_plaintext_password)."""
    if not isinstance(stored, str):
        return False, False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iterations_text, salt_text, expected = stored.split("$", 3)
            iterations = int(iterations_text)
            if not 100_000 <= iterations <= 1_000_000:
                return False, False
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_text), iterations
            ).hex()
            return hmac.compare_digest(actual, expected), False
        except (ValueError, TypeError):
            return False, False
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8")), True


def is_secure_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded_proto.split(",")[0].strip() or request.url.scheme
    return scheme.lower() == "https"


def load_db() -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        for item in data:
            item.pop("management_link", None)
            item.pop("details", None)
        return data
    except (json.JSONDecodeError, OSError):
        return []


def save_db() -> None:
    tmp = DB_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(institutions_db, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_FILE)


def create_session(user: str) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user": user, "expires": datetime.now() + timedelta(hours=SESSION_HOURS)}
    return token


def get_session_user(token: str | None) -> str | None:
    if not token:
        return None
    session = SESSIONS.get(token)
    if not session:
        return None
    if session["expires"] < datetime.now():
        SESSIONS.pop(token, None)
        return None
    return session["user"]


def get_current_user(request: Request) -> str | None:
    return get_session_user(request.cookies.get("session_token"))


def require_auth(request: Request) -> None:
    if not get_current_user(request):
        raise HTTPException(401, "Unauthorized")


def create_inst_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    INST_SESSIONS[token] = {
        "username": username,
        "expires": datetime.now() + timedelta(hours=INST_SESSION_HOURS),
    }
    return token


def get_inst_session(token: str | None) -> str | None:
    if not token:
        return None
    session = INST_SESSIONS.get(token)
    if not session:
        return None
    if session["expires"] < datetime.now():
        INST_SESSIONS.pop(token, None)
        return None
    return session["username"]


def get_current_inst_username(request: Request) -> str | None:
    return get_inst_session(request.cookies.get("inst_session_token"))


def get_institution_by_username(username: str) -> dict | None:
    return next((i for i in institutions_db if i.get("inst_username") == username), None)


def enrich_institution(inst: dict, base_url: str = "") -> dict:
    """Enrich institution with decrypted password and relative paths for dynamic origin."""
    enriched = inst.copy()
    encrypted_password = enriched.pop("inst_password_encrypted", None)
    if encrypted_password:
        enriched["inst_password"] = decrypt_institution_password(encrypted_password) or ""
    elif str(enriched.get("inst_password", "")).startswith("pbkdf2_sha256$"):
        enriched["inst_password"] = ""
    username = inst.get("inst_username", "")
    
    # ប្រើប្រាស់ Relative Paths ដើម្បីឱ្យ Browser យក Domain បច្ចុប្បន្នជានិច្ច
    enriched["management_link"] = f"/manage/{username}"
    enriched["public_dashboard_link"] = f"/public/{username}"
    return enriched


async def save_upload(file: UploadFile | None, prefix: str) -> str | None:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "ប្រភេទឯកសារមិនត្រឹមត្រូវ")
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, "ឯកសារធំពេក (អតិបរមា 5MB)")
    content, output_ext = await asyncio.to_thread(optimize_uploaded_image, content, ext)

    filename = f"{prefix}_{secrets.token_hex(6)}{output_ext}"
    (UPLOAD_DIR / filename).write_bytes(content)
    return f"/static/uploads/{filename}"


def optimize_uploaded_image(content: bytes, ext: str) -> tuple[bytes, str]:
    if ext == ".svg":
        return content, ext
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise HTTPException(400, "រូបភាពមានទំហំធំពេក")
            if image.format == "GIF" and getattr(image, "is_animated", False):
                return content, ext

            image = ImageOps.exif_transpose(image)
            image.thumbnail(
                (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=80, method=6)
            webp_content = output.getvalue()
            if len(webp_content) < len(content):
                return webp_content, ".webp"
            return content, ext
    except HTTPException:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(400, "ឯកសាររូបភាពមិនត្រឹមត្រូវ") from None


def delete_old_upload(url: str | None) -> None:
    if not url or not url.startswith("/static/uploads/"):
        return
    filepath = BASE_DIR / url.lstrip("/")
    try:
        if filepath.exists() and filepath.is_file():
            filepath.unlink()
    except OSError:
        pass


def get_next_id() -> int:
    return max((inst.get("id", 0) for inst in institutions_db), default=0) + 1


institutions_db = load_db()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "backend-api"}


@app.get("/static/uploads/{filename}")
async def uploaded_file(filename: str):
    if Path(filename).name != filename or filename in {".", ".."}:
        raise HTTPException(400, "ឈ្មោះឯកសារមិនត្រឹមត្រូវ")
    file_path = UPLOAD_DIR / filename
    if not file_path.is_file():
        raise HTTPException(404, "រកមិនឃើញរូបភាព")
    return FileResponse(
        file_path,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/ui/state")
async def ui_state(request: Request):
    return {
        "admin_user": get_current_user(request),
        "institution_username": get_current_inst_username(request),
    }


@app.get("/api/ui/institutions")
async def list_institutions_for_admin(request: Request):
    require_auth(request)
    return [enrich_institution(inst, get_base_url(request)) for inst in institutions_db]


@app.get("/api/ui/institution/{username}")
async def get_public_institution(username: str):
    inst = get_institution_by_username(username)
    if not inst:
        raise HTTPException(404, "រកមិនឃើញស្ថាប័ននេះទេ")
    return {
        "institution_name": inst.get("institution_name", ""),
        "teacher_name": inst.get("teacher_name", ""),
        "profile_image_url": inst.get("profile_image_url"),
        "inst_username": username,
    }


@app.get("/api/public/{username}/dashboard")
async def public_dashboard_data_route(username: str):
    if not get_institution_by_username(username):
        raise HTTPException(404, "រកមិនឃើញស្ថាប័ននេះទេ")
    return get_public_dashboard_data(username)


@app.post("/api/auth/admin/login")
async def admin_login_api(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise HTTPException(503, "សូមកំណត់ ADMIN_USERNAME និង ADMIN_PASSWORD នៅក្នុង Environment Variables")
    if not (
        hmac.compare_digest(username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
        and hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
    ):
        raise HTTPException(401, "ព័ត៌មានមិនត្រឹមត្រូវ! សូមព្យាយាមម្ដងទៀត។")
    token = create_session(username)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        key="session_token", value=token, httponly=True, samesite="lax",
        secure=is_secure_request(request), max_age=SESSION_HOURS * 3600, path="/"
    )
    return response


@app.post("/api/auth/institution/login")
async def institution_login_api(
    request: Request,
    inst_username: str = Form(...),
    inst_password: str = Form(...),
    next: str | None = Form(None),
):
    username = inst_username.strip()
    inst = get_institution_by_username(username)
    if not inst:
        raise HTTPException(401, "ឈ្មោះអ្នកប្រើប្រាស់មិនត្រឹមត្រូវ!")
    valid_password, legacy_password = verify_password(inst_password, inst.get("inst_password", ""))
    if not valid_password:
        raise HTTPException(401, "ពាក្យសម្ងាត់មិនត្រឹមត្រូវ! សូមព្យាយាមម្ដងទៀត។")
    if legacy_password and INSTITUTION_PASSWORD_ENCRYPTION_KEY:
        inst["inst_password_encrypted"] = encrypt_institution_password(inst_password)
        inst["inst_password"] = hash_password(inst_password)
        save_db()
    token = create_inst_session(username)
    response = JSONResponse({"ok": True, "next_path": get_manage_next_path(username, next)})
    response.set_cookie(
        key="inst_session_token", value=token, httponly=True, samesite="lax",
        secure=is_secure_request(request), max_age=INST_SESSION_HOURS * 3600, path="/"
    )
    return response


@app.post("/api/auth/admin/logout")
async def admin_logout_api(request: Request):
    token = request.cookies.get("session_token")
    if token:
        SESSIONS.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_token", path="/")
    return response


@app.post("/api/auth/institution/logout/{username}")
async def institution_logout_api(request: Request, username: str):
    token = request.cookies.get("inst_session_token")
    if token:
        INST_SESSIONS.pop(token, None)
    response = JSONResponse({"ok": True, "username": username})
    response.delete_cookie("inst_session_token", path="/")
    return response


@app.get("/api/ui/manage/{username}/mobile-students/qr.svg")
async def mobile_students_qr_api(request: Request, username: str):
    if get_current_inst_username(request) != username:
        raise HTTPException(401, "Unauthorized")
    if not get_institution_by_username(username):
        raise HTTPException(404, "រកមិនឃើញស្ថាប័ន")
    public_url = f"{get_base_url(request)}/manage/{username}/mobile-students"
    img = qrcode.make(public_url, image_factory=qrcode.image.svg.SvgPathImage)
    output = __import__("io").BytesIO()
    img.save(output)
    return Response(content=output.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/api/institutions")
async def create_institution(
    request: Request,
    institution_name: str = Form(...),
    teacher_name: str = Form(...),
    inst_username: str = Form(...),
    inst_password: str = Form(...),
    profile_image: UploadFile | None = File(None),
):
    require_auth(request)
    inst_username = inst_username.strip()
    if not inst_username:
        return JSONResponse(status_code=400, content={"error": "សូមបញ្ចូលឈ្មោះអ្នកប្រើប្រាស់"})
    if any(i.get("inst_username") == inst_username for i in institutions_db):
        return JSONResponse(status_code=400, content={"error": "Username នេះមានរួចហើយ"})
    clean_password = inst_password.strip()
    if not clean_password:
        return JSONResponse(status_code=400, content={"error": "សូមបញ្ចូលពាក្យសម្ងាត់"})
    encrypted_password = encrypt_institution_password(clean_password)
    new_id = get_next_id()
    image_url = await save_upload(profile_image, f"inst_{new_id}")
    new_inst = {
        "id": new_id,
        "institution_name": institution_name.strip(),
        "teacher_name": teacher_name.strip(),
        "inst_username": inst_username,
        "inst_password": hash_password(clean_password),
        "inst_password_encrypted": encrypted_password,
        "profile_image_url": image_url,
        "created_at": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    }
    institutions_db.append(new_inst)
    save_db()
    return {"message": "បង្កើតស្ថាប័នបានជោគជ័យ!", "data": enrich_institution(new_inst, get_base_url(request))}


@app.put("/api/institutions/{inst_id}")
async def update_institution(
    request: Request,
    inst_id: int,
    institution_name: str = Form(...),
    teacher_name: str = Form(...),
    inst_username: str = Form(...),
    inst_password: str = Form(""),
    profile_image: UploadFile | None = File(None),
):
    require_auth(request)
    inst_username = inst_username.strip()
    if any(i.get("inst_username") == inst_username and i.get("id") != inst_id for i in institutions_db):
        return JSONResponse(status_code=400, content={"error": "Username នេះមានរួចហើយ"})
    for inst in institutions_db:
        if inst.get("id") == inst_id:
            new_password = inst_password.strip()
            encrypted_password = encrypt_institution_password(new_password) if new_password else None
            inst["institution_name"] = institution_name.strip()
            inst["teacher_name"] = teacher_name.strip()
            inst["inst_username"] = inst_username
            if new_password:
                inst["inst_password"] = hash_password(new_password)
                inst["inst_password_encrypted"] = encrypted_password
            image_url = await save_upload(profile_image, f"inst_{inst_id}")
            if image_url:
                delete_old_upload(inst.get("profile_image_url"))
                inst["profile_image_url"] = image_url
            save_db()
            return {"message": "កែប្រែបានជោគជ័យ", "data": enrich_institution(inst, get_base_url(request))}
    return JSONResponse(status_code=404, content={"error": "រកមិនឃើញស្ថាប័ន"})


@app.delete("/api/institutions/{inst_id}")
async def delete_institution(request: Request, inst_id: int, password: str = Form(...)):
    require_auth(request)
    if not ADMIN_PASSWORD or not hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
        return JSONResponse(status_code=401, content={"error": "ពាក្យសម្ងាត់មិនត្រឹមត្រូវ"})
    target = next((i for i in institutions_db if i.get("id") == inst_id), None)
    if not target:
        return JSONResponse(status_code=404, content={"error": "រកមិនឃើញស្ថាប័ន"})
    username = target.get("inst_username", "")
    try:
        if username:
            await delete_institution_data(username)
    except (OSError, RuntimeError):
        return JSONResponse(status_code=500, content={"error": "មិនអាចលុបទិន្នន័យទាំងអស់របស់ស្ថាប័នបានទេ"})
    delete_old_upload(target.get("profile_image_url"))
    institutions_db.remove(target)
    save_db()
    for token, session in list(INST_SESSIONS.items()):
        if session.get("username") == username:
            INST_SESSIONS.pop(token, None)
    return {"message": "លុបបានជោគជ័យ"}


@app.exception_handler(404)
async def api_404_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=404, content={"error": exc.detail or "Not found"})


def verify_inst_password(username: str, password: str) -> bool:
    institution = get_institution_by_username(username)
    return institution is not None and verify_password(password, institution.get("inst_password", ""))[0]


configure_class_manager(get_current_inst_username, verify_inst_password)

app.include_router(class_router)
app.include_router(public_router)