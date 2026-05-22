# Deploy this license server to Render

This folder is only the online license/M-Pesa server. Upload this folder to GitHub, then deploy it as a Python Web Service.

## Render settings

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn license_server:app --host 0.0.0.0 --port $PORT
```

## Environment variables to set on Render

```text
TPP_SERVER_ADMIN_TOKEN=choose-a-strong-admin-token
DARAJA_ENV=sandbox
```

You can add the Daraja details from the server admin page after deployment, or set them as environment variables if your code supports them.

After Render gives you a URL such as:

```text
https://tweetpipeline-license-server.onrender.com
```

Open:

```text
https://tweetpipeline-license-server.onrender.com/admin?token=YOUR_ADMIN_TOKEN
```

Set:

```text
DARAJA_CONSUMER_KEY=your key
DARAJA_CONSUMER_SECRET=your secret
DARAJA_SHORTCODE=174379
DARAJA_PASSKEY=sandbox passkey
DARAJA_CALLBACK_URL=https://tweetpipeline-license-server.onrender.com/api/mpesa/callback
DARAJA_ENV=sandbox
DEFAULT_AMOUNT=50
LICENSE_DAYS=30
```

Then in the desktop app payment settings use:

```text
LICENSE_SERVER_URL=https://tweetpipeline-license-server.onrender.com
M_PESA_CALLBACK_URL=https://tweetpipeline-license-server.onrender.com/api/mpesa/callback
```

Do not give this server folder or admin token to customers.
