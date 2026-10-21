"""
db_supabase.py
--------------
គ្រប់គ្រងការ Sync ទិន្នន័យរវាង Local JSON និង Supabase Cloud Database ដោយស្វ័យប្រវត្តិ
"""
from __future__ import annotations

import os
import httpx
from typing import Any

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}


def is_supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def load_data(key: str, default: Any = None) -> Any:
    """ទាញទិន្នន័យពី Supabase app_storage"""
    if not is_supabase_enabled():
        return default
    try:
        url = f"{SUPABASE_URL}/rest/v1/app_storage?key=eq.{key}&select=data"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=HEADERS)
            if resp.status_code == 200:
                rows = resp.json()
                if rows and len(rows) > 0:
                    return rows[0].get("data")
    except Exception as e:
        print(f"[Supabase] Error loading key '{key}': {e}")
    return default


def save_data(key: str, data: Any) -> None:
    """Save ឬ Update (Upsert) ទិន្នន័យចូល Supabase"""
    if not is_supabase_enabled():
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/app_storage"
        payload = [{"key": key, "data": data}]
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=HEADERS, json=payload)
            if resp.status_code not in (200, 201):
                print(f"[Supabase] Save error for '{key}': {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[Supabase] Error saving key '{key}': {e}")


def delete_prefix_data(prefix: str) -> None:
    """លុបទិន្នន័យទាំងអស់របស់ស្ថាប័នចេញពី Supabase ពេលលុបស្ថាប័ន"""
    if not is_supabase_enabled():
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/app_storage?key=like.{prefix}*"
        with httpx.Client(timeout=10.0) as client:
            client.delete(url, headers=HEADERS)
    except Exception as e:
        print(f"[Supabase] Error deleting keys with prefix '{prefix}': {e}")
