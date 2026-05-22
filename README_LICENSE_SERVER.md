# Tweet Pipeline Pro License Server

This server handles M-Pesa STK Push, receives the Daraja callback, creates customer usernames/passwords, and validates logins.

## Install

```bat
cd license_server
pip install -r requirements_server.txt
```

## Run locally

```bat
run_server.bat
```

Open:

```text
http://127.0.0.1:8000/admin
```

The default admin token is:

```text
change-this-admin-token
```

Change it immediately in the admin page or by setting `TPP_SERVER_ADMIN_TOKEN`.

## Public callback

For sandbox testing, use ngrok:

```bat
ngrok http 8000
```

Then your callback is:

```text
https://YOUR-NGROK-DOMAIN.ngrok-free.app/api/mpesa/callback
```

Put that URL in the server admin page as `DARAJA_CALLBACK_URL`.

## Customer flow

1. Customer opens TweetPipelinePro.
2. Customer clicks Pay with M-Pesa.
3. Desktop app calls `/api/payment/start`.
4. Server sends STK Push through Daraja.
5. Daraja calls `/api/mpesa/callback` after payment.
6. Server creates username/password.
7. Desktop app polls `/api/payment/status/{checkout_id}` and shows login details.
