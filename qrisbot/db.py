"""Capa de datos SQLite.

Dinero: el saldo USDT se guarda como entero en micro-USDT
(1 USDT = 1.000.000 micro-USDT). Nunca se usa float para dinero.

Tablas:
- users: un registro por usuario de Telegram, con su saldo interno.
- payments: un registro por código QR generado (un pago = una orden).
- ledger: movimientos auditables del saldo (cada acreditación queda
  registrada con su orden, su tasa y sus importes).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Estados de un pago.
STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"
STATUS_REFUNDED = "refunded"  # reembolsado (saldo revertido, ver record_refund)

# Estados de la conversión IDR -> USDT de un pago ya pagado.
CONV_PENDING = "pending"      # pagado, conversión aún no intentada
CONV_CONVERTED = "converted"  # convertido y acreditado
CONV_FAILED = "failed"        # pagado pero SIN convertir (falló la tasa)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    balance_micro INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    order_id            TEXT PRIMARY KEY,
    telegram_user_id    INTEGER NOT NULL,
    username            TEXT,
    amount_idr          INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    qr_code_id          TEXT,
    qr_string           TEXT,
    gateway_payment_id  TEXT,
    payer_source        TEXT,
    receipt_id          TEXT,
    rate_idr_per_usdt   TEXT,
    usdt_credited_micro INTEGER,
    conversion_status   TEXT NOT NULL DEFAULT 'pending',
    raw_webhook         TEXT,
    expires_at          TEXT,
    created_at          TEXT NOT NULL,
    paid_at             TEXT,
    refunded_at         TEXT,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user
    ON payments (telegram_user_id, created_at);

CREATE TABLE IF NOT EXISTS ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id    INTEGER NOT NULL,
    order_id            TEXT NOT NULL,
    kind                TEXT NOT NULL,
    amount_micro_usdt   INTEGER NOT NULL,
    amount_idr          INTEGER NOT NULL,
    rate_idr_per_usdt   TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user
    ON ledger (telegram_user_id, created_at);
"""


def utcnow() -> str:
    """Marca de tiempo UTC en ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Acceso a SQLite. Un solo hilo (asyncio): sin pool ni locks."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        # timeout=10s: si la BD está bloqueada, espera en vez de
        # fallar al instante ("database is locked").
        self.conn = sqlite3.connect(self.path, check_same_thread=False,
                                    timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        # WAL: el bot y el webhook leen/escriben a la vez sin
        # bloquearse (los reintentos de Xendit llegan en paralelo).
        # busy_timeout: espera hasta 10 s antes de rendirse.
        # (En :memory: journal_mode devuelve "memory": no pasa nada,
        # es solo para los tests.)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Migraciones ligeras para BD creadas con versiones viejas."""
        cols = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(payments)")}
        if "refunded_at" not in cols:
            self.conn.execute(
                "ALTER TABLE payments ADD COLUMN refunded_at TEXT")

    def close(self) -> None:
        self.conn.close()

    # ---------------- usuarios ----------------

    def ensure_user(self, telegram_id: int, username: str | None) -> dict:
        """Crea el usuario si no existe y devuelve su fila."""
        now = utcnow()
        self.conn.execute(
            "INSERT INTO users (telegram_id, username, balance_micro,"
            " created_at, updated_at) VALUES (?, ?, 0, ?, ?)"
            " ON CONFLICT(telegram_id) DO UPDATE SET"
            " username=excluded.username, updated_at=excluded.updated_at",
            (telegram_id, username, now, now),
        )
        self.conn.commit()
        return self.get_user(telegram_id)  # type: ignore[return-value]

    def get_user(self, telegram_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_balance_micro(self, telegram_id: int) -> int:
        user = self.get_user(telegram_id)
        return int(user["balance_micro"]) if user else 0

    # ---------------- pagos ----------------

    def create_payment(self, *, order_id: str, telegram_user_id: int,
                       username: str | None, amount_idr: int,
                       qr_code_id: str | None, qr_string: str | None,
                       expires_at: str | None) -> dict:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO payments (order_id, telegram_user_id, username,"
            " amount_idr, status, qr_code_id, qr_string, expires_at,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (order_id, telegram_user_id, username, amount_idr,
             qr_code_id, qr_string, expires_at, now, now),
        )
        self.conn.commit()
        return self.get_payment(order_id)  # type: ignore[return-value]

    def get_payment(self, order_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM payments WHERE order_id = ?", (order_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_payments_for_user(self, telegram_id: int,
                              limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM payments WHERE telegram_user_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_payments(self, telegram_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM payments WHERE telegram_user_id = ?"
            " AND status = 'pending' ORDER BY created_at ASC",
            (telegram_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_paid_unconverted(self,
                             telegram_id: int | None = None) -> list[dict]:
        """Pagos pagados cuya conversión a USDT aún no se acreditó."""
        if telegram_id is None:
            rows = self.conn.execute(
                "SELECT * FROM payments WHERE status = 'paid'"
                " AND conversion_status IN ('pending', 'failed')"
                " ORDER BY paid_at ASC",
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM payments WHERE telegram_user_id = ?"
                " AND status = 'paid'"
                " AND conversion_status IN ('pending', 'failed')"
                " ORDER BY paid_at ASC",
                (telegram_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_paid(self, *, order_id: str, gateway_payment_id: str | None,
                  payer_source: str | None, receipt_id: str | None,
                  raw_webhook: dict) -> bool:
        """Marca un pago pendiente como pagado.

        Devuelve True si la transición pendiente -> pagado se aplicó.
        Devuelve False si el pago ya estaba pagado (webhook duplicado:
        Xendit reintenta las notificaciones). La condición
        `status = 'pending'` en el UPDATE es lo que hace idempotente
        el webhook: un duplicado no cambia nada.
        """
        now = utcnow()
        cur = self.conn.execute(
            "UPDATE payments SET status = 'paid',"
            " gateway_payment_id = ?, payer_source = ?, receipt_id = ?,"
            " raw_webhook = ?, paid_at = ?, updated_at = ?"
            " WHERE order_id = ? AND status = 'pending'",
            (gateway_payment_id, payer_source, receipt_id,
             json.dumps(raw_webhook, ensure_ascii=False), now, now,
             order_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def mark_failed(self, *, order_id: str, raw_webhook: dict) -> None:
        """Marca un pago como fallido (p. ej. importe distinto al pedido)."""
        self.conn.execute(
            "UPDATE payments SET status = 'failed', raw_webhook = ?,"
            " updated_at = ? WHERE order_id = ?",
            (json.dumps(raw_webhook, ensure_ascii=False), utcnow(),
             order_id),
        )
        self.conn.commit()

    def mark_expired(self, order_id: str) -> bool:
        """Marca un pago pendiente como expirado. True si cambió algo."""
        cur = self.conn.execute(
            "UPDATE payments SET status = 'expired', updated_at = ?"
            " WHERE order_id = ? AND status = 'pending'",
            (utcnow(), order_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def store_raw_webhook(self, order_id: str, raw_webhook: dict) -> None:
        self.conn.execute(
            "UPDATE payments SET raw_webhook = ?, updated_at = ?"
            " WHERE order_id = ?",
            (json.dumps(raw_webhook, ensure_ascii=False), utcnow(),
             order_id),
        )
        self.conn.commit()

    # ---------------- conversión y saldo ----------------

    def record_conversion(self, *, order_id: str, telegram_user_id: int,
                          amount_idr: int, rate_idr_per_usdt: str,
                          usdt_micro: int) -> bool:
        """Acredita la conversión en el saldo y la registra en el ledger.

        Todo en una sola transacción: saldo + ledger + estado del pago.
        Devuelve False si el pago ya estaba convertido (sin doble
        acreditación).
        """
        cur = self.conn.execute(
            "SELECT conversion_status FROM payments WHERE order_id = ?",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None or row["conversion_status"] == CONV_CONVERTED:
            return False
        now = utcnow()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "UPDATE users SET balance_micro = balance_micro + ?,"
                " updated_at = ? WHERE telegram_id = ?",
                (usdt_micro, now, telegram_user_id),
            )
            self.conn.execute(
                "INSERT INTO ledger (telegram_user_id, order_id, kind,"
                " amount_micro_usdt, amount_idr, rate_idr_per_usdt,"
                " created_at) VALUES (?, ?, 'credit', ?, ?, ?, ?)",
                (telegram_user_id, order_id, usdt_micro, amount_idr,
                 rate_idr_per_usdt, now),
            )
            self.conn.execute(
                "UPDATE payments SET conversion_status = 'converted',"
                " rate_idr_per_usdt = ?, usdt_credited_micro = ?,"
                " updated_at = ? WHERE order_id = ?",
                (rate_idr_per_usdt, usdt_micro, now, order_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def mark_conversion_failed(self, order_id: str) -> None:
        """El pago está pagado pero la tasa falló: queda pendiente de
        reintento (/reintentar o /mispagos). Nunca se acredita a ciegas."""
        self.conn.execute(
            "UPDATE payments SET conversion_status = 'failed',"
            " updated_at = ? WHERE order_id = ?",
            (utcnow(), order_id),
        )
        self.conn.commit()

    def record_refund(self, order_id: str) -> dict | None:
        """Revierte un pago reembolsado (webhook de refund o /reembolsar).

        - Si el pago ya estaba convertido: resta del saldo EXACTAMENTE
          lo acreditado y deja constancia en el ledger (kind='refund',
          importe negativo). El saldo nunca queda negativo por un
          reembolso: cada reembolso devuelve como máximo lo que su
          propio pago acreditó, y no hay otra vía de gasto.
        - Si estaba pagado pero sin convertir: solo marca el estado
          (no hay nada que restar).
        - Si estaba pendiente: lo marca reembolsado para que un pago
          tardío ya no pueda acreditarse (mark_paid exige 'pending').
        - Si ya estaba reembolsado o no existe: no hace nada.
        Devuelve None si no había nada que revertir; si no, un dict
        con el resumen de la reversión.
        """
        row = self.conn.execute(
            "SELECT * FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None or row["status"] == STATUS_REFUNDED:
            return None
        payment = dict(row)
        was_converted = (
            payment["status"] == STATUS_PAID
            and payment["conversion_status"] == CONV_CONVERTED)
        reversed_micro = (int(payment["usdt_credited_micro"] or 0)
                          if was_converted else 0)
        now = utcnow()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if was_converted:
                self.conn.execute(
                    "UPDATE users SET balance_micro = balance_micro - ?,"
                    " updated_at = ? WHERE telegram_id = ?",
                    (reversed_micro, now,
                     payment["telegram_user_id"]),
                )
                self.conn.execute(
                    "INSERT INTO ledger (telegram_user_id, order_id,"
                    " kind, amount_micro_usdt, amount_idr,"
                    " rate_idr_per_usdt, created_at)"
                    " VALUES (?, ?, 'refund', ?, ?, ?, ?)",
                    (payment["telegram_user_id"], order_id,
                     -reversed_micro, payment["amount_idr"],
                     payment["rate_idr_per_usdt"] or "", now),
                )
            self.conn.execute(
                "UPDATE payments SET status = 'refunded',"
                " refunded_at = ?, updated_at = ? WHERE order_id = ?",
                (now, now, order_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"order_id": order_id,
                "telegram_user_id": payment["telegram_user_id"],
                "usdt_reversed_micro": reversed_micro,
                "was_converted": was_converted,
                "amount_idr": payment["amount_idr"]}

    def get_ledger_for_user(self, telegram_id: int,
                            limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ledger WHERE telegram_user_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- utilidades ----------------

    def to_public_dict(self, payment: dict[str, Any]) -> dict[str, Any]:
        """Fila de pago sin el payload crudo (para mostrar al usuario)."""
        public = dict(payment)
        public.pop("raw_webhook", None)
        public.pop("qr_string", None)
        return public
