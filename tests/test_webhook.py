"""Tests del webhook: firma, pago, duplicados, errores."""

import shutil
import unittest

from tests.helpers import (ADMIN_ID, CALLBACK_TOKEN, USER_ID, make_service,
                           make_pending_payment, webhook_payload)


class TestWebhook(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service, self.db, self.notes, self.tmpdir = make_service()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _user_notes(self):
        return [t for uid, t in self.notes if uid == USER_ID]

    def _admin_notes(self):
        return [t for uid, t in self.notes if uid == ADMIN_ID]

    async def test_pago_completo(self):
        payment = make_pending_payment(self.service)
        status, body = await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), CALLBACK_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "paid")
        # 50.000 IDR a 16.500 = 3.030.303 micro-USDT
        self.assertEqual(body["usdt_micro"], 3030303)
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["conversion_status"], "converted")
        self.assertEqual(row["payer_source"], "GOPAY")
        self.assertEqual(row["gateway_payment_id"], "qrpy_test123")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 3030303)
        self.assertEqual(len(self._user_notes()), 1)
        self.assertIn("Pago recibido", self._user_notes()[0])
        self.assertIn("3.030303 USDT", self._user_notes()[0])
        self.assertEqual(len(self._admin_notes()), 1)
        # Ledger auditable
        ledger = self.db.get_ledger_for_user(USER_ID)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["amount_micro_usdt"], 3030303)
        self.assertEqual(ledger[0]["amount_idr"], 50000)

    async def test_webhook_duplicado_no_duplica(self):
        payment = make_pending_payment(self.service)
        payload = webhook_payload(payment["order_id"])
        await self.service.handle_webhook(payload, CALLBACK_TOKEN)
        status, body = await self.service.handle_webhook(
            payload, CALLBACK_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "duplicate")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 3030303)
        self.assertEqual(len(self._user_notes()), 1)
        self.assertEqual(len(self.db.get_ledger_for_user(USER_ID)), 1)

    async def test_token_forjado_rechazado(self):
        payment = make_pending_payment(self.service)
        status, _ = await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), "token-falso")
        self.assertEqual(status, 401)
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        self.assertEqual(self.notes, [])

    async def test_sin_token_rechazado(self):
        payment = make_pending_payment(self.service)
        status, _ = await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), None)
        self.assertEqual(status, 401)
        self.assertEqual(self.notes, [])

    async def test_orden_desconocida(self):
        status, body = await self.service.handle_webhook(
            webhook_payload("tg_999_abcdef12"), CALLBACK_TOKEN)
        self.assertEqual(status, 200)  # 200 para que Xendit no reintente
        self.assertEqual(body["outcome"], "unknown_order")
        self.assertEqual(self.notes, [])

    async def test_importe_distinto_no_acredita(self):
        payment = make_pending_payment(self.service, amount_idr=50000)
        status, body = await self.service.handle_webhook(
            webhook_payload(payment["order_id"], amount_idr=10000),
            CALLBACK_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "amount_mismatch")
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        self.assertEqual(self._user_notes(), [])
        self.assertEqual(len(self._admin_notes()), 1)  # el admin sí avisa

    async def test_no_completado_se_ignora(self):
        payment = make_pending_payment(self.service)
        status, body = await self.service.handle_webhook(
            webhook_payload(payment["order_id"], status="PENDING"),
            CALLBACK_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "not_completed")
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.notes, [])

    async def test_evento_inesperado(self):
        payment = make_pending_payment(self.service)
        payload = webhook_payload(payment["order_id"])
        payload["event"] = "invoice.paid"
        _, body = await self.service.handle_webhook(payload, CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "invalid")

    async def test_external_id_tambien_funciona(self):
        # Compatibilidad con la versión anterior de la API de Xendit
        payment = make_pending_payment(self.service)
        payload = webhook_payload(payment["order_id"])
        del payload["qr_code"]["reference_id"]
        payload["qr_code"]["external_id"] = payment["order_id"]
        _, body = await self.service.handle_webhook(payload, CALLBACK_TOKEN)
        self.assertEqual(body["outcome"], "paid")


if __name__ == "__main__":
    unittest.main()
