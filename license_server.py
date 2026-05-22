from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from requests.auth import HTTPBasicAuth

BASE_DIR = Path(__file__).resolve().parent

# VERCEL FIX:
# Serverless functions cannot write beside source files.
# On Vercel, SQLite must use /tmp. This is for testing only;
# use Supabase/Postgres later for real paid customers.
if os.getenv("VERCEL"):
    DATA_DIR = Path("/tmp")
else:
    DATA_DIR = BASE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path("/tmp/license_server.db")

DEFAULT_ADMIN_TOKEN = (
    os.getenv("LICENSE_SERVER_ADMIN_TOKEN")
    or os.getenv("TPP_SERVER_ADMIN_TOKEN")
    or "change-this-admin-token"
)

app = FastAPI(title="Tweet Pipeline Pro License Server")


class PaymentStartRequest(BaseModel):
    phone: str
    plan: str = "monthly"
    amount: Optional[int] = None
    device_id: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str
    device_id: Optional[str] = None


class LicenseCheckRequest(BaseModel):
    token: str
    device_id: Optional[str] = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
        # WAL is useful locally but can be unnecessary in serverless.
        if not os.getenv("VERCEL"):
            conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                amount INTEGER NOT NULL,
                plan TEXT NOT NULL,
                device_id TEXT DEFAULT '',
                merchant_request_id TEXT DEFAULT '',
                checkout_request_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                result_code INTEGER,
                result_desc TEXT DEFAULT '',
                mpesa_receipt TEXT DEFAULT '',
                username TEXT DEFAULT '',
                temp_password TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                paid_at TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                phone TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                plan TEXT NOT NULL,
                license_expiry TEXT,
                device_id TEXT DEFAULT '',
                token TEXT UNIQUE NOT NULL,
                must_change_password INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_login TEXT
            );
            """
        )
        defaults = {
            "DARAJA_ENV": "sandbox",
            "DARAJA_CONSUMER_KEY": os.getenv("DARAJA_CONSUMER_KEY", ""),
            "DARAJA_CONSUMER_SECRET": os.getenv("DARAJA_CONSUMER_SECRET", ""),
            "DARAJA_SHORTCODE": os.getenv("DARAJA_SHORTCODE", "174379"),
            "DARAJA_PASSKEY": os.getenv("DARAJA_PASSKEY", ""),
            "DARAJA_CALLBACK_URL": os.getenv("DARAJA_CALLBACK_URL", os.getenv("CALLBACK_URL", "")),
            "WEEKLY_PRICE": os.getenv("WEEKLY_PRICE", "500"),
            "MONTHLY_PRICE": os.getenv("MONTHLY_PRICE", "1500"),
            "LIFETIME_PRICE": os.getenv("LIFETIME_PRICE", "15000"),
            "LICENSE_DAYS_WEEKLY": os.getenv("LICENSE_DAYS_WEEKLY", "7"),
            "LICENSE_DAYS_MONTHLY": os.getenv("LICENSE_DAYS", "30"),
            "LICENSE_DAYS_LIFETIME": os.getenv("LICENSE_DAYS_LIFETIME", "36500"),
            "SERVER_ADMIN_TOKEN": DEFAULT_ADMIN_TOKEN,
            "ACCOUNT_REFERENCE": "TweetPro",
            "TRANSACTION_DESC": "License",
        }
        now = utc_now()
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)", (k, str(v), now))
        conn.commit()


def setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), utc_now()),
        )
        conn.commit()


def all_settings(mask_secret: bool = False) -> Dict[str, str]:
    with db() as conn:
        rows = conn.execute("SELECT key,value FROM settings ORDER BY key").fetchall()
    result = {r["key"]: r["value"] for r in rows}
    if mask_secret:
        for k in list(result):
            if "SECRET" in k or "PASSKEY" in k or "TOKEN" in k:
                v = result[k]
                result[k] = "" if not v else (v[:4] + "..." + v[-4:] if len(v) > 10 else "********")
    return result


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
        return int(setting("LICENSE_DAYS_WEEKLY", "7") or 7)
    if plan == "lifetime":
        return int(setting("LICENSE_DAYS_LIFETIME", "36500") or 36500)
    return int(setting("LICENSE_DAYS_MONTHLY", "30") or 30)


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


def create_or_update_paid_user(phone: str, plan: str, device_id: str) -> Dict[str, str]:
    username = f"user{phone}"
    temp_password = generate_temp_password()
    password_hash = hash_password(temp_password)
    days = plan_days(plan)
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users(username, phone, password_hash, status, plan, license_expiry, device_id, token, must_change_password, created_at)
            VALUES(?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(username) DO UPDATE SET
                phone=excluded.phone,
                password_hash=excluded.password_hash,
                status='active',
                plan=excluded.plan,
                license_expiry=excluded.license_expiry,
                device_id=excluded.device_id,
                token=excluded.token,
                must_change_password=1
            """,
            (username, phone, password_hash, "active", plan, expiry, device_id or "", token, utc_now()),
        )
        conn.commit()
    return {"username": username, "temporary_password": temp_password, "license_expiry": expiry, "token": token}


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def home() -> Dict[str, Any]:
    return {"status": "running", "service": "Tweet Pipeline Pro License Server", "time": utc_now()}


@app.get("/api/public/config")
def public_config() -> Dict[str, Any]:
    return {
        "weekly_price": plan_amount("weekly"),
        "monthly_price": plan_amount("monthly"),
        "lifetime_price": plan_amount("lifetime"),
        "plans": ["weekly", "monthly", "lifetime"],
    }


@app.post("/api/admin/settings")
def update_admin_settings(payload: Dict[str, Any], _ok: None = None, x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_token)
    allowed = {
        "DARAJA_ENV", "DARAJA_CONSUMER_KEY", "DARAJA_CONSUMER_SECRET", "DARAJA_SHORTCODE", "DARAJA_PASSKEY",
        "DARAJA_CALLBACK_URL", "WEEKLY_PRICE", "MONTHLY_PRICE", "LIFETIME_PRICE", "LICENSE_DAYS_WEEKLY",
        "LICENSE_DAYS_MONTHLY", "LICENSE_DAYS_LIFETIME", "SERVER_ADMIN_TOKEN", "ACCOUNT_REFERENCE", "TRANSACTION_DESC",
    }
    for k, v in payload.items():
        if k in allowed:
            set_setting(k, v)
    return {"status": "saved", "settings": all_settings(mask_secret=True)}


@app.get("/api/admin/settings")
def read_admin_settings(x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_token)
    return {"settings": all_settings(mask_secret=True)}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(token: str = "") -> str:
    real_token = setting("SERVER_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)
    if not token or not secrets.compare_digest(token, real_token):
        return """
        <html><body style='font-family:Arial;padding:30px;max-width:680px;margin:auto'>
        <h2>Tweet Pipeline Pro License Server</h2>
        <p>Enter admin token to open settings.</p>
        <form method='get'><input name='token' type='password' style='width:360px;padding:8px'> <button>Open</button></form>
        </body></html>
        """
    s = all_settings(mask_secret=False)
    with db() as conn:
        payments = conn.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 50").fetchall()
        users = conn.execute("SELECT username, phone, plan, status, license_expiry, device_id, created_at FROM users ORDER BY id DESC LIMIT 50").fetchall()
    def esc(x: Any) -> str:
        import html
        return html.escape(str(x or ""))
    setting_rows = "".join(
        f"<label>{esc(k)}</label><input name='{esc(k)}' value='{esc(v)}' {'type=password' if ('SECRET' in k or 'PASSKEY' in k or 'TOKEN' in k) else ''}>"
        for k, v in s.items()
    )
    payment_rows = "".join(f"<tr><td>{p['id']}</td><td>{esc(p['phone'])}</td><td>{p['amount']}</td><td>{esc(p['plan'])}</td><td>{esc(p['status'])}</td><td>{esc(p['mpesa_receipt'])}</td><td>{esc(p['username'])}</td><td>{esc(p['created_at'])}</td></tr>" for p in payments)
    user_rows = "".join(f"<tr><td>{esc(u['username'])}</td><td>{esc(u['phone'])}</td><td>{esc(u['plan'])}</td><td>{esc(u['status'])}</td><td>{esc(u['license_expiry'])}</td><td>{esc(u['device_id'])}</td></tr>" for u in users)
    return f"""
    <html><head><title>TPP License Server Admin</title>
    <style>body{{font-family:Segoe UI,Arial;background:#0f172a;color:#e5e7eb;padding:24px}} .card{{background:#111827;border-radius:14px;padding:18px;margin:12px 0}} label{{display:block;margin-top:10px;color:#93c5fd}} input{{width:100%;padding:9px;border-radius:8px;border:1px solid #334155;background:#020617;color:#e5e7eb}} button{{padding:10px 16px;border:0;border-radius:8px;background:#38bdf8;font-weight:bold;margin-top:12px}} table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #334155;padding:7px;font-size:12px}}</style></head>
    <body><h1>Tweet Pipeline Pro License Server Admin</h1>
    <div class='card'><h2>Payment / Daraja Settings</h2>
    <form method='post' action='/admin/save?token={quote(token)}'>{setting_rows}<button>Save Settings</button></form>
    <p>Callback URL to put in Daraja/server setting: <b>{esc(s.get('DARAJA_CALLBACK_URL'))}</b></p></div>
    <div class='card'><h2>Recent Payments</h2><table><tr><th>ID</th><th>Phone</th><th>Amount</th><th>Plan</th><th>Status</th><th>Receipt</th><th>Username</th><th>Created</th></tr>{payment_rows}</table></div>
    <div class='card'><h2>Users</h2><table><tr><th>Username</th><th>Phone</th><th>Plan</th><th>Status</th><th>Expiry</th><th>Device</th></tr>{user_rows}</table></div>
    </body></html>
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


@app.post("/api/payment/start")
def start_payment(payload: PaymentStartRequest) -> Dict[str, Any]:
    phone = normalize_phone(payload.phone)
    plan = (payload.plan or "monthly").lower()
    amount = int(payload.amount or plan_amount(plan))
    if amount < 1:
        raise HTTPException(status_code=400, detail="Amount must be at least 1 KES.")
    shortcode = setting("DARAJA_SHORTCODE", "174379")
    callback = setting("DARAJA_CALLBACK_URL")
    if not callback:
        raise HTTPException(status_code=400, detail="Callback URL is not configured on server admin page.")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    body = {
        "BusinessShortCode": shortcode,
        "Password": build_stk_password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": str(amount),
        "PartyA": phone,
        "PartyB": shortcode,
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
    with db() as conn:
        conn.execute(
            """
            INSERT INTO payments(phone, amount, plan, device_id, merchant_request_id, checkout_request_id, status, created_at)
            VALUES(?,?,?,?,?,?, 'pending', ?)
            """,
            (phone, amount, plan, payload.device_id or "", data.get("MerchantRequestID", ""), data.get("CheckoutRequestID", ""), utc_now()),
        )
        conn.commit()
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
    with db() as conn:
        payment = conn.execute("SELECT * FROM payments WHERE checkout_request_id=?", (checkout_id,)).fetchone()
        if not payment:
            return {"ResultCode": 1, "ResultDesc": "Payment not found"}
        if int(result_code or -1) == 0:
            receipt = ""
            amount = payment["amount"]
            callback_phone = payment["phone"]
            for item in stk.get("CallbackMetadata", {}).get("Item", []):
                if item.get("Name") == "MpesaReceiptNumber": receipt = str(item.get("Value", ""))
                if item.get("Name") == "PhoneNumber": callback_phone = str(item.get("Value", callback_phone))
                if item.get("Name") == "Amount":
                    try: amount = int(float(item.get("Value", amount)))
                    except Exception: pass
            creds = create_or_update_paid_user(payment["phone"], payment["plan"], payment["device_id"])
            conn.execute(
                """
                UPDATE payments SET status='paid', result_code=?, result_desc=?, mpesa_receipt=?, username=?, temp_password=?, paid_at=?
                WHERE checkout_request_id=?
                """,
                (result_code, result_desc, receipt, creds["username"], creds["temporary_password"], utc_now(), checkout_id),
            )
        else:
            status = "cancelled" if int(result_code or -1) == 1032 else "failed"
            conn.execute("UPDATE payments SET status=?, result_code=?, result_desc=? WHERE checkout_request_id=?", (status, result_code, result_desc, checkout_id))
        conn.commit()
    return {"ResultCode": 0, "ResultDesc": "Callback received"}


@app.get("/api/payment/status/{checkout_request_id}")
def payment_status(checkout_request_id: str) -> Dict[str, Any]:
    with db() as conn:
        p = conn.execute("SELECT * FROM payments WHERE checkout_request_id=?", (checkout_request_id,)).fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    if p["status"] == "paid":
        return {
            "status": "paid",
            "message": "Payment confirmed. Login details created.",
            "username": p["username"],
            "temporary_password": p["temp_password"],
            "plan": p["plan"],
            "amount": p["amount"],
            "mpesa_receipt": p["mpesa_receipt"],
        }
    if p["status"] in {"failed", "cancelled", "expired"}:
        return {"status": p["status"], "message": p["result_desc"] or "Payment failed/cancelled."}
    return {"status": "pending", "message": "Waiting for M-Pesa confirmation."}


@app.post("/api/auth/login")
def login(payload: LoginRequest) -> Dict[str, Any]:
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=?", (payload.username.strip(),)).fetchone()
        if not user or user["status"] != "active" or not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username/password or inactive account.")
        if user["license_expiry"]:
            exp = datetime.fromisoformat(user["license_expiry"].replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                raise HTTPException(status_code=403, detail="License expired.")
        if user["device_id"] and payload.device_id and user["device_id"] != payload.device_id:
            raise HTTPException(status_code=403, detail="License is locked to another device.")
        if payload.device_id and not user["device_id"]:
            conn.execute("UPDATE users SET device_id=? WHERE id=?", (payload.device_id, user["id"]))
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (utc_now(), user["id"]))
        conn.commit()
        return {
            "status": "success",
            "username": user["username"],
            "phone": user["phone"],
            "plan": user["plan"],
            "license_expiry": user["license_expiry"],
            "token": user["token"],
            "must_change_password": bool(user["must_change_password"]),
        }


@app.post("/api/license/check")
def license_check(payload: LicenseCheckRequest) -> Dict[str, Any]:
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE token=?", (payload.token,)).fetchone()
    if not user or user["status"] != "active":
        raise HTTPException(status_code=403, detail="License inactive.")
    if user["license_expiry"]:
        exp = datetime.fromisoformat(user["license_expiry"].replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            raise HTTPException(status_code=403, detail="License expired.")
    if user["device_id"] and payload.device_id and user["device_id"] != payload.device_id:
        raise HTTPException(status_code=403, detail="License locked to another device.")
    return {"status": "active", "username": user["username"], "plan": user["plan"], "license_expiry": user["license_expiry"]}


# Initialize when module is imported. This helps serverless platforms.
try:
    init_db()
except Exception as exc:
    print(f"[TPP] DB init warning: {exc}")
