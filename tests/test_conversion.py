"""Tests de la conversión IDR -> USDT y su reintento."""

import shutil
import unittest
from decimal import Decimal

from qrisbot.rates import RateError
from tests.helpers import (USER_ID, CALLBACK_TOKEN, make_service,
                           make_pending_payment, webhook_payload)


class TestConversion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service, self.db, self.notes, self.tmpdir = make_service(
            rate_error=True)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_fallo_de_tasa_no_acredita_nada(self):
        """Si CoinGecko falla: pagado pero SIN convertir, sin adivinar."""
        payment = make_pending_payment(self.service)
        status, body = await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), CALLBACK_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "paid_unconverted")
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["conversion_status"], "failed")
        self.assertIsNone(row["usdt_credited_micro"])
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)
        self.assertEqual(self.db.get_ledger_for_user(USER_ID), [])
        user_notes = [t for uid, t in self.notes if uid == USER_ID]
        self.assertEqual(len(user_notes), 1)
        self.assertIn("Pago recibido", user_notes[0])
        self.assertIn("pendiente", user_notes[0])
        self.assertIn("/reintentar", user_notes[0])

    async def test_reintento_convierte_y_no_duplica(self):
        payment = make_pending_payment(self.service)
        await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), CALLBACK_TOKEN)
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)

        # La tasa vuelve: el reintento acredita una sola vez.
        async def tasa_ok() -> Decimal:
            return Decimal("16500")

        self.service.rate_provider = tasa_ok
        results = await self.service.retry_pending_conversions(USER_ID)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "converted")
        self.assertEqual(results[0]["usdt_micro"], 3030303)
        self.assertEqual(self.db.get_balance_micro(USER_ID), 3030303)
        row = self.db.get_payment(payment["order_id"])
        assert row is not None
        self.assertEqual(row["conversion_status"], "converted")
        self.assertEqual(row["rate_idr_per_usdt"], "16500")

        # Segundo reintento: ya no hay nada pendiente, saldo intacto.
        results = await self.service.retry_pending_conversions(USER_ID)
        self.assertEqual(results, [])
        self.assertEqual(self.db.get_balance_micro(USER_ID), 3030303)
        self.assertEqual(len(self.db.get_ledger_for_user(USER_ID)), 1)

    async def test_reintento_con_tasa_caida_sigue_pendiente(self):
        payment = make_pending_payment(self.service)
        await self.service.handle_webhook(
            webhook_payload(payment["order_id"]), CALLBACK_TOKEN)
        results = await self.service.retry_pending_conversions(USER_ID)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "rate_failed")
        self.assertEqual(self.db.get_balance_micro(USER_ID), 0)

    async def test_sin_pendientes_no_hace_nada(self):
        results = await self.service.retry_pending_conversions(USER_ID)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
