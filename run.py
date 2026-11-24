"""Punto de entrada: bot de Telegram (polling) + servidor webhook.

Uso:
    cp .env.example .env   # y rellena tus claves
    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python run.py

El bot atiende por polling y el servidor aiohttp recibe los webhooks
de Xendit en POST /webhook/qris (puerto WEBHOOK_PORT, por defecto 8080).
La URL pública del webhook (WEBHOOK_URL) debe configurarse en el panel
de Xendit: Settings -> Callbacks -> "QR code paid & refunded".
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from qrisbot.bot import build_application
from qrisbot.config import load_config
from qrisbot.db import Database
from qrisbot.payments import PaymentsService
from qrisbot.webhook import start_webhook_server
from qrisbot.xendit import XenditClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def sanitize_proxy_env() -> None:
    """httpx (usado por python-telegram-bot) interpreta NO_PROXY como
    patrones URL y se rompe con entradas IPv6 entre corchetes
    (InvalidURL). Se deja un NO_PROXY mínimo y seguro; los proxies
    HTTP(S) del entorno se respetan tal cual."""
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"


async def main() -> None:
    sanitize_proxy_env()
    try:
        config = load_config()
    except ValueError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc

    db = Database(config.db_path)
    xendit = XenditClient(api_key=config.xendit_api_key,
                          base_url=config.xendit_base_url,
                          api_version=config.xendit_api_version)
    service = PaymentsService(db=db, config=config, xendit=xendit)
    app = build_application(config, service)

    stop_event = asyncio.Event()

    def _request_stop(*_args: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_a: _request_stop())

    log.info("Arrancando bot de Telegram (polling)...")
    await app.initialize()
    await app.start()
    assert app.updater is not None
    await app.updater.start_polling(drop_pending_updates=True)
    me = await app.bot.get_me()
    log.info("Bot en línea como @%s", me.username)

    runner = await start_webhook_server(service, config)
    log.info("Todo en marcha. Pulsa Ctrl+C para parar.")

    await stop_event.wait()
    log.info("Parando...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await runner.cleanup()
    await xendit.close()
    db.close()
    log.info("Parado.")


if __name__ == "__main__":
    asyncio.run(main())
