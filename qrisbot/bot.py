"""Handlers del bot de Telegram (python-telegram-bot v22, async).

Comandos (todo en español):
- /start      bienvenida y ayuda
- /pagar      genera un QR QRIS para pagar en IDR (opcional: /pagar 50000)
- /mispagos   lista tus pagos y actualiza su estado con Xendit
- /balance    tu saldo interno en USDT
- /reintentar reintenta convertir pagos pagados sin convertir
- /reembolsar (solo admin) revierte un pago y resta su saldo
- /cancelar   cancela la conversación actual (/pagar)
- /help       ayuda
"""

from __future__ import annotations

import io
import logging

from telegram import InputFile, Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)

from . import qrgen, rates
from .config import Config
from .db import (STATUS_EXPIRED, STATUS_FAILED, STATUS_PAID, STATUS_PENDING,
                 STATUS_REFUNDED)
from .payments import PaymentsService, esc
from .xendit import MAX_AMOUNT_IDR, MIN_AMOUNT_IDR, XenditError

log = logging.getLogger(__name__)

ASK_AMOUNT = 1

STATUS_EMOJI = {
    STATUS_PENDING: "⏳ pendiente",
    STATUS_PAID: "✅ pagado",
    STATUS_EXPIRED: "⌛ expirado",
    STATUS_FAILED: "❌ fallido",
    STATUS_REFUNDED: "↩️ reembolsado",
}

HELP_TEXT = (
    "🏦 <b>Pagos QRIS — ayuda</b>\n\n"
    "Paga con cualquier app indonesia (GoPay, OVO, DANA, bancos) "
    "escaneando un código QR, y recibe el equivalente en USDT en tu "
    "saldo interno del bot.\n\n"
    "<b>Comandos</b>\n"
    "/pagar — generar un QR de pago (o /pagar 50000)\n"
    "/mispagos — ver y actualizar tus pagos\n"
    "/balance — tu saldo en USDT\n"
    "/reintentar — reintentar conversiones pendientes\n"
    "/reembolsar — (solo admin) revertir un pago\n"
    "/cancelar — cancelar la operación actual\n\n"
    f"Importe mínimo: {rates.format_idr(MIN_AMOUNT_IDR)} · "
    f"máximo: {rates.format_idr(MAX_AMOUNT_IDR)}.\n\n"
    "⚠️ QRIS solo funciona en Indonesia: para pagar necesitas una "
    "app de pago indonesia. El saldo USDT es interno del bot."
)


def parse_amount(text: str) -> int | None:
    """Acepta "50000", "50.000", "50,000" o "Rp 50000". None si no vale."""
    cleaned = (text or "").strip().lower()
    for prefix in ("rp", "idr"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    cleaned = cleaned.replace(".", "").replace(",", "").replace(" ", "")
    if not cleaned.isdigit():
        return None
    return int(cleaned)


def build_application(config: Config,
                      service: PaymentsService) -> Application:
    """Crea la aplicación de Telegram y conecta el notificador."""
    app = Application.builder().token(config.bot_token).build()

    async def telegram_notifier(telegram_user_id: int, text: str) -> None:
        await app.bot.send_message(chat_id=telegram_user_id, text=text,
                                   parse_mode="HTML")

    service.notifier = telegram_notifier

    async def start_cmd(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        assert user is not None
        service.db.ensure_user(user.id, user.username)
        await update.message.reply_text(
            "👋 ¡Hola! Soy el bot de pagos QRIS.\n\n"
            "Genero un código QR para que pagues en rupias indonesias "
            "(IDR) con GoPay, OVO, DANA o tu banco, y acredito el "
            "equivalente en USDT en tu saldo interno.\n\n"
            "Usa /pagar para crear un pago o /help para ver los comandos.",
            parse_mode="HTML")

    async def help_cmd(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP_TEXT, parse_mode="HTML")

    async def _create_qr(update: Update, amount_idr: int) -> None:
        """Crea el pago en Xendit y envía el QR al usuario."""
        user = update.effective_user
        assert user is not None
        assert update.message is not None
        if not MIN_AMOUNT_IDR <= amount_idr <= MAX_AMOUNT_IDR:
            await update.message.reply_text(
                "⚠️ El importe debe estar entre "
                f"{rates.format_idr(MIN_AMOUNT_IDR)} y "
                f"{rates.format_idr(MAX_AMOUNT_IDR)}.")
            return
        await update.message.reply_text("⏳ Generando tu código QR…")
        try:
            payment = await service.create_payment(
                telegram_user_id=user.id, username=user.username,
                amount_idr=amount_idr)
        except ValueError as exc:
            # Límite de pagos pendientes: mensaje claro, sin error feo.
            await update.message.reply_text(f"⚠️ {exc}")
            return
        except XenditError as exc:
            log.error("Xendit falló al crear QR: %s", exc)
            await update.message.reply_text(
                "❌ No se pudo generar el código QR en este momento. "
                "Inténtalo de nuevo en unos minutos.")
            return
        png = qrgen.qr_png_bytes(payment["qr_string"])
        caption = (
            "🧾 <b>Pago QRIS</b>\n"
            f"Importe: <b>{rates.format_idr(amount_idr)}</b>\n"
            f"Orden: <code>{payment['order_id']}</code>\n"
        )
        if payment.get("expires_at"):
            caption += f"Vence (UTC): {payment['expires_at']}\n"
        caption += (
            "\nEscanea el código con GoPay, OVO, DANA o la app de tu "
            "banco y confirma el pago. Te avisaré aquí mismo cuando "
            "se confirme y tu saldo en USDT se actualizará solo.\n"
            "Usa /mispagos para ver el estado.")
        await update.message.reply_photo(
            photo=InputFile(io.BytesIO(png), filename="qris.png"),
            caption=caption, parse_mode="HTML")

    async def pagar_entry(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> int:
        """Entrada de /pagar: con importe directo o pidiendo el importe."""
        assert update.message is not None
        if context.args:
            amount = parse_amount(" ".join(context.args))
            if amount is None:
                await update.message.reply_text(
                    "⚠️ No entendí ese importe. Escríbelo solo en "
                    "números, por ejemplo: /pagar 50000")
                return ConversationHandler.END
            await _create_qr(update, amount)
            return ConversationHandler.END
        await update.message.reply_text(
            "💰 ¿Qué importe quieres pagar en IDR?\n"
            f"(mínimo {rates.format_idr(MIN_AMOUNT_IDR)}, máximo "
            f"{rates.format_idr(MAX_AMOUNT_IDR)}).\n"
            "Escribe solo el número, o /cancelar para salir.")
        return ASK_AMOUNT

    async def pagar_amount(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> int:
        assert update.message is not None
        amount = parse_amount(update.message.text or "")
        if amount is None:
            await update.message.reply_text(
                "⚠️ Eso no parece un importe. Escribe solo el número "
                "en IDR (por ejemplo 50000), o /cancelar para salir.")
            return ASK_AMOUNT
        await _create_qr(update, amount)
        return ConversationHandler.END

    async def cancel_cmd(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
        assert update.message is not None
        await update.message.reply_text("Operación cancelada.")
        return ConversationHandler.END

    async def mispagos_cmd(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        assert user is not None
        assert update.message is not None
        await update.message.reply_text(
            "🔄 Actualizando tus pagos con Xendit…")
        summary = await service.refresh_user_payments(user.id)
        payments = service.db.get_payments_for_user(user.id, limit=10)
        if not payments:
            await update.message.reply_text(
                "Aún no tienes pagos. Usa /pagar para crear el primero.")
            return
        lines = ["🧾 <b>Tus últimos pagos</b>\n"]
        for p in payments:
            status = STATUS_EMOJI.get(p["status"], p["status"])
            line = (f"• <code>{p['order_id']}</code> — "
                    f"{rates.format_idr(p['amount_idr'])} — {status}")
            if p["status"] == STATUS_PAID:
                if p.get("usdt_credited_micro") is not None:
                    line += (f" → "
                             f"{rates.format_usdt(p['usdt_credited_micro'])}")
                else:
                    line += " (conversión pendiente: /reintentar)"
                if p.get("payer_source"):
                    line += f" · {esc(p['payer_source'])}"
            lines.append(line)
        notes = []
        if summary.get("paid_now"):
            notes.append(f"✅ {len(summary['paid_now'])} pago(s) "
                         "confirmado(s) ahora.")
        if summary.get("expired_now"):
            notes.append(f"⌛ {len(summary['expired_now'])} pago(s) "
                         "expirado(s).")
        if summary.get("converted"):
            notes.append(f"💱 {len(summary['converted'])} conversión(es) "
                         "completada(s).")
        if notes:
            lines.append("\n" + "\n".join(notes))
        await update.message.reply_text("\n".join(lines),
                                        parse_mode="HTML")

    async def balance_cmd(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        assert user is not None
        assert update.message is not None
        service.db.ensure_user(user.id, user.username)
        balance = service.db.get_balance_micro(user.id)
        ledger = service.db.get_ledger_for_user(user.id, limit=5)
        text = (f"💰 <b>Tu saldo: {rates.format_usdt(balance)}</b>\n\n"
                "Saldo interno del bot (no es una transferencia "
                "on-chain).")
        if ledger:
            text += "\n\n<b>Últimos movimientos</b>\n"
            for entry in ledger:
                micro = entry['amount_micro_usdt']
                sign = "+" if micro >= 0 else "−"
                text += (f"• {sign}{rates.format_usdt(abs(micro))}"
                         f" — {rates.format_idr(entry['amount_idr'])} "
                         f"(orden <code>{entry['order_id']}</code>)\n")
        await update.message.reply_text(text, parse_mode="HTML")

    async def reintentar_cmd(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        assert user is not None
        assert update.message is not None
        results = await service.retry_pending_conversions(user.id)
        if not results:
            await update.message.reply_text(
                "No tienes pagos pendientes de conversión. ✅")
            return
        converted = [r for r in results
                     if r.get("outcome") == "converted"]
        failed = [r for r in results if r.get("outcome") == "rate_failed"]
        lines = []
        if converted:
            total = sum(r["usdt_micro"] for r in converted)
            lines.append(
                f"✅ {len(converted)} conversión(es) completada(s): "
                f"{rates.format_usdt(total)} acreditados.")
        if failed:
            lines.append(
                f"⏳ {len(failed)} conversión(es) siguen pendientes: "
                "no se pudo consultar la tasa. Inténtalo de nuevo en "
                "unos minutos.")
        await update.message.reply_text("\n".join(lines),
                                        parse_mode="HTML")

    async def reembolsar_cmd(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
        """Solo el admin: revierte un pago y resta su saldo.

        Útil cuando Xendit reembolsa un pago y el aviso automático
        no llegó, o para corregir un caso a mano.
        """
        user = update.effective_user
        assert user is not None
        assert update.message is not None
        if config.admin_id is None or user.id != config.admin_id:
            await update.message.reply_text(
                "⛔ Solo el administrador puede reembolsar pagos.")
            return
        if not context.args:
            await update.message.reply_text(
                "Uso: /reembolsar <order_id>\n"
                "Ejemplo: /reembolsar tg_2018620717_ab12cd34")
            return
        order_id = context.args[0].strip()
        result = await service.refund_payment(order_id, via="manual")
        outcome = result.get("outcome")
        if outcome == "refunded":
            await update.message.reply_text(
                "↩️ Reembolso procesado.\n"
                f"Orden: <code>{esc(order_id)}</code>\n"
                "Revertido: "
                f"{rates.format_usdt(result['usdt_reversed_micro'])}",
                parse_mode="HTML")
        elif outcome == "unknown_order":
            await update.message.reply_text(
                f"❓ No conozco la orden <code>{esc(order_id)}</code>.",
                parse_mode="HTML")
        else:  # duplicate_refund
            await update.message.reply_text(
                "Esa orden ya estaba reembolsada.")

    async def error_handler(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Error en un handler: %s", context.error)

    pagar_conv = ConversationHandler(
        entry_points=[CommandHandler("pagar", pagar_entry)],
        states={ASK_AMOUNT: [MessageHandler(
            filters.TEXT & ~filters.COMMAND, pagar_amount)]},
        fallbacks=[CommandHandler("cancelar", cancel_cmd)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(pagar_conv)
    app.add_handler(CommandHandler("mispagos", mispagos_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("reintentar", reintentar_cmd))
    app.add_handler(CommandHandler("reembolsar", reembolsar_cmd))
    app.add_handler(CommandHandler("cancelar", cancel_cmd))
    app.add_error_handler(error_handler)
    return app
