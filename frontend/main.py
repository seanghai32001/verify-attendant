"""Frontend/UI server for the Institution/Class Management application.

This service hosts the Jinja templates and proxies /api and /static requests
through BACKEND_URL. Browser cookies are scoped to this frontend domain and
forwarded only server-to-server to the backend API.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import quote, parse_qs
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8001").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT_SECONDS", "120"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        app.state.backend_client = client
        yield


app = FastAPI(
    title="Institution Management Frontend",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def forwarded_headers(request: Request) -> dict[str, str]:
    excluded = {
        "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    }
    headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded}
    headers["x-forwarded-host"] = request.headers.get("host", "localhost")
    headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", request.url.scheme)
    return headers


async def backend_call(
    request: Request,
    path: str | None = None,
    method: str | None = None,
    body: bytes | None = None,
) -> httpx.Response:
    path = path or request.url.path
    target = f"{BACKEND_URL}{path}"
    query = request.url.query if path == request.url.path else ""
    if query:
        target = f"{target}?{query}"
    headers = forwarded_headers(request)
    try:
        client: httpx.AsyncClient = request.app.state.backend_client
        return await client.request(
            method or request.method,
            target,
            headers=headers,
            content=body if body is not None else (await request.body()),
        )
    except httpx.RequestError as exc:
        raise HTTPException(502, f"មិនអាចភ្ជាប់ទៅ Backend បានទេ។ សូមពិនិត្យ BACKEND_URL។ ({type(exc).__name__})") from None


def upstream_response(upstream: httpx.Response) -> Response:
    excluded = {
        "content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive",
        "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade", "set-cookie",
    }
    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
    response = Response(content=upstream.content, status_code=upstream.status_code, headers=headers)
    for cookie in upstream.headers.get_list("set-cookie"):
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response


def redirect_with_cookies(upstream: httpx.Response, target: str) -> RedirectResponse:
    response = RedirectResponse(url=target, status_code=303)
    for cookie in upstream.headers.get_list("set-cookie"):
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response


async def backend_json(request: Request, path: str) -> dict | list:
    response = await backend_call(request, path=path, method="GET", body=b"")
    if response.status_code >= 400:
        detail = "Backend request failed"
        try:
            payload = response.json()
            detail = payload.get("detail") or payload.get("error") or detail
        except ValueError:
            pass
        raise HTTPException(response.status_code, detail)
    try:
        return response.json()
    except ValueError:
        raise HTTPException(502, "Backend បានបញ្ជូនទិន្នន័យ JSON មិនត្រឹមត្រូវ") from None


async def get_state(request: Request) -> dict:
    value = await backend_json(request, "/api/ui/state")
    return value if isinstance(value, dict) else {}


async def get_institution(request: Request, username: str) -> dict:
    quoted = quote(username, safe="")
    value = await backend_json(request, f"/api/ui/institution/{quoted}")
    if not isinstance(value, dict):
        raise HTTPException(502, "ទិន្នន័យស្ថាប័នមិនត្រឹមត្រូវ")
    return value


def page_error(request: Request, code: int, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"code": code, "message": message},
        status_code=code,
    )


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    state = await get_state(request)
    if state.get("admin_user"):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login")
async def login(request: Request):
    upstream = await backend_call(request, path="/api/auth/admin/login", method="POST", body=await request.body())
    if upstream.status_code == 200:
        return redirect_with_cookies(upstream, "/dashboard")
    message = "ព័ត៌មានមិនត្រឹមត្រូវ! សូមព្យាយាមម្ដងទៀត។"
    try:
        payload = upstream.json()
        message = payload.get("detail") or payload.get("error") or message
    except ValueError:
        pass
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": message}, status_code=upstream.status_code
    )


@app.get("/logout")
async def logout(request: Request):
    upstream = await backend_call(request, path="/api/auth/admin/logout", method="POST", body=b"")
    return redirect_with_cookies(upstream, "/")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    state = await get_state(request)
    if not state.get("admin_user"):
        return RedirectResponse("/", status_code=303)
    institutions = await backend_json(request, "/api/ui/institutions")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"institutions": institutions, "base_url": str(request.base_url).rstrip("/")},
    )


@app.get("/manage/{username}", response_class=HTMLResponse)
async def manage_entry(request: Request, username: str, next: str | None = None):
    inst, state = await asyncio.gather(
        get_institution(request, username),
        get_state(request),
    )
    if state.get("institution_username") == username:
        # The backend applies an allow-list to `next` during login; apply the same
        # safe choices here so a URL cannot become an open redirect.
        target = f"/manage/{username}/mobile-students" if next == f"/manage/{username}/mobile-students" else f"/manage/{username}/dashboard"
        if next and next.startswith(f"/manage/{username}/mobile-students?class_id="):
            value = next.split("class_id=", 1)[1]
            if value.isdigit():
                target = f"/manage/{username}/mobile-students?class_id={value}"
        return RedirectResponse(target, status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="institution_login.html",
        context={"inst": inst, "username": username, "error": None, "next": next or f"/manage/{username}/dashboard"},
    )


@app.post("/manage/{username}/login")
async def manage_login(request: Request, username: str):
    raw_body = await request.body()
    try:
        fields = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        fields = {}
    submitted_user = (fields.get("inst_username") or [""])[0].strip()
    if submitted_user != username:
        inst = await get_institution(request, username)
        return templates.TemplateResponse(
            request=request,
            name="institution_login.html",
            context={"inst": inst, "username": username, "error": "ឈ្មោះអ្នកប្រើប្រាស់មិនត្រឹមត្រូវ!", "next": f"/manage/{username}/dashboard"},
            status_code=401,
        )
    upstream = await backend_call(request, path="/api/auth/institution/login", method="POST", body=raw_body)
    if upstream.status_code == 200:
        try:
            next_path = upstream.json().get("next_path") or f"/manage/{username}/dashboard"
        except ValueError:
            next_path = f"/manage/{username}/dashboard"
        return redirect_with_cookies(upstream, next_path)
    inst = await get_institution(request, username)
    message = "ពាក្យសម្ងាត់មិនត្រឹមត្រូវ! សូមព្យាយាមម្ដងទៀត។"
    try:
        payload = upstream.json()
        message = payload.get("detail") or payload.get("error") or message
    except ValueError:
        pass
    next_path = (fields.get("next") or [f"/manage/{username}/dashboard"])[0]
    return templates.TemplateResponse(
        request=request,
        name="institution_login.html",
        context={"inst": inst, "username": username, "error": message, "next": next_path},
        status_code=upstream.status_code,
    )


@app.get("/public/{username}", response_class=HTMLResponse)
async def public_dashboard(request: Request, username: str):
    inst = await get_institution(request, username)
    return templates.TemplateResponse(request=request, name="User_Moble/UserDas.html", context={"inst": inst})


@app.get("/public/{username}/mobile-students", response_class=HTMLResponse)
async def public_mobile_students(request: Request, username: str):
    inst = await get_institution(request, username)
    return templates.TemplateResponse(
        request=request,
        name="User_Moble/Stude.html",
        context={"inst": inst, "public_mode": True},
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def require_institution_page(request: Request, username: str) -> dict | RedirectResponse:
    state = await get_state(request)
    if state.get("institution_username") != username:
        return RedirectResponse(f"/manage/{username}", status_code=303)
    return await get_institution(request, username)


@app.get("/manage/{username}/dashboard", response_class=HTMLResponse)
async def manage_dashboard(request: Request, username: str):
    inst = await require_institution_page(request, username)
    if isinstance(inst, RedirectResponse):
        return inst
    return templates.TemplateResponse(request=request, name="manage_dashboard.html", context={"inst": inst})


@app.get("/manage/{username}/students", response_class=HTMLResponse)
async def manage_students(request: Request, username: str):
    inst = await require_institution_page(request, username)
    if isinstance(inst, RedirectResponse):
        return inst
    return templates.TemplateResponse(
        request=request,
        name="students.html",
        context={"inst": inst, "mobile_students_link": f"{str(request.base_url).rstrip('/')}/manage/{username}/mobile-students"},
    )


@app.get("/manage/{username}/mobile-students/qr.svg")
async def mobile_students_qr(request: Request, username: str):
    upstream = await backend_call(
        request,
        path=f"/api/ui/manage/{quote(username, safe='')}/mobile-students/qr.svg",
        method="GET",
        body=b"",
    )
    return upstream_response(upstream)


@app.get("/manage/{username}/mobile-students", response_class=HTMLResponse)
async def manage_mobile_students(request: Request, username: str):
    inst = await require_institution_page(request, username)
    if isinstance(inst, RedirectResponse):
        return inst
    return templates.TemplateResponse(request=request, name="User_Moble/Stude.html", context={"inst": inst})


@app.get("/manage/{username}/telegram", response_class=HTMLResponse)
async def manage_telegram(request: Request, username: str):
    inst = await require_institution_page(request, username)
    if isinstance(inst, RedirectResponse):
        return inst
    return templates.TemplateResponse(request=request, name="telegram.html", context={"inst": inst})


@app.get("/manage/{username}/logout")
async def manage_logout(request: Request, username: str):
    upstream = await backend_call(
        request,
        path=f"/api/auth/institution/logout/{quote(username, safe='')}",
        method="POST",
        body=b"",
    )
    return redirect_with_cookies(upstream, f"/manage/{username}")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_api(request: Request, path: str):
    upstream = await backend_call(request)
    return upstream_response(upstream)


@app.api_route("/static/{path:path}", methods=["GET", "HEAD"])
async def proxy_static(request: Request, path: str):
    upstream = await backend_call(request)
    return upstream_response(upstream)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
        return Response(
            content=(__import__("json").dumps({"detail": exc.detail}, ensure_ascii=False)),
            status_code=exc.status_code,
            media_type="application/json",
        )
    return page_error(request, exc.status_code, str(exc.detail))
