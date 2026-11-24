"""Lógica de negocio de los pagos QRIS.

Este módulo no sabe nada de Telegram ni de HTTP: recibe un payload de
webhook (o un pago consultado a Xendit) y decide qué hacer con la base
de datos y qué notificaciones enviar. Así se puede probar sin red.

Flujo de un pago:
1. /pagar crea una orden `tg_<user_id>_<uuid8>` y un QR dinámico en
   Xendit con ese id como reference_id.
2. El usuario escanea el QR con GoPay, OVO, DANA o su banco y paga.
3. Xendit llama a POST /webhook/qris con `event: "qr.payment"` y
   `status: "COMPLETED"`. El reference_id del webhook identifica la
   orden y, por tanto, al usuario exacto que pagó.
4. El pago se marca pagado (idempotente: los reintentos de Xendit no
   duplican nada), se convierte IDR -> USDT con la tasa de CoinGecko
   y se acredita el saldo interno en USDT del usuario.
"""

from __future__ import annotations

import hmac
import html
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from . import rates
from .config import Config
from .db import Database
from .rates import RateError
from .xendit import XenditClient, XenditError

log = logging.getLogger(__name__)

# Máximo de códigos QR pendientes por usuario: evita que alguien
# spamee la creación de QR (cada uno es una llamada a Xendit).
MAX_PENDING_PER_USER = 5

# Estados de webhook que Xendit puede enviar para un reembolso.
REFUND_STATUSES = {"REFUNDED", "REFUND_COMPLETED", "REFUND"}

# tg_<telegram_user_id>_<8 hex>. El id de usuario dentro de la orden es
# lo que permite reconocer de dónde viene cada pago.
ORDER_RE = re.compile(r"^tg_(\d+)_([0-9a-f]{8})$")

Notifier = Callable[[int, str], Awaitable[None]]
RateProvider = Callable[[], Awaitable[Decimal]]


def new_order_id(telegram_user_id: int) -> str:
    """Id de orden único que lleva dentro el id de Telegram del pagador."""
    return f"tg_{telegram_user_id}_{uuid.uuid4().hex[:8]}"


def parse_order_id(order_id: str) -> int | None:
    """Extrae el telegram_user_id de una orden, o None si no es válida."""
    match = ORDER_RE.match(order_id or "")
    return int(match.group(1)) if match else None


def esc(value: Any) -> str:
    """Escapa un valor para interpolarlo en un mensaje HTML de Telegram.

    Los nombres de usuario, la app de pago y otros campos que vienen
    de fuera podrían contener <, > o & e inyectar HTML en el mensaje.
    """
    return html.escape(str(value), quote=False)


def _parse_webhook_amount(amount: Any) -> int | None:
    """Importe del webhook: entero estricto, o None si no vale.

    Acepta un int o una cadena de solo dígitos. Rechaza float,
    booleanos y cadenas raras ("12.5", "1e5"): un importe ambiguo
    nunca debe acreditarse.
    """
    if isinstance(amount, bool):
        return None
    if isinstance(amount, int):
        return amount if amount >= 0 else None
    if isinstance(amount, str):
        cleaned = amount.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def verify_callback_token(provided: str | None, expected: str) -> bool:
    """Compara el token del webhook con el configurado (tiempo constante).

    Sin token o con token distinto -> False. NUNCA se confía en un
    webhook sin verificar: cualquiera podría falsificar un pago.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def extract_order_id(payload: dict) -> str | None:
    """reference_id (API 2022-07-31) o external_id (versión anterior)."""
    qr_code = payload.get("qr_code") or {}
    return qr_code.get("reference_id") or qr_code.get("external_id")


def extract_payer_source(payload: dict) -> str | None:
    """App/banco con el que pagó el usuario (GOPAY, OVO, BCA, ...)."""
    detail = payload.get("payment_detail") or {}
    return detail.get("source")


def extract_receipt_id(payload: dict) -> str | None:
    detail = payload.get("payment_detail") or {}
    return detail.get("receipt_id")


async def _noop_notifier(telegram_user_id: int, text: str) -> None:
    log.info("Notificación (sin bot) a %s: %s", telegram_user_id,
             text.splitlines()[0] if text else "")


class PaymentsService:
    """Orquesta Xendit + base de datos + notificaciones."""

    def __init__(self, db: Database, config: Config,
                 xendit: XenditClient | None = None,
                 notifier: Notifier | None = None,
                 rate_provider: RateProvider | None = None):
        self.db = db
        self.config = config
        self.xendit = xendit
        self.notifier = notifier or _noop_notifier
        self.rate_provider = (
            rate_provider
            or (lambda: rates.fetch_idr_per_usdt(config.coingecko_url))
        )

    # ---------------- crear un pago ----------------

    async def create_payment(self, telegram_user_id: int,
                             username: str | None,
                             amount_idr: int) -> dict:
        """Crea la orden en Xendit y la guarda como pendiente.

        Devuelve la fila del pago (incluye qr_string para el PNG).
        Lanza ValueError (importe fuera de rango) o XenditError.
        """
        if self.xendit is None:
            raise RuntimeError("Cliente Xendit no configurado")
        self.db.ensure_user(telegram_user_id, username)
        pending = self.db.get_pending_payments(telegram_user_id)
        if len(pending) >= MAX_PENDING_PER_USER:
            raise ValueError(
                f"Ya tienes {MAX_PENDING_PER_USER} pagos pendientes. "
                "Paga o espera a que venzan antes de crear otro QR.")
        order_id = new_order_id(telegram_user_id)
        qr = await self.xendit.create_dynamic_qr(
            reference_id=order_id, amount_idr=amount_idr,
            metadata={"telegram_user_id": str(telegram_user_id),
                      "username": username or ""})
        try:
            payment = self.db.create_payment(
                order_id=order_id, telegram_user_id=telegram_user_id,
                username=username, amount_idr=amount_idr,
                qr_code_id=qr.get("id"), qr_string=qr.get("qr_string"),
                expires_at=qr.get("expires_at"))
        except Exception:
            # El QR ya existe en Xendit pero no quedó en la BD: no
            # tragarse el error en silencio, para que el QR huérfano
            # se vea en los logs y se pueda desactivar a mano.
            log.error("QR huérfano en Xendit: orden %s creada pero no "
                      "guardada en la BD (qr_code_id=%s)",
                      order_id, qr.get("id"))
            raise
        log.info("Orden creada: %s (%s IDR) usuario %s", order_id,
                 amount_idr, telegram_user_id)
        return payment

    # ---------------- webhook ----------------

    async def handle_webhook(self, payload: dict,
                             callback_token: str | None
                             ) -> tuple[int, dict]:
        """Procesa un webhook de Xendit. Devuelve (HTTP status, body).

        401 si el token no verifica. Cualquier otro resultado devuelve
        200 para que Xendit no reintente: un webhook duplicado o de una
        orden desconocida no es un error del servidor.
        """
        if not verify_callback_token(callback_token,
                                     self.config.xendit_callback_token):
            log.warning("Webhook rechazado: token de callback inválido")
            return 401, {"error": "invalid callback token"}
        result = await self.process_qr_payment(payload)
        return 200, result

    async def process_qr_payment(self, payload: dict) -> dict:
        """Procesa un pago QRIS (webhook o consulta a Xendit).

        Devuelve un dict con `outcome`:
        paid | paid_unconverted | duplicate | unknown_order |
        amount_mismatch | not_completed | invalid |
        refunded | duplicate_refund (los reembolsos se derivan a
        process_refund).
        """
        raw_event = payload.get("event")
        # Reembolsos: Xendit los avisa por este mismo callback
        # ("QR code paid & refunded"). Se detectan por el nombre del
        # evento o por el estado, y van por su propio camino.
        if ("refund" in str(raw_event or "").lower()
                or payload.get("status") in REFUND_STATUSES):
            return await self.process_refund(payload)
        if raw_event is not None and raw_event != "qr.payment":
            return {"outcome": "invalid",
                    "reason": f"evento no esperado: {raw_event}"}
        if payload.get("status") != "COMPLETED":
            return {"outcome": "not_completed",
                    "status": payload.get("status")}
        order_id = extract_order_id(payload)
        if not order_id:
            # Solo las claves del payload, nunca el contenido entero.
            log.warning("Webhook sin reference_id/external_id "
                        "(claves: %s)", sorted(payload.keys()))
            return {"outcome": "invalid",
                    "reason": "falta reference_id en qr_code"}
        payment = self.db.get_payment(order_id)
        if payment is None:
            log.warning("Webhook de orden desconocida: %s", order_id)
            return {"outcome": "unknown_order", "order_id": order_id}

        raw_amount = payload.get("amount")
        amount = _parse_webhook_amount(raw_amount)
        if raw_amount is not None and amount is None:
            # Importe ambiguo (float, texto raro): no acreditar.
            log.warning("Webhook con importe no válido en %s: %r",
                        order_id, raw_amount)
            return {"outcome": "invalid",
                    "reason": "importe no válido"}
        if amount is not None and amount != payment["amount_idr"]:
            # El importe no coincide con lo pedido: NO acreditar.
            self.db.mark_failed(order_id=order_id, raw_webhook=payload)
            log.error("Importe distinto en %s: pedido %s, recibido %s",
                      order_id, payment["amount_idr"], amount)
            await self._notify_admin(
                "⚠️ <b>Pago con importe distinto</b>\n"
                f"Orden: <code>{esc(order_id)}</code>\n"
                f"Pedido: {rates.format_idr(payment['amount_idr'])}\n"
                f"Recibido: {rates.format_idr(amount)}\n"
                "No se acreditó nada. Revisa el caso a mano.")
            return {"outcome": "amount_mismatch", "order_id": order_id,
                    "expected_idr": payment["amount_idr"],
                    "received_idr": amount}

        marked = self.db.mark_paid(
            order_id=order_id,
            gateway_payment_id=payload.get("id"),
            payer_source=extract_payer_source(payload),
            receipt_id=extract_receipt_id(payload),
            raw_webhook=payload)
        if not marked:
            # Ya estaba pagado: reintento de Xendit. No hacer nada más.
            log.info("Webhook duplicado ignorado: %s", order_id)
            return {"outcome": "duplicate", "order_id": order_id}

        payment = self.db.get_payment(order_id)
        assert payment is not None
        source = payment.get("payer_source") or "QRIS"
        try:
            converted, rate, micro = await self._convert_and_credit(
                payment)
        except RateError as exc:
            # Pagado pero SIN convertir: no inventar tasa ni acreditar.
            self.db.mark_conversion_failed(order_id)
            log.error("Conversión fallida en %s: %s", order_id, exc)
            await self._notify_user(
                payment["telegram_user_id"],
                "✅ <b>Pago recibido</b>: "
                f"{rates.format_idr(payment['amount_idr'])} "
                f"por {esc(source)}.\n"
                "⏳ La conversión a USDT está pendiente: no se pudo "
                "consultar la tasa en este momento.\n"
                "Tu pago está registrado y no se pierde. Usa "
                "/reintentar en unos minutos.")
            await self._notify_admin(
                "💰 <b>Pago QRIS pagado (conversión pendiente)</b>\n"
                f"Orden: <code>{esc(order_id)}</code>\n"
                f"Usuario: {payment['telegram_user_id']} "
                f"(@{esc(payment.get('username') or '-')})\n"
                f"Importe: {rates.format_idr(payment['amount_idr'])} "
                f"por {esc(source)}\n"
                f"Motivo: {esc(exc)}")
            return {"outcome": "paid_unconverted", "order_id": order_id,
                    "reason": str(exc)}

        balance = self.db.get_balance_micro(payment["telegram_user_id"])
        await self._notify_user(
            payment["telegram_user_id"],
            "✅ <b>Pago recibido</b>: "
            f"{rates.format_idr(payment['amount_idr'])} "
            f"por {esc(source)}.\n"
            f"💱 Convertido a {rates.format_usdt(micro)} "
            f"(tasa {rate:,} IDR/USDT).\n"
            f"💰 Tu saldo ahora es de {rates.format_usdt(balance)}.")
        await self._notify_admin(
            "💰 <b>Pago QRIS pagado</b>\n"
            f"Orden: <code>{esc(order_id)}</code>\n"
            f"Usuario: {payment['telegram_user_id']} "
            f"(@{esc(payment.get('username') or '-')})\n"
            f"Importe: {rates.format_idr(payment['amount_idr'])} "
            f"por {esc(source)}\n"
            f"Acreditado: {rates.format_usdt(micro)} "
            f"(tasa {rate:,} IDR/USDT)")
        log.info("Pago acreditado: %s -> %s micro-USDT (tasa %s)",
                 order_id, micro, rate)
        return {"outcome": "paid", "order_id": order_id,
                "usdt_micro": micro, "rate_idr_per_usdt": str(rate),
                "converted": converted}

    async def process_refund(self, payload: dict) -> dict:
        """Procesa un reembolso avisado por Xendit.

        Revierte el saldo interno que el pago había acreditado
        (o solo marca la orden si aún no estaba convertido).
        Idempotente: un segundo aviso del mismo reembolso no resta
        dos veces.
        """
        order_id = extract_order_id(payload)
        if not order_id:
            log.warning("Refund sin reference_id/external_id "
                        "(claves: %s)", sorted(payload.keys()))
            return {"outcome": "invalid",
                    "reason": "falta reference_id en qr_code"}
        return await self.refund_payment(order_id, via="webhook")

    async def refund_payment(self, order_id: str,
                             via: str = "manual") -> dict:
        """Revierte un pago: resta el saldo acreditado y avisa.

        `via` es "webhook" (aviso de Xendit) o "manual" (/reembolsar).
        """
        payment = self.db.get_payment(order_id)
        if payment is None:
            log.warning("Reembolso de orden desconocida: %s", order_id)
            return {"outcome": "unknown_order", "order_id": order_id}
        result = self.db.record_refund(order_id)
        if result is None:
            return {"outcome": "duplicate_refund",
                    "order_id": order_id}
        user_id = result["telegram_user_id"]
        reversed_micro = result["usdt_reversed_micro"]
        balance = self.db.get_balance_micro(user_id)
        if result["was_converted"]:
            await self._notify_user(
                user_id,
                "↩️ <b>Reembolso</b>\n"
                f"Orden: <code>{esc(order_id)}</code>\n"
                f"Se devolvieron {rates.format_usdt(reversed_micro)} "
                "de tu saldo.\n"
                f"💰 Tu saldo ahora es de "
                f"{rates.format_usdt(balance)}.")
        else:
            await self._notify_user(
                user_id,
                "↩️ <b>Reembolso</b>\n"
                f"Orden: <code>{esc(order_id)}</code>\n"
                "El pago fue reembolsado antes de convertirse: no "
                "se había acreditado nada.")
        await self._notify_admin(
            "↩️ <b>Reembolso procesado</b>\n"
            f"Orden: <code>{esc(order_id)}</code>\n"
            f"Usuario: {user_id}\n"
            f"Importe: {rates.format_idr(result['amount_idr'])}\n"
            f"Revertido: {rates.format_usdt(reversed_micro)} "
            f"(vía {esc(via)})")
        log.info("Reembolso: %s -> -%s micro-USDT (vía %s)",
                 order_id, reversed_micro, via)
        return {"outcome": "refunded", "order_id": order_id,
                "usdt_reversed_micro": reversed_micro,
                "was_converted": result["was_converted"]}

    async def _convert_and_credit(self, payment: dict
                                  ) -> tuple[bool, Decimal, int]:
        """Convierte el pago a USDT y lo acredita (una sola vez).

        Devuelve (convertido, tasa, micro-USDT). Lanza RateError si no
        se pudo obtener la tasa.
        """
        rate = await self.rate_provider()
        micro = rates.idr_to_micro_usdt(payment["amount_idr"], rate)
        converted = self.db.record_conversion(
            order_id=payment["order_id"],
            telegram_user_id=payment["telegram_user_id"],
            amount_idr=payment["amount_idr"],
            rate_idr_per_usdt=str(rate),
            usdt_micro=micro)
        return converted, rate, micro

    # ---------------- reintentos y actualización ----------------

    async def retry_pending_conversions(
            self, telegram_user_id: int) -> list[dict]:
        """Reintenta convertir los pagos pagados sin convertir del usuario.

        Lo usa /reintentar (y /mispagos). Devuelve un resultado por pago.
        """
        results = []
        for payment in self.db.get_paid_unconverted(telegram_user_id):
            try:
                converted, rate, micro = await self._convert_and_credit(
                    payment)
            except RateError as exc:
                log.error("Reintento de conversión fallido en %s: %s",
                          payment["order_id"], exc)
                results.append({"order_id": payment["order_id"],
                                "outcome": "rate_failed",
                                "reason": str(exc)})
                continue
            if converted:
                balance = self.db.get_balance_micro(telegram_user_id)
                await self._notify_user(
                    telegram_user_id,
                    "✅ <b>Conversión completada</b>\n"
                    f"Orden: <code>{esc(payment['order_id'])}</code>\n"
                    f"{rates.format_idr(payment['amount_idr'])} → "
                    f"{rates.format_usdt(micro)} "
                    f"(tasa {rate:,} IDR/USDT).\n"
                    f"💰 Tu saldo ahora es de "
                    f"{rates.format_usdt(balance)}.")
                results.append({"order_id": payment["order_id"],
                                "outcome": "converted",
                                "usdt_micro": micro,
                                "rate_idr_per_usdt": str(rate)})
            else:
                results.append({"order_id": payment["order_id"],
                                "outcome": "already_converted"})
        return results

    async def refresh_user_payments(self, telegram_user_id: int) -> dict:
        """Actualiza los pagos pendientes del usuario consultando Xendit.

        - Marca expirados los que pasaron su expires_at.
        - Consulta los pagos de cada QR pendiente: si Xendit ya tiene
          un pago COMPLETED, lo procesa como si llegara el webhook.
        - Reintenta las conversiones pendientes.
        """
        summary: dict[str, Any] = {"checked": 0, "paid_now": [],
                                   "expired_now": [], "converted": []}
        if self.xendit is None:
            summary["error"] = "Cliente Xendit no configurado"
            return summary
        now = datetime.now(timezone.utc)
        for payment in self.db.get_pending_payments(telegram_user_id):
            summary["checked"] += 1
            expires_at = payment.get("expires_at")
            if expires_at:
                try:
                    exp = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00"))
                    if now > exp:
                        self.db.mark_expired(payment["order_id"])
                        summary["expired_now"].append(
                            payment["order_id"])
                        continue
                except ValueError:
                    pass  # fecha inválida: se sigue consultando a Xendit
            try:
                payments = await self.xendit.get_payments(
                    payment["order_id"])
            except XenditError as exc:
                log.warning("No se pudo consultar %s: %s",
                            payment["order_id"], exc)
                continue
            for item in payments:
                if item.get("status") == "COMPLETED":
                    result = await self.process_qr_payment(item)
                    if result.get("outcome") in ("paid",
                                                "paid_unconverted"):
                        summary["paid_now"].append(payment["order_id"])
                    break
        retries = await self.retry_pending_conversions(telegram_user_id)
        summary["converted"] = [
            r["order_id"] for r in retries
            if r.get("outcome") == "converted"]
        return summary

    # ---------------- notificaciones ----------------

    async def _notify_user(self, telegram_user_id: int, text: str) -> None:
        try:
            await self.notifier(telegram_user_id, text)
        except Exception as exc:  # noqa: BLE001 - notificar no puede romper el pago
            log.error("No se pudo avisar al usuario %s: %s",
                      telegram_user_id, exc)

    async def _notify_admin(self, text: str) -> None:
        admin_id = self.config.admin_id
        if admin_id:
            await self._notify_user(admin_id, text)
