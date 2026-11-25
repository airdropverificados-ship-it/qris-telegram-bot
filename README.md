# Telegram Bot with QRIS Payments (Xendit) + USDT Balance

> A Telegram bot that issues dynamic QRIS payment codes via Xendit,
> confirms payment through a webhook, converts the amount to USDT at the
> CoinGecko rate and credits the user's in-bot balance. Python, SQLite,
> polling + webhook server, with a test suite. **Note:** QRIS payments only
> work in Indonesia, and collecting real money requires an Indonesian
> Xendit merchant account.

# What it does

1. The user asks `/pagar 50000` → the bot creates a dynamic QRIS QR code
   in Xendit and sends the image.
2. The user scans the QR with GoPay, OVO, DANA or their bank and pays.
3. Xendit notifies the webhook (`POST /webhook/qris`). Every order carries
   the Telegram id inside (`tg_<user_id>_<uuid8>`), so the bot knows exactly
   who paid, how much and with which app.
4. The bot converts the amount to USDT at the CoinGecko rate and credits
   the user's internal balance (`/balance`).

## ⚠️ Real requirements (read before getting excited)

- **QRIS is Indonesia-only.** Paying requires an Indonesian app (GoPay,
  OVO, DANA, Indonesian banks). Outside Indonesia nobody can pay these QRs.
- **Collecting real money requires a Xendit merchant account:**
  company registration in Indonesia + Indonesian bank account + KYC/KYB
  verification. Without that, the code only works in test/sandbox mode
  (with `xnd_development_...` keys and simulated payments). This
  repository includes no account and no keys.
- **The USDT balance is INTERNAL to the bot, not an on-chain transfer.**
  The real IDR stays in the merchant's Xendit account / Indonesian bank.
  Converting that IDR into real USDT in a wallet is a separate manual step
  (the gateway does not settle in crypto). The bot never sends USDT to any
  wallet: it only records an internal balance at the market rate at the
  moment of payment.
- The IDR/USDT rate comes from the free public CoinGecko API (no key). If
  it fails, the payment stays paid-but-unconverted and is retried with
  `/reintentar` or `/mispagos`. An invented amount is never credited.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN, XENDIT_API_KEY,
                       # XENDIT_CALLBACK_TOKEN, WEBHOOK_URL, ADMIN_ID
.venv/bin/python run.py
```

The bot serves via polling and the webhook listens on `WEBHOOK_PORT`
(8080 by default).

## Configure Xendit

1. In the Xendit dashboard: Settings → API Keys → create a secret key
   (test: `xnd_development_...`).
2. Enable QR Codes in Settings → Payment Methods.
3. In Settings → Callbacks → **QR code paid & refunded**: set your public
   webhook URL (`https://your-domain/webhook/qris`) and copy the
   **Verification Token** to `XENDIT_CALLBACK_TOKEN` in `.env`.
4. The webhook must be reachable from the internet: use a domain with
   HTTPS or, to test locally, a tunnel (ngrok, Cloudflare Tunnel).
   Without a public URL, Xendit cannot notify payments.

## Test in test mode (no real money)

1. Use the `xnd_development_...` key and the test callback URL.
2. `/pagar 50000` in the bot → you receive the QR.
3. Simulate the payment with the Xendit API (replace `YOUR_KEY` and the
   `reference_id` = the QR's order id):

```bash
curl https://api.xendit.co/qr_codes/tg_123456789_ab12cd34/payments/simulate \
  -X POST -u YOUR_KEY: -H "api-version: 2022-07-31" -d amount=50000
```

Xendit will send the real webhook to your URL and the bot will credit
the balance.

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome |
| `/pagar [amount]` | Generates a QRIS QR (1,500–10,000,000 IDR) |
| `/mispagos` | Lists your payments and refreshes their status |
| `/balance` | Your internal USDT balance + movements |
| `/reintentar` | Retries pending conversions |
| `/reembolsar` | (Admin only) reverses a payment and subtracts its balance |
| `/cancelar` | Cancels the current operation |
| `/help` | Help |

## Refunds

Xendit reports refunds through the same callback ("QR code paid &
refunded"). When the notice arrives, the bot reverses the balance that
payment had credited (recorded in the ledger) and notifies the user and
the admin. A second notice of the same refund does not subtract twice.
If the notice never arrives, the admin can reverse a payment by hand
with `/reembolsar <order_id>`.

## Architecture

```
run.py            start: bot (polling) + webhook (aiohttp)
qrisbot/
  config.py       environment variables (.env)
  db.py           SQLite: users, payments, ledger (audit)
  xendit.py       QR Codes API client (create QR, payments, simulate)
  rates.py        CoinGecko rate + conversion (rounds down)
  qrgen.py        qr_string -> PNG
  payments.py     logic: webhook, idempotency, conversion, notices
  webhook.py      POST /webhook/qris (verifies x-callback-token)
  bot.py          Telegram commands
tests/            unittest, no network (signature, duplicates, conversion)
```

Money: the balance is stored in micro-USDT (integer, 1 USDT = 1,000,000).
Floats are never used for money. Conversion rounds down.

## Security

- The webhook verifies the `x-callback-token` header before processing
  anything (401 on mismatch). Without this, anyone could fake a payment.
- Webhook hardening: per-minute and per-IP rate limit
  (`WEBHOOK_RATE_LIMIT_PER_MINUTE`, 120 by default) and an optional
  allowlist of IPs (`XENDIT_WEBHOOK_IPS`). In production (a
  `xnd_production_` key) the bot refuses to start unless `WEBHOOK_URL`
  is HTTPS.
- Idempotency: Xendit retries webhooks; a duplicate never credits twice
  (the UPDATE requires `status = 'pending'` and conversion requires
  `conversion_status != 'converted'`). The same holds for refunds.
- If the webhook amount does not match the order, the payment is marked
  failed, nothing is credited and the admin is notified. Ambiguous
  amounts (floats, weird text) are rejected without crediting.
- SQLite in WAL mode with a 10 s `busy_timeout`: the bot and the webhook
  write at the same time without locking.
- Text coming from outside (usernames, payment app names) is escaped
  before going into a Telegram HTML message.
- Maximum 5 pending QRs per user (anti-spam on creation).
- Keys only live in `.env` (never in the code).

External audit: the code passed an audit with Grok (22 Nov 2026). The 4
major points it found (SQLite without WAL, missing refunds, webhook
hardening, HTML escaping) are fixed and covered by tests
(`tests/test_grok_fixes.py`). The money math and idempotency came out
correct.

## Alternative: Midtrans

If you prefer another Indonesian gateway, Midtrans offers QRIS with its
Core API (`POST /v2/charge`, `payment_type: "qris"`) and HTTP
notifications verified with `signature_key` (SHA-512 of
`order_id + status_code + gross_amount + server_key`). This project's
structure (order with user id + verified webhook + conversion) works the
same by changing only `xendit.py` and the webhook verifier.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
# or, with the standard library only:
.venv/bin/python -m unittest discover -s tests -t . -v
```

53 tests, no network. Install the dependencies first
(`pip install -r requirements.txt`).

## License

MIT — see `LICENSE`.
