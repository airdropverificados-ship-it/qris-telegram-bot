"""Tasa de cambio IDR -> USDT y matemática de dinero.

Fuente: CoinGecko API pública (`/simple/price`), sin clave.
Devuelve cuántos IDR cuesta 1 USDT. La conversión redondea SIEMPRE
hacia abajo: nunca se acredita más de lo recibido.

Todo el dinero se maneja en enteros (micro-USDT) o Decimal. Nunca float.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

import httpx

MICRO_PER_USDT = 1_000_000
COINGECKO_TIMEOUT = 10.0  # segundos


class RateError(Exception):
    """No se pudo obtener una tasa válida."""


async def fetch_idr_per_usdt(coingecko_url: str,
                             client: httpx.AsyncClient | None = None
                             ) -> Decimal:
    """Precio de 1 USDT en IDR según CoinGecko (tether vs idr).

    Lanza RateError si la red falla, la respuesta no es 200 o el
    dato no viene / no es positivo. Quien llama decide qué hacer
    (el pago queda pagado-pero-sin-convertir, nunca se inventa tasa).
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=COINGECKO_TIMEOUT)
    assert client is not None
    try:
        resp = await client.get(
            coingecko_url,
            params={"ids": "tether", "vs_currencies": "idr"},
        )
        if resp.status_code != 200:
            raise RateError(
                f"CoinGecko devolvió HTTP {resp.status_code}")
        data = resp.json()
        raw = data.get("tether", {}).get("idr")
        if raw is None:
            raise RateError("CoinGecko no devolvió el precio tether/idr")
        rate = Decimal(str(raw))
        if rate <= 0:
            raise RateError(f"Tasa no positiva: {rate}")
        return rate
    except RateError:
        raise
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise RateError(f"No se pudo consultar CoinGecko: {exc}") from exc
    finally:
        if own_client:
            await client.aclose()


def idr_to_micro_usdt(amount_idr: int, rate_idr_per_usdt: Decimal) -> int:
    """Convierte IDR a micro-USDT redondeando hacia ABAJO.

    Ejemplo: 15.000 IDR a 16.500,50 IDR/USDT =
    floor(15.000 * 1.000.000 / 16.500,50) = 909.063 micro-USDT
    (0,909063 USDT). El redondeo hacia abajo protege al bot: nunca
    acredita más de lo que vale el pago recibido.
    """
    if amount_idr <= 0:
        return 0
    if rate_idr_per_usdt <= 0:
        raise ValueError("La tasa debe ser positiva")
    micro = (Decimal(amount_idr) * MICRO_PER_USDT
             / rate_idr_per_usdt).to_integral_value(
                 rounding=ROUND_FLOOR)
    return int(micro)


def format_usdt(micro: int) -> str:
    """Micro-USDT -> texto legible, p. ej. 909086 -> "0.909086 USDT".

    Acepta negativos (reembolsos): -909063 -> "-0.909063 USDT".
    """
    micro = int(micro)
    if micro < 0:
        return "-" + format_usdt(-micro)
    whole, frac = divmod(micro, MICRO_PER_USDT)
    return f"{whole}.{frac:06d} USDT"


def format_idr(amount_idr: int) -> str:
    """IDR -> texto legible, p. ej. 150000 -> "Rp 150.000"."""
    return "Rp " + f"{int(amount_idr):,}".replace(",", ".")
