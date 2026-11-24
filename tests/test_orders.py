"""Tests de órdenes, verificación de token, conversión y formato."""

import unittest
from decimal import Decimal

from qrisbot import qrgen, rates
from qrisbot.payments import (extract_order_id, extract_payer_source,
                              new_order_id, parse_order_id,
                              verify_callback_token)


class TestOrderIds(unittest.TestCase):
    def test_round_trip(self):
        order_id = new_order_id(2018620717)
        self.assertRegex(order_id, r"^tg_2018620717_[0-9a-f]{8}$")
        self.assertEqual(parse_order_id(order_id), 2018620717)

    def test_ids_unicos(self):
        self.assertNotEqual(new_order_id(123), new_order_id(123))

    def test_parse_invalidos(self):
        for bad in ("", "tg_", "tg_abc_12345678", "tg_123_zzzzzzzz",
                    "tg_123_1234567", "otro_123_abcdef12", None):
            self.assertIsNone(parse_order_id(bad))  # type: ignore[arg-type]


class TestCallbackToken(unittest.TestCase):
    def test_valido(self):
        self.assertTrue(verify_callback_token("secreto", "secreto"))

    def test_forjado(self):
        self.assertFalse(verify_callback_token("otro", "secreto"))

    def test_falta_token(self):
        self.assertFalse(verify_callback_token(None, "secreto"))
        self.assertFalse(verify_callback_token("", "secreto"))

    def test_sin_token_configurado(self):
        self.assertFalse(verify_callback_token("x", ""))


class TestPayloadExtraction(unittest.TestCase):
    def test_reference_id(self):
        payload = {"qr_code": {"reference_id": "tg_1_ab12cd34"}}
        self.assertEqual(extract_order_id(payload), "tg_1_ab12cd34")

    def test_external_id_fallback(self):
        payload = {"qr_code": {"external_id": "tg_1_ab12cd34"}}
        self.assertEqual(extract_order_id(payload), "tg_1_ab12cd34")

    def test_sin_qr_code(self):
        self.assertIsNone(extract_order_id({}))
        self.assertIsNone(extract_order_id({"qr_code": {}}))

    def test_payer_source(self):
        self.assertEqual(
            extract_payer_source({"payment_detail": {"source": "OVO"}}),
            "OVO")
        self.assertIsNone(extract_payer_source({}))


class TestConversion(unittest.TestCase):
    def test_ejemplo_documentado(self):
        # 15.000 IDR a 16.500,50 IDR/USDT = 909.063 micro-USDT
        self.assertEqual(
            rates.idr_to_micro_usdt(15000, Decimal("16500.50")), 909063)

    def test_minimo(self):
        # 1.500 IDR a 16.500 = 90.909 micro-USDT (0,090909 USDT)
        self.assertEqual(
            rates.idr_to_micro_usdt(1500, Decimal("16500")), 90909)

    def test_redondeo_siempre_hacia_abajo(self):
        # 100 IDR a 3 IDR/USDT = 33,333333... -> 33.333.333 micro
        self.assertEqual(
            rates.idr_to_micro_usdt(100, Decimal("3")), 33333333)

    def test_cero_y_tasa_invalida(self):
        self.assertEqual(rates.idr_to_micro_usdt(0, Decimal("16500")), 0)
        with self.assertRaises(ValueError):
            rates.idr_to_micro_usdt(1000, Decimal("0"))
        with self.assertRaises(ValueError):
            rates.idr_to_micro_usdt(1000, Decimal("-5"))


class TestFormato(unittest.TestCase):
    def test_usdt(self):
        self.assertEqual(rates.format_usdt(909063), "0.909063 USDT")
        self.assertEqual(rates.format_usdt(1500000), "1.500000 USDT")
        self.assertEqual(rates.format_usdt(0), "0.000000 USDT")

    def test_idr(self):
        self.assertEqual(rates.format_idr(150000), "Rp 150.000")
        self.assertEqual(rates.format_idr(1500), "Rp 1.500")


class TestQrPng(unittest.TestCase):
    def test_genera_png_valido(self):
        png = qrgen.qr_png_bytes("0002010102QRIS-DE-PRUEBA")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 100)


if __name__ == "__main__":
    unittest.main()
