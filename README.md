# Backend API — Institution Management

This folder is the API/data service. It does not serve HTML pages. The frontend service calls it via `BACKEND_URL` and proxies browser `/api/*` and `/static/*` requests.

## Local run (Windows PowerShell)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ADMIN_USERNAME = "your_admin_name"
$env:ADMIN_PASSWORD = "use_a_long_unique_password"
$env:INSTITUTION_PASSWORD_ENCRYPTION_KEY = "your_existing_fernet_key"
python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

Health check: `http://127.0.0.1:8001/api/health`

Uploaded raster images are resized to fit within 1200 × 1200 pixels and converted to WebP at quality 80 when that produces a smaller file. Animated GIFs and SVG images are kept unchanged. The backend installs Pillow from `requirements.txt`.

## Hosting setup

Deploy this folder as a Python web service. Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Set these environment variables in the hosting provider's Secrets/Environment section:

- `ADMIN_USERNAME` — admin login name.
- `ADMIN_PASSWORD` — strong admin password. There is no default `admin/111` login.
- `INSTITUTION_PASSWORD_ENCRYPTION_KEY` — valid Fernet key. Keep the original key if existing passwords were encrypted with it.
- For a persistent disk mounted at `/var/data`: `DATA_DIR=/var/data/data`, `SESSIONS_DIR=/var/data/sessions`, `UPLOAD_DIR=/var/data/uploads`.

The first run copies bundled data/session/upload files to the configured persistent directories only when the destination file does not already exist. Keep the persistent disk attached on redeploys. Use **one backend instance** unless sessions are moved to a shared store; login sessions currently live in memory.

## Security

- Do not upload this folder or its data/session files to a public Git repository.
- `data/` contains institution/student contact data and Telegram configuration. `sessions/*.session` contains Telegram authorization credentials; treat it like a password.
- Do not share `.env` values. Keep API credentials and encryption keys in hosting secrets, never in frontend code.
