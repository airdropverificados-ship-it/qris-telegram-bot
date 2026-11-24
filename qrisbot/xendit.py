"""Cliente de la API de QR Codes de Xendit (QRIS).

Documentación oficial (verificada 22 nov 2026):
https://archive.developers.xendit.co/api-reference/qr-codes/

Versión de API usada: 2022-07-31 (la actual recomendada por Xendit).
- Crear QR dinámico: POST /qr_codes
  {reference_id, type: "DYNAMIC", currency: "IDR", amount}
  -> {id, qr_string, status: "ACTIVE", expires_at, channel_code, ...}
  La callback URL NO va en la petición: se configura en el panel de
  Xendit (Settings -> Callbacks -> "QR code paid & refunded").
- Consultar pagos: GET /qr_codes/payments?reference_id=...
- Simular pago (solo modo test): POST /qr_codes/:id/payments/simulate

Autenticación: HTTP Basic con la clave secreta como usuario
(xnd_development_... en test, xnd_production_... en vivo).
Importes: mínimo 1.500 IDR, máximo 10.000.000 IDR por pago.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

MIN_AMOUNT_IDR = 1_500
MAX_AMOUNT_IDR = 10_000_000
DEFAULT_TIMEOUT = 30.0


class XenditError(Exception):
    """Error devuelto por la API de Xendit (o fallo de red)."""

    def __init__(self, message: str, error_code: str | None = None,
                 status_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class XenditClient:
    """Cliente asíncrono mínimo para los QR Codes de Xendit."""

    def __init__(self, api_key: str,
                 base_url: str = "https://api.xendit.co",
                 api_version: str = "2022-07-31",
                 client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self._client = client  # inyectable (tests / cliente compartido)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        basic = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/json",
            "api-version": self.api_version,
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str,
                       **kwargs: Any) -> Any:
        client = await self._get_client()
        try:
            resp = await client.request(
                method, f"{self.base_url}{path}",
                headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise XenditError(f"No se pudo contactar a Xendit: {exc}"
                              ) from exc
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            raise XenditError(
                body.get("message") or f"Xendit HTTP {resp.status_code}",
                error_code=body.get("error_code"),
                status_code=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---------------- operaciones ----------------

    async def create_dynamic_qr(self, reference_id: str, amount_idr: int,
                                metadata: dict | None = None) -> dict:
        """Crea un QR dinámico QRIS de importe fijo (un solo pago).

        reference_id es nuestro order_id (tg_<user>_<uuid8>): Xendit lo
        devuelve en el webhook y así sabemos quién pagó.
        """
        if not MIN_AMOUNT_IDR <= amount_idr <= MAX_AMOUNT_IDR:
            raise ValueError(
                f"El importe debe estar entre {MIN_AMOUNT_IDR:,} y "
                f"{MAX_AMOUNT_IDR:,} IDR".replace(",", "."))
        body: dict[str, Any] = {
            "reference_id": reference_id,
            "type": "DYNAMIC",
            "currency": "IDR",
            "amount": amount_idr,
        }
        if metadata:
            body["metadata"] = metadata
        data = await self._request("POST", "/qr_codes", json=body)
        log.info("QR creado: reference_id=%s id=%s", reference_id,
                 data.get("id"))
        return data

    async def get_qr(self, reference_id: str) -> dict:
        """Detalle de un QR por su reference_id."""
        return await self._request("GET", f"/qr_codes/{reference_id}")

    async def get_payments(self, reference_id: str) -> list[dict]:
        """Pagos recibidos en un QR.

        La API 2022-07-31 documenta el filtro por reference_id; si el
        servidor lo rechaza, se reintenta con external_id (nombre de
        la versión anterior). Devuelve lista (vacía si nadie pagó).
        """
        try:
            data = await self._request(
                "GET", "/qr_codes/payments",
                params={"reference_id": reference_id})
        except XenditError as exc:
            if exc.status_code in (400, 422):
                data = await self._request(
                    "GET", "/qr_codes/payments",
                    params={"external_id": reference_id})
            else:
                raise
        return data if isinstance(data, list) else []

    async def simulate_payment(self, reference_id: str,
                               amount_idr: int) -> dict:
        """Simula un pago en modo TEST (solo con claves xnd_development_).

        Xendit envía el webhook real a la callback URL de test, así que
        sirve para probar el flujo completo sin dinero real.
        """
        return await self._request(
            "POST", f"/qr_codes/{reference_id}/payments/simulate",
            json={"amount": amount_idr})
