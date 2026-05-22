from __future__ import annotations

import base64
import hashlib
import os
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

app = FastAPI(title="Tweet Pipeline Pro License Server - Supabase")

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
    "DARAJA_PASSKEY": os.getenv("DARAJA_PASSKEY", ""),
    "DARAJA_CALLBACK_URL": os.getenv("CALLBACK_URL", os.getenv("DARAJA_CALLBACK_URL", "")),
    "WEEKLY_PRICE": os.getenv("WEEKLY_PRICE", "500"),
    "MONTHLY_PRICE": os.getenv("MONTHLY_PRICE", os.getenv("DEFAULT_AMOUNT", "1500")),
    "LIFETIME_PRICE": os.getenv("LIFETIME_PRICE", "15000"),
    "LICENSE_DAYS_WEEKLY": os.getenv("LICENSE_DAYS_WEEKLY", "7"),
    "LICENSE_DAYS_MONTHLY": os.getenv("LICENSE_DAYS", os.getenv("LICENSE_DAYS_MONTHLY", "30")),
    "LICENSE_DAYS_LIFETIME": os.getenv("LICENSE_DAYS_LIFETIME", "36500"),
    "SERVER_ADMIN_TOKEN": DEFAULT_ADMIN_TOKEN,
    "ACCOUNT_REFERENCE": os.getenv("ACCOUNT_REFERENCE", "TweetPro"),
    "TRANSACTION_DESC": os.getenv("TRANSACTION_DESC", "License"),
}

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    response = requests.post(
        sb_url(table),
        headers=sb_headers("return=representation"),
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=500, detail={"supabase_insert_error": response.text, "table": table})
    return response.json()


def sb_update(table: str, filters: Dict[str, str], payload: Dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.patch(
        sb_url(table),
        headers=sb_headers("return=representation"),
        params=filters,
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=500, detail={"supabase_update_error": response.text, "table": table})
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
        raise HTTPException(status_code=500, detail={"supabase_upsert_error": response.text, "table": table})
    return response.json()


def setting(key: str, default: str = "") -> str:
    # Environment variables are always fallback. Supabase settings override them.
    fallback = ENV_DEFAULTS.get(key, os.getenv(key, default))
    try:
        rows = sb_select("settings", {"select": "value", "key": f"eq.{key}", "limit": "1"})
        if rows:
            return str(rows[0].get("value", fallback))
    except HTTPException:
        # During first setup before schema is created, keep app usable enough to show clear home/admin errors.
        pass
    return str(fallback if fallback is not None else default)


def set_setting(key: str, value: Any) -> None:
    sb_upsert(
        "settings",
        {"key": key, "value": str(value), "updated_at": utc_now()},
        "key",
    )


def all_settings(mask_secret: bool = False) -> Dict[str, str]:
    result = dict(ENV_DEFAULTS)
    try:
        rows = sb_select("settings", {"select": "key,value", "order": "key.asc"})
        for row in rows:
            result[str(row["key"])] = str(row.get("value", ""))
    except HTTPException:
        pass
    if mask_secret:
        for k in list(result):
            if any(s in k for s in ["SECRET", "PASSKEY", "TOKEN", "KEY"]):
                v = result[k]
                result[k] = "" if not v else (v[:4] + "..." + v[-4:] if len(v) > 10 else "********")
    return result


def ensure_default_settings() -> None:
    # Safe to call often. Keeps Supabase settings table populated.
    for key, value in ENV_DEFAULTS.items():
        try:
            rows = sb_select("settings", {"select": "key", "key": f"eq.{key}", "limit": "1"})
            if not rows:
                set_setting(key, value)
        except HTTPException:
            # If schema not created yet, /health will reveal it.
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


def safe_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value)))
    except Exception:
        return default


def plan_amount(plan: str) -> int:
    plan = (plan or "monthly").lower()
    if plan == "weekly":
        return safe_int(setting("WEEKLY_PRICE", "500"), 500)
    if plan == "lifetime":
        return safe_int(setting("LIFETIME_PRICE", "15000"), 15000)
    return safe_int(setting("MONTHLY_PRICE", "1500"), 1500)


def plan_days(plan: str) -> int:
    plan = (plan or "monthly").lower()
    if plan == "weekly":
        return safe_int(setting("LICENSE_DAYS_WEEKLY", "7"), 7)
    if plan == "lifetime":
        return safe_int(setting("LICENSE_DAYS_LIFETIME", "36500"), 36500)
    return safe_int(setting("LICENSE_DAYS_MONTHLY", "30"), 30)


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
        raise HTTPException(status_code=400, detail="Daraja Consumer Key/Secret not configured.")
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
        raise HTTPException(status_code=400, detail="Daraja Passkey not configured.")
    raw = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


def create_or_update_paid_user(phone: str, plan: str, device_id: str) -> Dict[str, str]:
    username = f"user{phone}"
    temp_password = generate_temp_password()
    password_hash = hash_password(temp_password)
    days = plan_days(plan)
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)

    sb_upsert(
        "users",
        {
            "username": username,
            "phone": phone,
            "password_hash": password_hash,
            "status": "active",
            "plan": plan,
            "license_expiry": expiry,
            "device_id": device_id or "",
            "token": token,
            "must_change_password": True,
            "created_at": utc_now(),
            "last_login": None,
        },
        "username",
    )
    return {"username": username, "temporary_password": temp_password, "license_expiry": expiry, "token": token}


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/")
def home() -> Dict[str, Any]:
    return {
        "status": "running",
        "service": "Tweet Pipeline Pro License Server",
        "storage": "supabase",
        "time": utc_now(),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    require_supabase()
    # Verifies that tables exist.
    settings_ok = sb_select("settings", {"select": "key", "limit": "1"})
    payments_ok = sb_select("payments", {"select": "id", "limit": "1"})
    users_ok = sb_select("users", {"select": "id", "limit": "1"})
    ensure_default_settings()
    return {
        "status": "ok",
        "supabase": "connected",
        "settings_table": True,
        "payments_table": True,
        "users_table": True,
        "time": utc_now(),
    }


@app.get("/api/public/config")
def public_config() -> Dict[str, Any]:
    ensure_default_settings()
    return {
        "weekly_price": plan_amount("weekly"),
        "monthly_price": plan_amount("monthly"),
        "lifetime_price": plan_amount("lifetime"),
        "plans": ["weekly", "monthly", "lifetime"],
    }


@app.post("/api/admin/settings")
def update_admin_settings(payload: Dict[str, Any], x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
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
    ensure_default_settings()
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
    payments = sb_select("payments", {"select": "*", "order": "id.desc", "limit": "50"})
    users = sb_select("users", {"select": "username,phone,plan,status,license_expiry,device_id,created_at", "order": "id.desc", "limit": "50"})

    def esc(x: Any) -> str:
        import html
        return html.escape(str(x or ""))

    setting_rows = "".join(
        f"<label>{esc(k)}</label><input name='{esc(k)}' value='{esc(v)}' {'type=password' if ('SECRET' in k or 'PASSKEY' in k or 'TOKEN' in k) else ''}>"
        for k, v in s.items()
    )
    payment_rows = "".join(
        f"<tr><td>{p.get('id')}</td><td>{esc(p.get('phone'))}</td><td>{p.get('amount')}</td><td>{esc(p.get('plan'))}</td><td>{esc(p.get('status'))}</td><td>{esc(p.get('result_desc'))}</td><td>{esc(p.get('mpesa_receipt'))}</td><td>{esc(p.get('username'))}</td><td>{esc(p.get('created_at'))}</td></tr>"
        for p in payments
    )
    user_rows = "".join(
        f"<tr><td>{esc(u.get('username'))}</td><td>{esc(u.get('phone'))}</td><td>{esc(u.get('plan'))}</td><td>{esc(u.get('status'))}</td><td>{esc(u.get('license_expiry'))}</td><td>{esc(u.get('device_id'))}</td></tr>"
        for u in users
    )

    return f"""
    <html><head><title>TPP License Server Admin</title>
    <style>
    body{{font-family:Segoe UI,Arial;background:#0f172a;color:#e5e7eb;padding:24px}}
    .card{{background:#111827;border-radius:14px;padding:18px;margin:12px 0;box-shadow:0 10px 30px #0004}}
    label{{display:block;margin-top:10px;color:#93c5fd;font-weight:600}}
    input{{width:100%;padding:9px;border-radius:8px;border:1px solid #334155;background:#020617;color:#e5e7eb}}
    button{{padding:10px 16px;border:0;border-radius:8px;background:#38bdf8;font-weight:bold;margin-top:12px;cursor:pointer}}
    table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #334155;padding:7px;font-size:12px;text-align:left}}
    code{{background:#020617;padding:3px 6px;border-radius:6px}}
    </style></head>
    <body><h1>Tweet Pipeline Pro License Server Admin</h1>
    <div class='card'><h2>Payment / Daraja Settings</h2>
    <form method='post' action='/admin/save?token={quote(token)}'>{setting_rows}<button>Save Settings</button></form>
    <p>Callback URL: <code>{esc(s.get('DARAJA_CALLBACK_URL'))}</code></p></div>
    <div class='card'><h2>Recent Payments</h2><table><tr><th>ID</th><th>Phone</th><th>Amount</th><th>Plan</th><th>Status</th><th>Result</th><th>Receipt</th><th>Username</th><th>Created</th></tr>{payment_rows}</table></div>
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
    ensure_default_settings()
    phone = normalize_phone(payload.phone)
    plan = (payload.plan or "monthly").lower()
    amount = int(payload.amount or plan_amount(plan))
    if amount < 1:
        raise HTTPException(status_code=400, detail="Amount must be at least 1 KES.")

    shortcode = setting("DARAJA_SHORTCODE", "174379")
    callback = setting("DARAJA_CALLBACK_URL")
    if not callback:
        raise HTTPException(status_code=400, detail="Callback URL is not configured.")

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
    response = requests.post(
        stk_url(),
        json=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=35,
    )
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
            "merchant_request_id": data.get("MerchantRequestID", ""),
            "checkout_request_id": data.get("CheckoutRequestID", ""),
            "status": "pending",
            "created_at": utc_now(),
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

    rows = sb_select("payments", {"select": "*", "checkout_request_id": f"eq.{checkout_id}", "limit": "1"})
    if not rows:
        # This is useful to diagnose callback arriving before/without stored request.
        sb_insert(
            "payments",
            {
                "phone": "unknown",
                "amount": 0,
                "plan": "unknown",
                "device_id": "",
                "merchant_request_id": stk.get("MerchantRequestID", ""),
                "checkout_request_id": checkout_id,
                "status": "callback_without_payment",
                "result_code": int(result_code or -1),
                "result_desc": result_desc or "Payment not found for callback",
                "created_at": utc_now(),
            },
        )
        return {"ResultCode": 0, "ResultDesc": "Callback stored without matching payment"}

    payment = rows[0]
    if int(result_code or -1) == 0:
        receipt = ""
        for item in stk.get("CallbackMetadata", {}).get("Item", []):
            if item.get("Name") == "MpesaReceiptNumber":
                receipt = str(item.get("Value", ""))

        creds = create_or_update_paid_user(payment["phone"], payment["plan"], payment.get("device_id") or "")
        sb_update(
            "payments",
            {"checkout_request_id": f"eq.{checkout_id}"},
            {
                "status": "paid",
                "result_code": int(result_code),
                "result_desc": result_desc,
                "mpesa_receipt": receipt,
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
    rows = sb_select("payments", {"select": "*", "checkout_request_id": f"eq.{checkout_request_id}", "limit": "1"})
    if not rows:
        raise HTTPException(status_code=404, detail="Payment not found")
    p = rows[0]
    if p["status"] == "paid":
        return {
            "status": "paid",
            "message": "Payment confirmed. Login details created.",
            "username": p.get("username"),
            "temporary_password": p.get("temp_password"),
            "plan": p.get("plan"),
            "amount": p.get("amount"),
            "mpesa_receipt": p.get("mpesa_receipt"),
        }
    if p["status"] in {"failed", "cancelled", "expired", "callback_without_payment"}:
        return {"status": p["status"], "message": p.get("result_desc") or "Payment failed/cancelled."}
    return {"status": "pending", "message": "Waiting for M-Pesa confirmation."}


@app.post("/api/auth/login")
def login(payload: LoginRequest) -> Dict[str, Any]:
    rows = sb_select("users", {"select": "*", "username": f"eq.{payload.username.strip()}", "limit": "1"})
    if not rows:
        raise HTTPException(status_code=401, detail="Invalid username/password or inactive account.")
    user = rows[0]
    if user.get("status") != "active" or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid username/password or inactive account.")
    if user.get("license_expiry"):
        exp = datetime.fromisoformat(str(user["license_expiry"]).replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            raise HTTPException(status_code=403, detail="License expired.")
    if user.get("device_id") and payload.device_id and user.get("device_id") != payload.device_id:
        raise HTTPException(status_code=403, detail="License is locked to another device.")
    if payload.device_id and not user.get("device_id"):
        sb_update("users", {"id": f"eq.{user['id']}"}, {"device_id": payload.device_id, "last_login": utc_now()})
    else:
        sb_update("users", {"id": f"eq.{user['id']}"}, {"last_login": utc_now()})

    return {
        "status": "success",
        "username": user.get("username"),
        "phone": user.get("phone"),
        "plan": user.get("plan"),
        "license_expiry": user.get("license_expiry"),
        "token": user.get("token"),
        "must_change_password": bool(user.get("must_change_password")),
    }


@app.post("/api/license/check")
def license_check(payload: LicenseCheckRequest) -> Dict[str, Any]:
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
    if user.get("device_id") and payload.device_id and user.get("device_id") != payload.device_id:
        raise HTTPException(status_code=403, detail="License locked to another device.")
    return {"status": "active", "username": user.get("username"), "plan": user.get("plan"), "license_expiry": user.get("license_expiry")}
