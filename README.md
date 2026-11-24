# Telegram Bot with QRIS Payments (Xendit) + USDT Balance

> **In English:** a Telegram bot that issues dynamic QRIS payment codes via
> Xendit, confirms payment through a webhook, converts the amount to USDT at
> the CoinGecko rate and credits the user's in-bot balance. Python, SQLite,
> polling + webhook server, with a test suite. The detailed documentation below
> is in Spanish. Note: QRIS payments only work in Indonesia, and collecting
> real money requires an Indonesian Xendit merchant account.

# Bot de Telegram con pagos QRIS (Xendit) + saldo USDT

Bot que genera códigos QR QRIS para cobrar en rupias indonesias (IDR)
y acredita el equivalente en USDT en el saldo interno del usuario.

## Qué hace

1. El usuario pide `/pagar 50000` → el bot crea un QR dinámico QRIS
   en Xendit y envía la imagen.
2. El usuario escanea el QR con GoPay, OVO, DANA o su banco y paga.
3. Xendit avisa al webhook (`POST /webhook/qris`). Cada orden lleva
   el id de Telegram dentro (`tg_<user_id>_<uuid8>`), así que el bot
   sabe exactamente quién pagó, cuánto y con qué app.
4. El bot convierte el importe a USDT con la tasa de CoinGecko y lo
   acredita en el saldo interno del usuario (`/balance`).

## ⚠️ Requisitos reales (léelo antes de ilusionarte)

- **QRIS es solo para Indonesia.** Pagar exige una app indonesia
  (GoPay, OVO, DANA, bancos indonesios). Fuera de Indonesia nadie
  puede pagar estos QR.
- **Cobrar dinero real exige una cuenta de comercio (merchant) en
  Xendit**: registro de empresa en Indonesia + cuenta de banco
  indonesia + verificación KYC/KYB. Sin eso, el código solo funciona
  en modo test/sandbox (con claves `xnd_development_...` y pagos
  simulados). Este repositorio no incluye ninguna cuenta ni clave.
- **El saldo USDT es INTERNO del bot, no una transferencia on-chain.**
  El IDR real queda en la cuenta de Xendit / banco indonesio del
  comercio. Convertir ese IDR en USDT real en una billetera es un
  paso manual aparte (la pasarela no liquida en cripto). El bot no
  envía USDT a ninguna billetera: solo anota un saldo interno a la
  tasa de mercado del momento del pago.
- La tasa IDR/USDT viene de la API pública gratuita de CoinGecko
  (sin clave). Si falla, el pago queda pagado-pero-sin-convertir y
  se reintenta con `/reintentar` o `/mispagos`. Nunca se acredita
  un importe inventado.

## Instalación

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # rellena BOT_TOKEN, XENDIT_API_KEY,
                       # XENDIT_CALLBACK_TOKEN, WEBHOOK_URL, ADMIN_ID
.venv/bin/python run.py
```

El bot atiende por polling y el webhook escucha en el puerto
`WEBHOOK_PORT` (8080 por defecto).

## Configurar Xendit

1. En el panel de Xendit: Settings → API Keys → crea una clave
   secreta (test: `xnd_development_...`).
2. Activa QR Codes en Settings → Payment Methods.
3. En Settings → Callbacks → **QR code paid & refunded**: pon la
   URL pública de tu webhook (`https://tu-dominio/webhook/qris`) y
   copia el **Verification Token** a `XENDIT_CALLBACK_TOKEN` en `.env`.
4. El webhook debe ser accesible desde internet: usa un dominio con
   HTTPS o, para probar en local, un túnel (ngrok, Cloudflare Tunnel).
   Sin URL pública, Xendit no puede avisar de los pagos.

## Probar en modo test (sin dinero real)

1. Usa la clave `xnd_development_...` y la callback URL de test.
2. `/pagar 50000` en el bot → recibes el QR.
3. Simula el pago con la API de Xendit (sustituye `TU_CLAVE` y el
   `reference_id` = order_id del QR):

```bash
curl https://api.xendit.co/qr_codes/tg_123456789_ab12cd34/payments/simulate \
  -X POST -u TU_CLAVE: -H "api-version: 2022-07-31" -d amount=50000
```

Xendit enviará el webhook real a tu URL y el bot acreditará el saldo.

## Comandos

| Comando | Qué hace |
|---|---|
| `/start` | Bienvenida |
| `/pagar [importe]` | Genera un QR QRIS (1.500–10.000.000 IDR) |
| `/mispagos` | Lista tus pagos y actualiza su estado |
| `/balance` | Tu saldo interno en USDT + movimientos |
| `/reintentar` | Reintenta conversiones pendientes |
| `/reembolsar` | (Solo admin) revierte un pago y resta su saldo |
| `/cancelar` | Cancela la operación actual |
| `/help` | Ayuda |

## Reembolsos

Xendit avisa de los reembolsos por el mismo callback
("QR code paid & refunded"). Cuando llega el aviso, el bot revierte
el saldo que ese pago había acreditado (con constancia en el ledger)
y avisa al usuario y al admin. Un segundo aviso del mismo reembolso
no resta dos veces. Si el aviso no llega, el admin puede revertir
un pago a mano con `/reembolsar <order_id>`.

## Arquitectura

```
run.py            arranque: bot (polling) + webhook (aiohttp)
qrisbot/
  config.py       variables de entorno (.env)
  db.py           SQLite: users, payments, ledger (auditoría)
  xendit.py       cliente API QR Codes (crear QR, pagos, simular)
  rates.py        tasa CoinGecko + conversión (redondeo hacia abajo)
  qrgen.py        qr_string -> PNG
  payments.py     lógica: webhook, idempotencia, conversión, avisos
  webhook.py      POST /webhook/qris (verifica x-callback-token)
  bot.py          comandos de Telegram
tests/            unittest sin red (firma, duplicados, conversión)
```

Dinero: el saldo se guarda en micro-USDT (entero, 1 USDT = 1.000.000).
Nunca se usa float para dinero. La conversión redondea hacia abajo.

## Seguridad

- El webhook verifica la cabecera `x-callback-token` antes de
  procesar nada (401 si no coincide). Sin esto cualquiera podría
  falsificar un pago.
- Endurecimiento del webhook: límite de peticiones por minuto y por
  IP (`WEBHOOK_RATE_LIMIT_PER_MINUTE`, 120 por defecto) y lista
  opcional de IPs permitidas (`XENDIT_WEBHOOK_IPS`). En producción
  (clave `xnd_production_`) el bot no arranca si `WEBHOOK_URL` no
  es HTTPS.
- Idempotencia: Xendit reintenta los webhooks; un duplicado no
  acredita dos veces (el UPDATE exige `status = 'pending'` y la
  conversión exige `conversion_status != 'converted'`). Lo mismo
  vale para los reembolsos.
- Si el importe del webhook no coincide con el pedido, el pago se
  marca fallido, no se acredita nada y se avisa al admin. Los
  importes ambiguos (float, texto raro) se rechazan sin acreditar.
- SQLite en modo WAL con `busy_timeout` de 10 s: el bot y el
  webhook escriben a la vez sin bloqueos.
- Los textos que vienen de fuera (nombres de usuario, app de pago)
  se escapan antes de ir a un mensaje HTML de Telegram.
- Máximo 5 QR pendientes por usuario (anti-spam de creación).
- Las claves solo viven en `.env` (nunca en el código).

Auditoría externa: el código pasó una auditoría con Grok
(22 nov 2026). Los 4 puntos mayores que encontró (SQLite sin WAL,
falta de reembolsos, endurecimiento del webhook, escape HTML)
están corregidos y cubiertos por tests (`tests/test_grok_fixes.py`).
La matemática del dinero y la idempotencia salieron correctas.

## Alternativa: Midtrans

Si prefieres otro gateway indonesio, Midtrans ofrece QRIS con su
Core API (`POST /v2/charge`, `payment_type: "qris"`) y notificaciones
HTTP verificadas con `signature_key` (SHA-512 de
`order_id + status_code + gross_amount + server_key`). La estructura
de este proyecto (orden con user id + webhook verificado + conversión)
sirve igual cambiando solo `xendit.py` y el verificador del webhook.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```
