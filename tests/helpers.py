"""Ayudas compartidas por los tests (sin red, sin Telegram real)."""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal

from qrisbot.config import Config
from qrisbot.db import Database
from qrisbot.payments import PaymentsService, new_order_id
from qrisbot.rates import RateError

CALLBACK_TOKEN = "token-de-prueba"
ADMIN_ID = 999
USER_ID = 2018620717


def make_config() -> Config:
    return Config(
        bot_token="token-falso",
        xendit_api_key="xnd_development_falso",
        xendit_callback_token=CALLBACK_TOKEN,
        webhook_url="https://ejemplo.test/webhook/qris",
        admin_id=ADMIN_ID,
    )


def make_service(rate: str | None = "16500",
                 rate_error: bool = False):
    """Servicio con BD temporal, notificador falso y tasa fija.

    Devuelve (service, db, notificaciones, tmpdir). La tasa fija es
    16.500 IDR/USDT salvo que se pida error de tasa.
    """
    tmpdir = tempfile.mkdtemp(prefix="qris-test-")
    db = Database(os.path.join(tmpdir, "test.db"))
    notifications: list[tuple[int, str]] = []

    async def fake_notifier(user_id: int, text: str) -> None:
        notifications.append((user_id, text))

    async def fixed_rate() -> Decimal:
        if rate_error:
            raise RateError("sin red (prueba)")
        return Decimal(rate or "16500")

    service = PaymentsService(db=db, config=make_config(),
                              notifier=fake_notifier,
                              rate_provider=fixed_rate)
    return service, db, notifications, tmpdir


def make_pending_payment(service: PaymentsService, user_id: int = USER_ID,
                         amount_idr: int = 50000) -> dict:
    """Crea un pago pendiente directamente en la BD (sin llamar a Xendit)."""
    service.db.ensure_user(user_id, "usuario_prueba")
    order_id = new_order_id(user_id)
    return service.db.create_payment(
        order_id=order_id, telegram_user_id=user_id,
        username="usuario_prueba", amount_idr=amount_idr,
        qr_code_id="qr_test123", qr_string="0002010102QRIS-DE-PRUEBA",
        expires_at="2030-01-01T00:00:00Z")


def webhook_payload(order_id: str, amount_idr: int = 50000,
                    status: str = "COMPLETED",
                    source: str = "GOPAY") -> dict:
    """Payload de webhook con la forma exacta de la doc de Xendit."""
    return {
        "event": "qr.payment",
        "id": "qrpy_test123",
        "amount": amount_idr,
        "created": "2026-11-22T07:00:00.000Z",
        "qr_code": {
            "id": "qr_test123",
            "reference_id": order_id,
            "type": "DYNAMIC",
        },
        "status": status,
        "payment_detail": {
            "receipt_id": "123456",
            "source": source,
        },
    }
