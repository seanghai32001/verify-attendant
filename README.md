# ប្រព័ន្ធគ្រប់គ្រងស្ថាប័ន — បំបែក Backend និង Frontend

សំណុំឯកសារនេះត្រូវបានបំបែកជាសេវាកម្មពីរដាច់ដោយឡែក៖

- **`backend/`** — FastAPI API, Login/Auth, ការគ្រប់គ្រងទិន្នន័យស្ថាប័ន/សិស្ស, Telegram, រូបភាព និងឯកសារទិន្នន័យ។
- **`frontend/`** — ទំព័រ HTML/Jinja ភាសាខ្មែរ និង UI server ដែលបញ្ជូន `/api/*` និង `/static/*` ទៅ Backend ដោយស្វ័យប្រវត្តិ។

## របៀបដាក់លើ Hosting ពីរ

### ១. ដាក់ Backend

Deploy ថត `backend/` ជា Python Web Service។

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

កំណត់ Environment Variables / Secrets នៅលើ hosting៖

- `ADMIN_USERNAME` — ឈ្មោះ Admin
- `ADMIN_PASSWORD` — ពាក្យសម្ងាត់ Admin ដែលខ្លាំង
- `INSTITUTION_PASSWORD_ENCRYPTION_KEY` — Fernet key ត្រឹមត្រូវ; ប្រសិនបើមាន password ដែលបាន encrypt រួច ត្រូវប្រើ key ចាស់ដដែល
- `DATA_DIR=/var/data/data`
- `SESSIONS_DIR=/var/data/sessions`
- `UPLOAD_DIR=/var/data/uploads`

ភ្ជាប់ **Persistent Disk** ទៅកាន់ `/var/data` ដើម្បីរក្សាទុក database, រូបភាព និង Telegram session កុំឱ្យបាត់ពេល redeploy។ សេវា Backend គួរប្រើ instance តែមួយ ព្រោះ login sessions នៅក្នុង memory។

Health check៖ `https://YOUR-BACKEND-URL/api/health`

### ២. ដាក់ Frontend

Deploy ថត `frontend/` ជា Python Web Service មួយទៀត ដាច់ពី Backend។

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

កំណត់ Environment Variable៖

```text
BACKEND_URL=https://YOUR-BACKEND-URL
```

`BACKEND_URL` ត្រូវជាអាសយដ្ឋានសាធារណៈរបស់ Backend ហើយកុំដាក់ `/` នៅខាងចុង។ បន្ទាប់មក អ្នកប្រើប្រាស់ចូលតាម **Frontend URL** មិនមែន Backend URL ទេ។

> ចំណាំ៖ Frontend នៅតែប្រើ Jinja templates ពីគម្រោងដើម ដូច្នេះវាជា Python Web Service មិនមែន static HTML សម្រាប់ដាក់លើ Vercel ដោយផ្ទាល់ទេ។ វាបំបែក UI server និង API server ដាច់ពីគ្នា ដោយមិនចាំបាច់សរសេរទំព័រទាំងអស់ឡើងវិញ។

## សុវត្ថិភាពឯកសារ

Backend ZIP រក្សាទុកទិន្នន័យដើមដែលមានព័ត៌មានសិស្ស/លេខទូរសព្ទ និង Telegram session credential ដើម្បីរក្សាដំណើរការដែលមានស្រាប់។ **កុំ upload ZIP នេះទៅ GitHub public ឬផ្ញើឱ្យអ្នកដទៃ**។ Frontend ZIP មិនមាន database ឬ Telegram session ទេ។

កូដ Backend ត្រូវបានកែឱ្យអាន `ADMIN_USERNAME` និង `ADMIN_PASSWORD` ពី Environment Variables ហើយមិនប្រើ default `admin/111` ទៀតទេ។ សូមកំណត់ secrets មុន deploy។
