# Frontend UI — Institution Management

This is the separate UI server. It hosts the existing Khmer Jinja templates and forwards `/api/*` plus `/static/*` requests to the Backend API. This keeps the existing application pages and browser-side JavaScript working across two different hosting services.

Because the original UI uses Jinja-rendered templates, this folder must run as a **Python web service**; it is not a plain static Vercel export without a further frontend rewrite.

## Local run (Windows PowerShell)

Start Backend on port `8001` first, then in another terminal:

```powershell
cd frontend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:BACKEND_URL = "http://127.0.0.1:8001"
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000`.

## Hosting setup

Deploy this folder as a separate Python web service.

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Set `BACKEND_URL` to the public URL of the deployed Backend service, without a trailing slash. Example: `https://your-backend-service.onrender.com`.

The frontend does not contain student databases, Telegram session files, admin secrets, or the Fernet key. Browser requests are routed through this frontend service to the backend.
