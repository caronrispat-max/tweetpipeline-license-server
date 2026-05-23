from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from requests.auth import HTTPBasicAuth

app = FastAPI(title="Tweet Pipeline Pro License Server - Final Supabase")

# ---------------------------------------------------------------------
# Environment / defaults
# ---------------------------------------------------------------------
DEFAULT_ADMIN_TOKEN = os.getenv(
    "TPP_SERVER_ADMIN_TOKEN",
    os.getenv("LICENSE_SERVER_ADMIN_TOKEN", "change-this-admin-token"),
)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

ENV_DEFAULTS: Dict[str, str] = {
    "DARAJA_ENV": os.getenv("DARAJA_ENV", "sandbox"),
    "DARAJA_CONSUMER_KEY": os.getenv("DARAJA_CONSUMER_KEY", ""),
    "DARAJA_CONSUMER_SECRET": os.getenv("DARAJA_CONSUMER_SECRET", ""),
    "DARAJA_SHORTCODE": os.getenv("DARAJA_SHORTCODE", "174379"),
    "DARAJA_PARTYB": os.getenv("DARAJA_PARTYB", os.getenv("DARAJA_SHORTCODE", "174379")),
    "DARAJA_PASSKEY": os.getenv("DARAJA_PASSKEY", ""),
    "DARAJA_CALLBACK_URL": os.getenv("CALLBACK_URL", os.getenv("DARAJA_CALLBACK_URL", "")),
    "M_PESA_TRANSACTION_TYPE": os.getenv("M_PESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
    "WEEKLY_PRICE": os.getenv("WEEKLY_PRICE", "500"),
    "MONTHLY_PRICE": os.getenv("MONTHLY_PRICE", os.getenv("DEFAULT_AMOUNT", "1500")),
    "LIFETIME_PRICE": os.getenv("LIFETIME_PRICE", "15000"),
    "LICENSE_DAYS_WEEKLY": os.getenv("LICENSE_DAYS_WEEKLY", "7"),
    "LICENSE_DAYS_MONTHLY": os.getenv("LICENSE_DAYS", os.getenv("LICENSE_DAYS_MONTHLY", "30")),
    "LICENSE_DAYS_LIFETIME": os.getenv("LICENSE_DAYS_LIFETIME", "36500"),
    "SERVER_ADMIN_TOKEN": DEFAULT_ADMIN_TOKEN,
    "ACCOUNT_REFERENCE": os.getenv("ACCOUNT_REFERENCE", "TweetPro"),
    "TRANSACTION_DESC": os.getenv("TRANSACTION_DESC", "License"),
    "ENFORCE_DEVICE_LOCK": os.getenv("ENFORCE_DEVICE_LOCK", "1"),
    "ENFORCE_IP_LOCK": os.getenv("ENFORCE_IP_LOCK", "1"),
    "ALLOW_SANDBOX_SUCCESS_TEXT_AS_PAID": os.getenv("ALLOW_SANDBOX_SUCCESS_TEXT_AS_PAID", "0"),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
    "TELEGRAM_APPROVAL_SECRET": os.getenv("TELEGRAM_APPROVAL_SECRET", DEFAULT_ADMIN_TOKEN),
    "MANUAL_APPROVAL_ENABLED": os.getenv("MANUAL_APPROVAL_ENABLED", "1"),
}

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class PaymentStartRequest(BaseModel):
    phone: str
    plan: str = "monthly"
    amount: Optional[int] = None
    device_id: Optional[str] = None


class ManualPaymentRequest(BaseModel):
    phone: str
    plan: str = "monthly"
    amount: Optional[int] = None
    device_id: Optional[str] = None
    message: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str
    device_id: Optional[str] = None


class LicenseCheckRequest(BaseModel):
    token: str
    device_id: Optional[str] = None


# ---------------------------------------------------------------------
# Base helpers
# ---------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or ""
    return ""


def require_supabase() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in Vercel environment variables.",
        )


def sb_headers(prefer: Optional[str] = None) -> Dict[str, str]:
    require_supabase()
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def sb_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def sb_select(table: str, params: Dict[str, str]) -> list[dict[str, Any]]:
    response = requests.get(sb_url(table), headers=sb_headers(), params=params, timeout=30)
    if response.status_code >= 400:
        raise HTTPException(status_code=500, detail={"supabase_select_error": response.text, "table": table})
    return response.json()


def sb_insert(table: str, payload: Dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.post(sb_url(table), headers=sb_headers("return=representation"), json=payload, timeout=30)
    if response.status_code >= 400:
        raise HTTPException(status_code=500, detail={"supabase_insert_error": response.text, "table": table, "payload": payload})
    return response.json()


def sb_update(table: str, filters: Dict[str, str], payload: Dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.patch(sb_url(table), headers=sb_headers("return=representation"), params=filters, json=payload, timeout=30)
    if response.status_code >= 400:
        raise HTTPException(status_code=500, detail={"supabase_update_error": response.text, "table": table, "payload": payload})
    return response.json()


def sb_upsert(table: str, payload: Dict[str, Any], on_conflict: str) -> list[dict[str, Any]]:
    response = requests.post(
        sb_url(table),
        headers=sb_headers("resolution=merge-duplicates,return=representation"),
        params={"on_conflict": on_conflict},
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=500, detail={"supabase_upsert_error": response.text, "table": table, "payload": payload})
    return response.json()


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------
def setting(key: str, default: str = "") -> str:
    fallback = ENV_DEFAULTS.get(key, os.getenv(key, default))
    try:
        rows = sb_select("settings", {"select": "value", "key": f"eq.{key}", "limit": "1"})
        if rows:
            value = rows[0].get("value", fallback)
            # Empty Supabase settings should not accidentally erase env secrets.
            if value in (None, "") and fallback not in (None, ""):
                return str(fallback)
            return str(value)
    except HTTPException:
        pass
    return str(fallback if fallback is not None else default)


def set_setting(key: str, value: Any) -> None:
    sb_upsert("settings", {"key": key, "value": str(value), "updated_at": utc_now()}, "key")


def all_settings(mask_secret: bool = False) -> Dict[str, str]:
    result = dict(ENV_DEFAULTS)
    try:
        rows = sb_select("settings", {"select": "key,value", "order": "key.asc"})
        for row in rows:
            k = str(row["key"])
            v = str(row.get("value", ""))
            if v or not result.get(k):
                result[k] = v
    except HTTPException:
        pass
    if mask_secret:
        for k in list(result):
            if any(s in k for s in ["SECRET", "PASSKEY", "TOKEN", "KEY"]):
                v = result[k]
                result[k] = "" if not v else (v[:4] + "..." + v[-4:] if len(v) > 10 else "********")
    return result


def ensure_default_settings() -> None:
    for key, value in ENV_DEFAULTS.items():
        try:
            rows = sb_select("settings", {"select": "key", "key": f"eq.{key}", "limit": "1"})
            if not rows:
                set_setting(key, value)
        except HTTPException:
            return


def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    token = setting("SERVER_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)
    if not x_admin_token or not secrets.compare_digest(str(x_admin_token), str(token)):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def normalize_phone(phone: str) -> str:
    phone = (phone or "").strip().replace(" ", "").replace("+", "")
    if phone.startswith("07") and len(phone) == 10:
        return "254" + phone[1:]
    if phone.startswith("01") and len(phone) == 10:
        return "254" + phone[1:]
    if phone.startswith("254") and len(phone) == 12 and phone.isdigit():
        return phone
    raise HTTPException(status_code=400, detail="Invalid phone number. Use 2547XXXXXXXX or 07XXXXXXXX.")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 220_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        _method, salt, digest = stored_hash.split("$", 2)
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 220_000).hex()
        return secrets.compare_digest(check, digest)
    except Exception:
        return False


def generate_temp_password(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    raw = "".join(secrets.choice(alphabet) for _ in range(length))
    return raw[:4] + "-" + raw[4:8] + "-" + raw[8:]


def plan_amount(plan: str) -> int:
    plan = (plan or "monthly").lower()
    if plan == "weekly":
        return int(float(setting("WEEKLY_PRICE", "500") or 500))
    if plan == "lifetime":
        return int(float(setting("LIFETIME_PRICE", "15000") or 15000))
    return int(float(setting("MONTHLY_PRICE", "1500") or 1500))


def plan_days(plan: str) -> int:
    plan = (plan or "monthly").lower()
    if plan == "weekly":
        return int(float(setting("LICENSE_DAYS_WEEKLY", "7") or 7))
    if plan == "lifetime":
        return int(float(setting("LICENSE_DAYS_LIFETIME", "36500") or 36500))
    return int(float(setting("LICENSE_DAYS_MONTHLY", "30") or 30))


# ---------------------------------------------------------------------
# Daraja / M-Pesa
# ---------------------------------------------------------------------
def token_url() -> str:
    if setting("DARAJA_ENV", "sandbox").lower() == "production":
        return "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    return "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"


def stk_url() -> str:
    if setting("DARAJA_ENV", "sandbox").lower() == "production":
        return "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    return "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"


def get_access_token() -> str:
    key = setting("DARAJA_CONSUMER_KEY")
    secret = setting("DARAJA_CONSUMER_SECRET")
    if not key or not secret:
        raise HTTPException(status_code=400, detail="Daraja Consumer Key/Secret not configured on server admin page.")
    response = requests.get(token_url(), auth=HTTPBasicAuth(key, secret), timeout=30)
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if response.status_code != 200 or not data.get("access_token"):
        raise HTTPException(status_code=500, detail={"message": "Failed to get Daraja token", "response": data})
    return data["access_token"]


def build_stk_password(timestamp: str) -> str:
    shortcode = setting("DARAJA_SHORTCODE", "174379")
    passkey = setting("DARAJA_PASSKEY")
    if not passkey:
        raise HTTPException(status_code=400, detail="Daraja Passkey not configured on server admin page.")
    raw = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


# ---------------------------------------------------------------------
# Users/licenses
# ---------------------------------------------------------------------
def create_or_update_paid_user(phone: str, plan: str, device_id: str = "", ip_address: str = "") -> Dict[str, str]:
    username = f"user{phone}"
    temp_password = generate_temp_password()
    password_hash = hash_password(temp_password)
    days = plan_days(plan)
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)
    rows = sb_upsert(
        "users",
        {
            "username": username,
            "phone": phone,
            "password_hash": password_hash,
            "status": "active",
            "plan": plan,
            "license_expiry": expiry,
            "device_id": device_id or "",
            "ip_address": ip_address or "",
            "token": token,
            "must_change_password": True,
            "created_at": utc_now(),
            "last_login": None,
        },
        "username",
    )
    return {"username": username, "temporary_password": temp_password, "license_expiry": expiry, "token": token}


def find_payment_by_checkout(checkout_id: str) -> Optional[dict[str, Any]]:
    rows = sb_select("payments", {"select": "*", "checkout_request_id": f"eq.{checkout_id}", "limit": "1"})
    return rows[0] if rows else None


# ---------------------------------------------------------------------
# Telegram/manual approval
# ---------------------------------------------------------------------
def telegram_configured() -> bool:
    return bool(setting("TELEGRAM_BOT_TOKEN", "").strip() and setting("TELEGRAM_CHAT_ID", "").strip())


def send_telegram_result(text: str) -> Dict[str, Any]:
    token = setting("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = setting("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is empty."}
    if not chat_id:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID is empty."}
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        if resp.status_code >= 400 or not data.get("ok", False):
            return {"ok": False, "status_code": resp.status_code, "error": data.get("description") or data}
        return {"ok": True, "status_code": resp.status_code, "response": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_telegram(text: str) -> bool:
    return bool(send_telegram_result(text).get("ok"))


def make_manual_links(request_id: str) -> tuple[str, str]:
    base = setting("PUBLIC_SERVER_URL", "") or os.getenv("VERCEL_URL", "")
    if base and not base.startswith("http"):
        base = "https://" + base
    if not base:
        callback = setting("DARAJA_CALLBACK_URL", "")
        base = callback.replace("/api/mpesa/callback", "").rstrip("/") if callback else "https://tweetpipeline-license-server.vercel.app"
    secret = setting("TELEGRAM_APPROVAL_SECRET", DEFAULT_ADMIN_TOKEN)
    approve = f"{base}/api/manual/approve/{quote(request_id)}?token={quote(secret)}"
    reject = f"{base}/api/manual/reject/{quote(request_id)}?token={quote(secret)}"
    return approve, reject


def create_manual_request(phone: str, plan: str, amount: int, device_id: str, msg: str, ip: str) -> dict[str, Any]:
    request_id = "MR-" + secrets.token_hex(5).upper()
    payload = {
        "request_id": request_id,
        "phone": phone,
        "amount": amount,
        "plan": plan,
        "device_id": device_id or "",
        "ip_address": ip or "",
        "message": msg[:2500],
        "status": "pending",
        "username": "",
        "temp_password": "",
        "created_at": utc_now(),
        "approved_at": None,
        "admin_note": "",
    }
    sb_insert("manual_requests", payload)
    approve, reject = make_manual_links(request_id)
    text = (
        "Tweet Pipeline Pro manual payment request\n\n"
        f"Manual Request ID: {request_id}\n"
        f"Phone: {phone}\n"
        f"Plan: {plan}\n"
        f"Amount: KES {amount}\n"
        f"Device: {device_id or '-'}\n"
        f"IP: {ip or '-'}\n\n"
        f"User message/proof:\n{msg or '-'}\n\n"
        f"APPROVE: {approve}\n"
        f"REJECT: {reject}\n\n"
        f"Telegram reply option: reply with 'approved {request_id}' to approve."
    )
    telegram_result = send_telegram_result(text)
    payload["telegram_sent"] = bool(telegram_result.get("ok"))
    payload["telegram_error"] = "" if telegram_result.get("ok") else str(telegram_result.get("error") or telegram_result)
    return payload


def approve_manual_request(request_id: str, note: str = "approved") -> dict[str, Any]:
    rows = sb_select("manual_requests", {"select": "*", "request_id": f"eq.{request_id}", "limit": "1"})
    if not rows:
        raise HTTPException(status_code=404, detail="Manual request not found")
    req = rows[0]
    if req.get("status") == "approved" and req.get("username") and req.get("temp_password"):
        return req
    creds = create_or_update_paid_user(req["phone"], req["plan"], req.get("device_id", ""), req.get("ip_address", ""))
    update = {
        "status": "approved",
        "username": creds["username"],
        "temp_password": creds["temporary_password"],
        "approved_at": utc_now(),
        "admin_note": note,
    }
    updated = sb_update("manual_requests", {"request_id": f"eq.{request_id}"}, update)[0]
    send_telegram(f"Approved {request_id}\nUsername: {creds['username']}\nPassword created and shown to user page.")
    return updated


# ---------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------
@app.on_event("startup")
def startup() -> None:
    try:
        ensure_default_settings()
    except Exception:
        pass


@app.get("/")
def home() -> Dict[str, Any]:
    return {"status": "running", "service": "Tweet Pipeline Pro License Server", "storage": "supabase", "time": utc_now()}


@app.get("/health")
def health() -> Dict[str, Any]:
    require_supabase()
    checks: Dict[str, Any] = {"status": "ok", "supabase": "connected", "time": utc_now()}
    for table in ["settings", "payments", "users", "manual_requests"]:
        try:
            sb_select(table, {"select": "*", "limit": "1"})
            checks[f"{table}_table"] = True
        except Exception as exc:
            checks["status"] = "error"
            checks[f"{table}_table"] = False
            checks[f"{table}_error"] = str(exc)
    return checks


@app.get("/api/public/config")
def public_config() -> Dict[str, Any]:
    return {
        "weekly_price": plan_amount("weekly"),
        "monthly_price": plan_amount("monthly"),
        "lifetime_price": plan_amount("lifetime"),
        "plans": ["weekly", "monthly", "lifetime"],
        "manual_approval_enabled": truthy(setting("MANUAL_APPROVAL_ENABLED", "1")),
    }


@app.post("/api/admin/settings")
def update_admin_settings(payload: Dict[str, Any], x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_token)
    allowed = {
        "DARAJA_ENV", "DARAJA_CONSUMER_KEY", "DARAJA_CONSUMER_SECRET", "DARAJA_SHORTCODE", "DARAJA_PARTYB",
        "DARAJA_PASSKEY", "DARAJA_CALLBACK_URL", "M_PESA_TRANSACTION_TYPE", "WEEKLY_PRICE", "MONTHLY_PRICE",
        "LIFETIME_PRICE", "LICENSE_DAYS_WEEKLY", "LICENSE_DAYS_MONTHLY", "LICENSE_DAYS_LIFETIME", "SERVER_ADMIN_TOKEN",
        "ACCOUNT_REFERENCE", "TRANSACTION_DESC", "ENFORCE_DEVICE_LOCK", "ENFORCE_IP_LOCK", "ALLOW_SANDBOX_SUCCESS_TEXT_AS_PAID",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_APPROVAL_SECRET", "MANUAL_APPROVAL_ENABLED", "PUBLIC_SERVER_URL",
    }
    for k, v in payload.items():
        if k in allowed:
            set_setting(k, v)
    return {"status": "saved", "settings": all_settings(mask_secret=True)}


@app.get("/api/admin/settings")
def read_admin_settings(x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_token)
    return {"settings": all_settings(mask_secret=True)}


@app.post("/api/admin/test-telegram")
def admin_test_telegram(payload: Dict[str, Any], x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_token)
    message = str(payload.get("message") or "Tweet Pipeline Pro Telegram test.")
    result = send_telegram_result(message)
    return result


# ---------------------------------------------------------------------
# Admin live control center API
# ---------------------------------------------------------------------
def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_today_utc(value: Any) -> bool:
    dt = _parse_iso_dt(value)
    return bool(dt and dt.date() == datetime.now(timezone.utc).date())


def _safe_table(table: str, params: Dict[str, str]) -> list[dict[str, Any]]:
    try:
        return sb_select(table, params)
    except Exception:
        return []


def _admin_live_payload() -> Dict[str, Any]:
    users = _safe_table("users", {"select": "*", "order": "id.desc", "limit": "1000"})
    payments = _safe_table("payments", {"select": "*", "order": "id.desc", "limit": "1000"})
    manual = _safe_table("manual_requests", {"select": "*", "order": "id.desc", "limit": "1000"})

    now = datetime.now(timezone.utc)
    active_users = 0
    expired_users = 0
    last_login = ""
    for u in users:
        if u.get("status") == "active":
            exp = _parse_iso_dt(u.get("license_expiry"))
            if exp and exp < now:
                expired_users += 1
            else:
                active_users += 1
        elif str(u.get("status", "")).lower() == "expired":
            expired_users += 1
        ll = u.get("last_login") or ""
        if ll and (not last_login or str(ll) > str(last_login)):
            last_login = str(ll)

    paid_payments = [p for p in payments if str(p.get("status", "")).lower() == "paid"]
    pending_payments = [p for p in payments if str(p.get("status", "")).lower() == "pending"]
    failed_payments = [p for p in payments if str(p.get("status", "")).lower() in {"failed", "cancelled", "expired"}]
    payments_today = [p for p in payments if _is_today_utc(p.get("paid_at") or p.get("created_at"))]
    paid_today = [p for p in paid_payments if _is_today_utc(p.get("paid_at") or p.get("created_at"))]

    pending_manual = [m for m in manual if str(m.get("status", "")).lower() == "pending"]
    approved_manual = [m for m in manual if str(m.get("status", "")).lower() == "approved"]
    rejected_manual = [m for m in manual if str(m.get("status", "")).lower() == "rejected"]

    activities: list[dict[str, Any]] = []
    for p in payments[:30]:
        activities.append({
            "time": p.get("paid_at") or p.get("created_at") or "",
            "type": "M-Pesa payment",
            "actor": p.get("phone") or "",
            "status": p.get("status") or "",
            "detail": f"{p.get('plan') or ''} | KES {p.get('amount') or ''} | {p.get('result_desc') or p.get('mpesa_receipt') or ''}".strip(),
        })
    for m in manual[:30]:
        activities.append({
            "time": m.get("approved_at") or m.get("created_at") or "",
            "type": "Manual proof",
            "actor": m.get("phone") or "",
            "status": m.get("status") or "",
            "detail": f"{m.get('request_id') or ''} | {m.get('plan') or ''} | KES {m.get('amount') or ''}".strip(),
        })
    for u in users[:30]:
        if u.get("last_login"):
            event_type = "Customer login"
            event_time = u.get("last_login")
        else:
            event_type = "User created"
            event_time = u.get("created_at") or ""
        activities.append({
            "time": event_time,
            "type": event_type,
            "actor": u.get("username") or u.get("phone") or "",
            "status": u.get("status") or "",
            "detail": f"{u.get('plan') or ''} | device {u.get('device_id') or '-'} | ip {u.get('ip_address') or '-'}".strip(),
        })
    activities.sort(key=lambda x: str(x.get("time") or ""), reverse=True)

    server_online = True
    telegram_ready = telegram_configured()
    return {
        "status": "ok",
        "server_online": server_online,
        "storage": "supabase",
        "server_time": utc_now(),
        "telegram_configured": telegram_ready,
        "manual_approval_enabled": truthy(setting("MANUAL_APPROVAL_ENABLED", "1")),
        "device_lock_enabled": truthy(setting("ENFORCE_DEVICE_LOCK", "1")),
        "ip_lock_enabled": truthy(setting("ENFORCE_IP_LOCK", "1")),
        "daraja_env": setting("DARAJA_ENV", "sandbox"),
        "transaction_type": setting("M_PESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
        "callback_url": setting("DARAJA_CALLBACK_URL", ""),
        "counts": {
            "total_users": len(users),
            "active_users": active_users,
            "expired_users": expired_users,
            "total_payments": len(payments),
            "paid_payments": len(paid_payments),
            "pending_payments": len(pending_payments),
            "failed_payments": len(failed_payments),
            "payments_today": len(payments_today),
            "paid_today": len(paid_today),
            "pending_manual_requests": len(pending_manual),
            "approved_manual_requests": len(approved_manual),
            "rejected_manual_requests": len(rejected_manual),
        },
        "latest": {
            "last_login": last_login,
            "latest_payment": payments[0] if payments else None,
            "latest_manual_request": manual[0] if manual else None,
            "latest_user": users[0] if users else None,
        },
        "activity": activities[:50],
    }


@app.get("/api/admin/stats")
def admin_stats(x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_token)
    return _admin_live_payload()


@app.get("/api/admin/activity")
def admin_activity(x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_token)
    payload = _admin_live_payload()
    return {"status": "ok", "server_time": payload["server_time"], "activity": payload["activity"]}


@app.post("/api/payment/start")
def start_payment(payload: PaymentStartRequest, request: Request) -> Dict[str, Any]:
    phone = normalize_phone(payload.phone)
    plan = (payload.plan or "monthly").lower()
    amount = int(payload.amount or plan_amount(plan))
    if amount < 1:
        raise HTTPException(status_code=400, detail="Amount must be at least 1 KES.")
    shortcode = setting("DARAJA_SHORTCODE", "174379")
    party_b = setting("DARAJA_PARTYB", shortcode) or shortcode
    callback = setting("DARAJA_CALLBACK_URL")
    if not callback:
        raise HTTPException(status_code=400, detail="Callback URL is not configured on server admin page.")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    body = {
        "BusinessShortCode": shortcode,
        "Password": build_stk_password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": setting("M_PESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
        "Amount": str(amount),
        "PartyA": phone,
        "PartyB": party_b,
        "PhoneNumber": phone,
        "CallBackURL": callback,
        "AccountReference": setting("ACCOUNT_REFERENCE", "TweetPro")[:12] or "TweetPro",
        "TransactionDesc": setting("TRANSACTION_DESC", "License")[:13] or "License",
    }
    token = get_access_token()
    response = requests.post(stk_url(), json=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=35)
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if response.status_code != 200 or data.get("ResponseCode") != "0":
        raise HTTPException(status_code=400, detail=data)
    sb_insert(
        "payments",
        {
            "phone": phone,
            "amount": amount,
            "plan": plan,
            "device_id": payload.device_id or "",
            "ip_address": request_ip(request),
            "merchant_request_id": data.get("MerchantRequestID", ""),
            "checkout_request_id": data.get("CheckoutRequestID", ""),
            "status": "pending",
            "result_code": None,
            "result_desc": data.get("ResponseDescription", ""),
            "mpesa_receipt": "",
            "username": "",
            "temp_password": "",
            "created_at": utc_now(),
            "paid_at": None,
        },
    )
    return {
        "status": "pending",
        "message": "STK Push sent. Enter your M-Pesa PIN.",
        "checkout_request_id": data.get("CheckoutRequestID"),
        "merchant_request_id": data.get("MerchantRequestID"),
    }


@app.post("/api/mpesa/callback")
async def mpesa_callback(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    stk = payload.get("Body", {}).get("stkCallback", {})
    checkout_id = stk.get("CheckoutRequestID")
    result_code = stk.get("ResultCode")
    result_desc = stk.get("ResultDesc", "")
    if not checkout_id:
        return {"ResultCode": 1, "ResultDesc": "Missing CheckoutRequestID"}
    payment = find_payment_by_checkout(checkout_id)
    if not payment:
        return {"ResultCode": 1, "ResultDesc": "Payment not found"}
    success = int(result_code or -1) == 0
    receipt = ""
    amount = payment.get("amount")
    callback_phone = payment.get("phone")
    for item in stk.get("CallbackMetadata", {}).get("Item", []):
        if item.get("Name") == "MpesaReceiptNumber":
            receipt = str(item.get("Value", ""))
        if item.get("Name") == "PhoneNumber":
            callback_phone = str(item.get("Value", callback_phone))
        if item.get("Name") == "Amount":
            try:
                amount = int(float(item.get("Value", amount)))
            except Exception:
                pass
    sandbox_text_paid = (
        setting("DARAJA_ENV", "sandbox").lower() == "sandbox"
        and truthy(setting("ALLOW_SANDBOX_SUCCESS_TEXT_AS_PAID", "0"))
        and "processed successfully" in str(result_desc).lower()
    )
    if success or sandbox_text_paid:
        creds = create_or_update_paid_user(payment["phone"], payment["plan"], payment.get("device_id", ""), payment.get("ip_address", ""))
        sb_update(
            "payments",
            {"checkout_request_id": f"eq.{checkout_id}"},
            {
                "status": "paid",
                "result_code": int(result_code or 0),
                "result_desc": result_desc,
                "mpesa_receipt": receipt or f"SANDBOX-{checkout_id[-8:]}",
                "username": creds["username"],
                "temp_password": creds["temporary_password"],
                "paid_at": utc_now(),
            },
        )
    else:
        status = "cancelled" if int(result_code or -1) == 1032 else "failed"
        sb_update(
            "payments",
            {"checkout_request_id": f"eq.{checkout_id}"},
            {"status": status, "result_code": int(result_code or -1), "result_desc": result_desc},
        )
    return {"ResultCode": 0, "ResultDesc": "Callback received"}


@app.get("/api/payment/status/{checkout_request_id}")
def payment_status(checkout_request_id: str) -> Dict[str, Any]:
    payment = find_payment_by_checkout(checkout_request_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.get("status") == "paid":
        return {
            "status": "paid",
            "message": "Payment confirmed. Login details created.",
            "username": payment.get("username"),
            "temporary_password": payment.get("temp_password"),
            "plan": payment.get("plan"),
            "amount": payment.get("amount"),
            "mpesa_receipt": payment.get("mpesa_receipt"),
        }
    if payment.get("status") in {"failed", "cancelled", "expired"}:
        return {"status": payment.get("status"), "message": payment.get("result_desc") or "Payment failed/cancelled."}
    return {"status": "pending", "message": "Waiting for M-Pesa confirmation."}


@app.post("/api/manual/request")
def manual_payment_request(payload: ManualPaymentRequest, request: Request) -> Dict[str, Any]:
    if not truthy(setting("MANUAL_APPROVAL_ENABLED", "1")):
        raise HTTPException(status_code=403, detail="Manual payment approval is disabled.")
    phone = normalize_phone(payload.phone)
    plan = (payload.plan or "monthly").lower()
    amount = int(payload.amount or plan_amount(plan))
    req = create_manual_request(phone, plan, amount, payload.device_id or "", payload.message or "", request_ip(request))
    return {
        "status": "pending",
        "message": "Manual payment request sent to owner. Keep this window open and wait for approval.",
        "request_id": req["request_id"],
        "telegram_sent": req.get("telegram_sent", False),
        "telegram_error": req.get("telegram_error", ""),
    }


@app.get("/api/manual/status/{request_id}")
def manual_status(request_id: str) -> Dict[str, Any]:
    rows = sb_select("manual_requests", {"select": "*", "request_id": f"eq.{request_id}", "limit": "1"})
    if not rows:
        raise HTTPException(status_code=404, detail="Manual request not found")
    req = rows[0]
    if req.get("status") == "approved":
        return {
            "status": "approved",
            "message": "Owner approved payment. Login details created.",
            "username": req.get("username"),
            "temporary_password": req.get("temp_password"),
            "plan": req.get("plan"),
            "amount": req.get("amount"),
        }
    if req.get("status") in {"rejected", "cancelled"}:
        return {"status": req.get("status"), "message": req.get("admin_note") or "Manual request was not approved."}
    return {"status": "pending", "message": "Waiting for owner approval."}


@app.get("/api/manual/approve/{request_id}", response_class=HTMLResponse)
def manual_approve(request_id: str, token: str = "") -> str:
    secret = setting("TELEGRAM_APPROVAL_SECRET", DEFAULT_ADMIN_TOKEN)
    if not token or not secrets.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Invalid approval token")
    req = approve_manual_request(request_id, "approved by link")
    return f"<html><body style='font-family:Arial;padding:24px'><h2>Approved</h2><p>{request_id} approved.</p><p>Username: <b>{req.get('username')}</b></p><p>The user's payment window will receive the password automatically.</p></body></html>"


@app.get("/api/manual/reject/{request_id}", response_class=HTMLResponse)
def manual_reject(request_id: str, token: str = "") -> str:
    secret = setting("TELEGRAM_APPROVAL_SECRET", DEFAULT_ADMIN_TOKEN)
    if not token or not secrets.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Invalid approval token")
    sb_update("manual_requests", {"request_id": f"eq.{request_id}"}, {"status": "rejected", "admin_note": "rejected by link"})
    send_telegram(f"Rejected {request_id}")
    return f"<html><body style='font-family:Arial;padding:24px'><h2>Rejected</h2><p>{request_id} rejected.</p></body></html>"


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request, token: str = "") -> Dict[str, Any]:
    # Optional: set Telegram webhook to /api/telegram/webhook?token=YOUR_TELEGRAM_APPROVAL_SECRET.
    secret = setting("TELEGRAM_APPROVAL_SECRET", DEFAULT_ADMIN_TOKEN)
    if not token or not secrets.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Invalid webhook token")
    update = await request.json()
    msg = update.get("message", {}) or update.get("edited_message", {})
    text = (msg.get("text") or "").strip()
    combined = text + "\n" + ((msg.get("reply_to_message") or {}).get("text") or "")
    match = re.search(r"(MR-[A-F0-9]{10})", combined, re.I)
    if match and "approved" in text.lower():
        approve_manual_request(match.group(1).upper(), "approved by telegram reply")
        return {"ok": True, "approved": match.group(1).upper()}
    return {"ok": True, "ignored": True}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request) -> Dict[str, Any]:
    username = payload.username.strip()
    rows = sb_select("users", {"select": "*", "username": f"eq.{username}", "limit": "1"})
    if not rows:
        raise HTTPException(status_code=401, detail="Invalid username/password or inactive account.")
    user = rows[0]
    if user.get("status") != "active" or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid username/password or inactive account.")
    if user.get("license_expiry"):
        exp = datetime.fromisoformat(str(user["license_expiry"]).replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            raise HTTPException(status_code=403, detail="License expired.")
    current_ip = request_ip(request)
    updates: Dict[str, Any] = {"last_login": utc_now()}
    if truthy(setting("ENFORCE_DEVICE_LOCK", "1")):
        existing_device = user.get("device_id") or ""
        if existing_device and payload.device_id and existing_device != payload.device_id:
            raise HTTPException(status_code=403, detail="License is locked to another computer/device.")
        if payload.device_id and not existing_device:
            updates["device_id"] = payload.device_id
    if truthy(setting("ENFORCE_IP_LOCK", "1")):
        existing_ip = user.get("ip_address") or ""
        if existing_ip and current_ip and existing_ip != current_ip:
            raise HTTPException(status_code=403, detail="License is locked to another IP address.")
        if current_ip and not existing_ip:
            updates["ip_address"] = current_ip
    if updates:
        sb_update("users", {"id": f"eq.{user['id']}"}, updates)
    return {
        "status": "success",
        "username": user["username"],
        "phone": user["phone"],
        "plan": user["plan"],
        "license_expiry": user["license_expiry"],
        "token": user["token"],
        "must_change_password": bool(user.get("must_change_password")),
    }


@app.post("/api/license/check")
def license_check(payload: LicenseCheckRequest, request: Request) -> Dict[str, Any]:
    rows = sb_select("users", {"select": "*", "token": f"eq.{payload.token}", "limit": "1"})
    if not rows:
        raise HTTPException(status_code=403, detail="License inactive.")
    user = rows[0]
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="License inactive.")
    if user.get("license_expiry"):
        exp = datetime.fromisoformat(str(user["license_expiry"]).replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            raise HTTPException(status_code=403, detail="License expired.")
    if truthy(setting("ENFORCE_DEVICE_LOCK", "1")) and user.get("device_id") and payload.device_id and user["device_id"] != payload.device_id:
        raise HTTPException(status_code=403, detail="License locked to another device.")
    if truthy(setting("ENFORCE_IP_LOCK", "1")) and user.get("ip_address") and request_ip(request) and user["ip_address"] != request_ip(request):
        raise HTTPException(status_code=403, detail="License locked to another IP.")
    return {"status": "active", "username": user["username"], "plan": user["plan"], "license_expiry": user["license_expiry"]}


# ---------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin_page(token: str = "") -> str:
    real_token = setting("SERVER_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)
    if not token or not secrets.compare_digest(token, real_token):
        return """
        <html><body style='font-family:Segoe UI,Arial;padding:30px;max-width:720px;margin:auto;background:#0f172a;color:#e5e7eb'>
        <h2>Tweet Pipeline Pro License Server</h2>
        <p>Enter admin token to open settings.</p>
        <form method='get'><input name='token' type='password' style='width:360px;padding:10px;border-radius:8px'> <button style='padding:10px 16px'>Open</button></form>
        </body></html>
        """
    ensure_default_settings()
    s = all_settings(mask_secret=False)
    live = _admin_live_payload()
    counts = live.get("counts", {})
    activity = live.get("activity", [])[:20]
    payments = sb_select("payments", {"select": "*", "order": "id.desc", "limit": "50"})
    users = sb_select("users", {"select": "username,phone,plan,status,license_expiry,device_id,ip_address,created_at,last_login", "order": "id.desc", "limit": "50"})
    try:
        manual = sb_select("manual_requests", {"select": "*", "order": "id.desc", "limit": "50"})
    except Exception:
        manual = []

    def esc(x: Any) -> str:
        import html
        return html.escape(str(x or ""))

    setting_rows = "".join(
        f"<label>{esc(k)}</label><input name='{esc(k)}' value='{esc(v)}' {'type=password' if any(x in k for x in ['SECRET','PASSKEY','TOKEN','KEY']) else ''}>"
        for k, v in s.items()
    )
    payment_rows = "".join(
        f"<tr><td>{p.get('id')}</td><td>{esc(p.get('phone'))}</td><td>{p.get('amount')}</td><td>{esc(p.get('plan'))}</td><td><b>{esc(p.get('status'))}</b></td><td>{esc(p.get('result_desc'))}</td><td>{esc(p.get('mpesa_receipt'))}</td><td>{esc(p.get('username'))}</td><td>{esc(p.get('created_at'))}</td></tr>"
        for p in payments
    )
    manual_rows = "".join(
        f"<tr><td>{esc(m.get('request_id'))}</td><td>{esc(m.get('phone'))}</td><td>{m.get('amount')}</td><td>{esc(m.get('plan'))}</td><td><b>{esc(m.get('status'))}</b></td><td>{esc(m.get('username'))}</td><td>{esc(m.get('message'))[:160]}</td><td><a href='/api/manual/approve/{quote(m.get('request_id',''))}?token={quote(setting('TELEGRAM_APPROVAL_SECRET', DEFAULT_ADMIN_TOKEN))}'>Approve</a> | <a href='/api/manual/reject/{quote(m.get('request_id',''))}?token={quote(setting('TELEGRAM_APPROVAL_SECRET', DEFAULT_ADMIN_TOKEN))}'>Reject</a></td></tr>"
        for m in manual
    )
    user_rows = "".join(
        f"<tr><td>{esc(u.get('username'))}</td><td>{esc(u.get('phone'))}</td><td>{esc(u.get('plan'))}</td><td>{esc(u.get('status'))}</td><td>{esc(u.get('license_expiry'))}</td><td>{esc(u.get('device_id'))}</td><td>{esc(u.get('ip_address'))}</td><td>{esc(u.get('last_login'))}</td></tr>"
        for u in users
    )
    stat_cards = "".join([
        f"<div class='metric'><b>{counts.get('total_users', 0)}</b><span>Total Users</span></div>",
        f"<div class='metric'><b>{counts.get('active_users', 0)}</b><span>Active Users</span></div>",
        f"<div class='metric'><b>{counts.get('paid_today', 0)}</b><span>Paid Today</span></div>",
        f"<div class='metric'><b>{counts.get('pending_manual_requests', 0)}</b><span>Manual Pending</span></div>",
        f"<div class='metric'><b>{counts.get('failed_payments', 0)}</b><span>Failed Payments</span></div>",
        f"<div class='metric'><b>{'Yes' if live.get('telegram_configured') else 'No'}</b><span>Telegram Ready</span></div>",
    ])
    activity_rows = "".join(
        f"<tr><td>{esc(a.get('time'))}</td><td>{esc(a.get('type'))}</td><td>{esc(a.get('actor'))}</td><td><b>{esc(a.get('status'))}</b></td><td>{esc(a.get('detail'))}</td></tr>"
        for a in activity
    )
    return f"""
    <html><head><title>TPP License Server Admin</title>
    <style>body{{font-family:Segoe UI,Arial;background:#0f172a;color:#e5e7eb;padding:24px}} .wrap{{max-width:1280px;margin:auto}} .card{{background:#111827;border:1px solid #243244;border-radius:16px;padding:18px;margin:14px 0;box-shadow:0 16px 40px rgba(0,0,0,.25)}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}} .metric{{background:#020617;border:1px solid #334155;border-radius:14px;padding:14px}} .metric b{{display:block;font-size:28px;color:#38bdf8}} .metric span{{color:#cbd5e1;font-size:12px}} h1{{margin:0 0 8px}} label{{display:block;margin-top:10px;color:#93c5fd;font-size:13px}} input{{width:100%;padding:10px;border-radius:10px;border:1px solid #334155;background:#020617;color:#e5e7eb}} button{{padding:11px 18px;border:0;border-radius:10px;background:#38bdf8;font-weight:bold;margin-top:14px;cursor:pointer}} table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #334155;padding:8px;font-size:12px;vertical-align:top}} a{{color:#38bdf8}}</style></head>
    <body><div class='wrap'><h1>Tweet Pipeline Pro License Server Admin</h1><p>Storage: Supabase | Time: {esc(utc_now())} | Live API: /api/admin/stats</p>
    <div class='card'><h2>Live Control Center</h2><div class='grid'>{stat_cards}</div><p>Device Lock: <b>{'ON' if live.get('device_lock_enabled') else 'OFF'}</b> | IP Lock: <b>{'ON' if live.get('ip_lock_enabled') else 'OFF'}</b> | Manual Approval: <b>{'ON' if live.get('manual_approval_enabled') else 'OFF'}</b> | Daraja: <b>{esc(live.get('daraja_env'))}</b></p><h3>Latest Activity</h3><table><tr><th>Time</th><th>Event</th><th>User/Phone</th><th>Status</th><th>Detail</th></tr>{activity_rows}</table></div>
    <div class='card'><h2>Payment / Daraja / Security / Telegram Settings</h2>
    <form method='post' action='/admin/save?token={quote(token)}'>{setting_rows}<button>Save Settings</button></form>
    <p>Callback URL: <b>{esc(s.get('DARAJA_CALLBACK_URL'))}</b></p>
    <p><b>Till mode:</b> set M_PESA_TRANSACTION_TYPE=CustomerBuyGoodsOnline, DARAJA_SHORTCODE=approved HO/store shortcode, DARAJA_PARTYB=actual Till number.</p></div>
    <div class='card'><h2>Manual Payment Requests</h2><table><tr><th>ID</th><th>Phone</th><th>Amount</th><th>Plan</th><th>Status</th><th>Username</th><th>Message</th><th>Action</th></tr>{manual_rows}</table></div>
    <div class='card'><h2>Recent M-Pesa Payments</h2><table><tr><th>ID</th><th>Phone</th><th>Amount</th><th>Plan</th><th>Status</th><th>Result</th><th>Receipt</th><th>Username</th><th>Created</th></tr>{payment_rows}</table></div>
    <div class='card'><h2>Users</h2><table><tr><th>Username</th><th>Phone</th><th>Plan</th><th>Status</th><th>Expiry</th><th>Device</th><th>IP</th><th>Last Login</th></tr>{user_rows}</table></div>
    </div></body></html>
    """


@app.post("/admin/save", response_class=HTMLResponse)
async def admin_save(request: Request, token: str = "") -> HTMLResponse:
    real_token = setting("SERVER_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)
    if not token or not secrets.compare_digest(token, real_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    form = await request.form()
    for k, v in form.items():
        set_setting(k, v)
    return HTMLResponse(f"<html><body style='font-family:Arial;padding:30px'><h3>Saved.</h3><a href='/admin?token={quote(token)}'>Back to admin</a></body></html>")
