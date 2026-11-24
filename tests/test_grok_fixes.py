"""Tests de las correcciones de la auditoría de Grok (22 nov 2026).

Cubre los 4 puntos mayores de Grok:
- SQLite en WAL + busy_timeout.
- Reembolsos: revierten el saldo, con constancia en el ledger,
  e idempotentes (un segundo aviso no resta dos veces).
- Escape HTML en las notificaciones.
- Endurecimiento del webhook (límite de tasa, IPs, HTTPS en
  producción) + importe estricto + límite de QR pendientes.
"""

import os
import shutil
import unittest
from unittest import mock

from qrisbot import rates
from qrisbot.config import load_config
from qrisbot.webhook import RateLimiter, ip_allowed
from tests.helpers import (ADMIN_ID, CALLBACK_TOKEN, USER_ID, make_service,
                           make_pending_payment, webhook_payload)


class FakeXendit:
    """Cliente Xendit falso para probar la creación de pagos."""

    def __init__(self):
        self.created: list[str] = []

    async def create_dynamic_qr(self, reference_id, amount_idr,
                                metadata=None):
        self.created.append(reference_id)
        return {"id": f"qr_{reference_id}",
                "qr_string": "0002010102QRIS-DE-PRUEBA",
                "expires_at": "2030-01-01T00:00:00Z"}


def refund_payload(order_id: str, amount_idr: int = 50000,
                   event: str = "qr.payment") -> dict:
    """Payload de reembolso: mismo callback, estado REFUNDED."""
    payload = webhook_payload(order_id, amount_idr=amount_idr,
                              status="REFUNDED")
    payload["event"] = event
    return payload


class TestSqliteWal(unittest.TestCase):
    def test_wal_y_busy_timeout_activos(self):
        service, db, _, tmpdir = make_service()
        try:
            mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")
            timeout = db.conn.execute(
                "PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(timeout, 10000)
        finally:
            db.close()
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestReembolsos(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service, self.db, self.notes, self.tmpdir = make_service()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _pagar(self, payment):
        _, body = await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "paid")
        return body

    def _user_notes(self):
        return [t for uid, t in self.notes if uid == USER_ID]

    async def test_reembolso_revierte_el_saldo(self):
        payment = make_pending_payment(self.service)
        await self._pagar(payment)
        self.assertEqual(self.db.get_balance_micro(USER_ID), 3030303)
        status, body = await self.service.handle_webhook(
            refund_payload(payment["order_id"]), CALLBACK_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "refunded")
        self.assertEqual(body["usdt_reversed_micro"], 3030303)
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "refunded")
        self.assertIsNotNone(row["refunded_at"])
        ledger = self.db.get_ledger_for_user(USER_ID)
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[0]["kind"], "refund")
        self.assertEqual(ledger[0]["amount_micro_usdt"], -3030303)
        self.assertIn("Reembolso", self._user_notes()[-1])

    async def test_segundo_reembolso_no_resta_dos_veces(self):
        payment = make_pending_payment(self.service)
        await self._pagar(payment)
        await self.service.handle_webhook(
            refund_payload(payment["order_id"]), CALLBACK_TOKEN)
        _, body = await self.service.handle_webhook(
            refund_payload(payment["order_id"]), CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "duplicate_refund")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        self.assertEqual(len(self.db.get_ledger_for_user(USER_ID)), 2)

    async def test_reembolso_por_nombre_de_evento(self):
        payment = make_pending_payment(self.service)
        await self._pagar(payment)
        _, body = await self.service.handle_webhook(
            refund_payload(payment["order_id"], event="qr.refund"),
            CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "refunded")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)

    async def test_reembolso_sin_convertir_no_toca_el_ledger(self):
        service, db, _, tmpdir = make_service(rate_error=True)
        try:
            payment = make_pending_payment(service)
            _, body = await service.handle_webhook(
                webhook_payload(payment["order_id"]), CALLBACK_TOKEN)
            self.assertEqual(body["outcome"], "paid_unconverted")
            _, body = await service.handle_webhook(
                refund_payload(payment["order_id"]), CALLBACK_TOKEN)
            self.assertEqual(body["outcome"], "refunded")
            self.assertFalse(body["was_converted"])
            self.assertEqual(body["usdt_reversed_micro"], 0)
            self.assertEqual(db.get_ledger_for_user(USER_ID), [])
            row = db.get_payment(payment["order_id"])
            assert row is not None
            self.assertEqual(row["status"], "refunded")
        finally:
            db.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_reembolso_orden_desconocida(self):
        _, body = await self.service.handle_webhook(
            refund_payload("tg_999_abcdef12"), CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "unknown_order")

    async def test_reembolso_manual_del_admin(self):
        payment = make_pending_payment(self.service)
        await self._pagar(payment)
        result = await self.service.refund_payment(
            payment["order_id"], via="manual")
        self.assertEqual(result["outcome"], "refunded")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        again = await self.service.refund_payment(payment["order_id"])
        self.assertEqual(again["outcome"], "duplicate_refund")

    async def test_pago_tardio_tras_reembolso_no_acredita(self):
        payment = make_pending_payment(self.service)
        await self.service.handle_webhook(
            refund_payload(payment["order_id"]), CALLBACK_TOKEN)
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "refunded")
        await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), CALLBACK_TOKEN)
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "refunded")


class TestEscapeHtml(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service, self.db, self.notes, self.tmpdir = make_service()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_nombre_malicioso_se_escapa(self):
        self.service.db.ensure_user(USER_ID, "<b>hola</b>")
        order_id = self.service.db.create_payment(
            order_id="tg_2018620717_ab12cd34",
            telegram_user_id=USER_ID, username="<b>hola</b>",
            amount_idr=50000, qr_code_id="qr_x",
            qr_string="0002010102QRIS-DE-PRUEBA",
            expires_at="2030-01-01T00:00:00Z")["order_id"]
        await self.service.handle_webhook(
            webhook_payload(order_id, source="<i>X</i>"), CALLBACK_TOKEN)
        admin_notes = [t for uid, t in self.notes if uid == ADMIN_ID]
        self.assertEqual(len(admin_notes), 1)
        self.assertIn("&lt;b&gt;hola&lt;/b&gt;", admin_notes[0])
        self.assertIn("&lt;i&gt;X&lt;/i&gt;", admin_notes[0])
        self.assertNotIn("<b>hola</b>", admin_notes[0])
        self.assertNotIn("<i>X</i>", admin_notes[0])


class TestImporteEstricto(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service, self.db, self.notes, self.tmpdir = make_service()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_importe_decimal_no_acredita_ni_rompe(self):
        payment = make_pending_payment(self.service)
        payload = webhook_payload(payment["order_id"])
        payload["amount"] = "12.5"
        _, body = await self.service.handle_webhook(
            payload, CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "invalid")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "pending")

    async def test_importe_float_no_acredita(self):
        payment = make_pending_payment(self.service)
        payload = webhook_payload(payment["order_id"])
        payload["amount"] = 50000.0
        _, body = await self.service.handle_webhook(
            payload, CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "invalid")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)

    async def test_importe_cadena_de_digitos_si_vale(self):
        payment = make_pending_payment(self.service)
        payload = webhook_payload(payment["order_id"])
        payload["amount"] = "50000"
        _, body = await self.service.handle_webhook(
            payload, CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "paid")


class TestRateLimiter(unittest.TestCase):
    def test_limite_por_ip(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertFalse(limiter.allow("1.2.3.4"))
        # Otra IP no se ve afectada.
        self.assertTrue(limiter.allow("5.6.7.8"))

    def test_sin_lista_todo_pasa(self):
        self.assertTrue(ip_allowed("1.2.3.4", ()))
        self.assertTrue(ip_allowed(None, ()))

    def test_con_lista_solo_esas_ip(self):
        self.assertTrue(ip_allowed("1.2.3.4", ("1.2.3.4",)))
        self.assertFalse(ip_allowed("9.9.9.9", ("1.2.3.4",)))
        self.assertFalse(ip_allowed(None, ("1.2.3.4",)))


class TestConfigEndurecida(unittest.TestCase):
    BASE_ENV = {
        "BOT_TOKEN": "t",
        "XENDIT_API_KEY": "xnd_development_x",
        "XENDIT_CALLBACK_TOKEN": "cb",
        "WEBHOOK_URL": "http://localhost:8080/webhook/qris",
    }

    def test_clave_produccion_exige_https(self):
        env = dict(self.BASE_ENV, XENDIT_API_KEY="xnd_production_x")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                load_config()

    def test_clave_produccion_con_https_ok(self):
        env = dict(self.BASE_ENV, XENDIT_API_KEY="xnd_production_x",
                   WEBHOOK_URL="https://ejemplo.test/webhook/qris")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
            self.assertFalse(cfg.is_test_mode)

    def test_clave_test_permite_http(self):
        with mock.patch.dict(os.environ, self.BASE_ENV, clear=True):
            cfg = load_config()
            self.assertTrue(cfg.is_test_mode)

    def test_rate_limit_invalido(self):
        env = dict(self.BASE_ENV,
                   WEBHOOK_RATE_LIMIT_PER_MINUTE="mucho")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                load_config()

    def test_ips_permitidas_se_parsean(self):
        env = dict(self.BASE_ENV,
                   XENDIT_WEBHOOK_IPS="1.2.3.4, 5.6.7.8")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
            self.assertEqual(cfg.webhook_allowed_ips,
                             ("1.2.3.4", "5.6.7.8"))


class TestLimiteQrPendientes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service, self.db, self.notes, self.tmpdir = make_service()
        self.service.xendit = FakeXendit()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_maximo_cinco_pendientes(self):
        for _ in range(5):
            await self.service.create_payment(
                telegram_user_id=USER_ID, username="u",
                amount_idr=50000)
        with self.assertRaises(ValueError):
            await self.service.create_payment(
                telegram_user_id=USER_ID, username="u",
                amount_idr=50000)
        self.assertEqual(len(self.service.xendit.created), 5)


class TestFormatoUsdtNegativo(unittest.TestCase):
    def test_negativo(self):
        self.assertEqual(rates.format_usdt(-909063), "-0.909063 USDT")
        self.assertEqual(rates.format_usdt(-1500000), "-1.500000 USDT")


if __name__ == "__main__":
    unittest.main()
