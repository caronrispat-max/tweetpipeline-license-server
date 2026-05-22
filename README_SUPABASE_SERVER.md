# Tweet Pipeline Pro License Server - Supabase Version

This version fixes the issue where STK Push succeeds but login details do not appear on Vercel. It stores payments and users permanently in Supabase instead of temporary `/tmp` SQLite.

## 1. Create Supabase tables

Open Supabase → SQL Editor → New Query.
Paste all contents of `supabase_schema.sql` and run it.

## 2. Get Supabase credentials

Open Supabase → Project Settings → API.
Copy:

- Project URL → `SUPABASE_URL`
- service_role key → `SUPABASE_SERVICE_ROLE_KEY`

Keep the service role key private. Never put it in the customer EXE.

## 3. Replace files in GitHub repo

Upload/replace these files in your GitHub repo:

- `license_server.py`
- `app.py`
- `requirements.txt`
- `pyproject.toml`
- `vercel.json`

## 4. Add Vercel Environment Variables

In Vercel → Project → Settings → Environment Variables, add/update:

```text
SUPABASE_URL = your Supabase project URL
SUPABASE_SERVICE_ROLE_KEY = your Supabase service_role key
DARAJA_ENV = sandbox
DARAJA_CONSUMER_KEY = your Daraja consumer key
DARAJA_CONSUMER_SECRET = your Daraja consumer secret
DARAJA_SHORTCODE = 174379
DARAJA_PASSKEY = sandbox passkey
CALLBACK_URL = https://tweetpipeline-license-server.vercel.app/api/mpesa/callback
TPP_SERVER_ADMIN_TOKEN = your admin token
LICENSE_SERVER_ADMIN_TOKEN = same admin token
WEEKLY_PRICE = 1
MONTHLY_PRICE = 1
LIFETIME_PRICE = 1
LICENSE_DAYS = 30
```

Then redeploy.

## 5. Test

Open:

```text
https://tweetpipeline-license-server.vercel.app/
```

Expected:

```json
{"status":"running","service":"Tweet Pipeline Pro License Server","storage":"supabase"}
```

Then open:

```text
https://tweetpipeline-license-server.vercel.app/health
```

Expected:

```json
{"status":"ok","supabase":"connected"}
```

## 6. Admin page

Open:

```text
https://tweetpipeline-license-server.vercel.app/admin?token=YOUR_TOKEN
```

You should see payments and users stored permanently from Supabase.

## 7. Customer flow

After customer pays:

- `payments.status` becomes `paid`
- `payments.username` is filled
- `payments.temp_password` is filled
- a row appears in `users`
- desktop app displays username/password

