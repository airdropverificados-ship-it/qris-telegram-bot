"""Servidor webhook (aiohttp) que recibe los callbacks de Xendit.

Endpoints:
- POST /webhook/qris : callback de pago QRIS ("qr.payment").
  Antes de procesar nada: lista de IPs permitidas (si está
  configurada) y límite de tasa por IP. Después verifica la
  cabecera x-callback-token y responde 401 si no coincide.
  Responde 200 rápido siempre que el token sea válido (Xendit
  reintenta si no recibe 2xx).
- GET /health : comprobación rápida de que el servidor está vivo.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque

from aiohttp import web

from .config import Config
from .payments import PaymentsService

log = logging.getLogger(__name__)


class RateLimiter:
    """Ventana deslizante por clave (IP), en memoria, sin dependencias."""

    def __init__(self, max_requests: int, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True


def ip_allowed(ip: str | None, allowed: tuple[str, ...]) -> bool:
    """Si no hay lista configurada, todo pasa; si la hay, solo esas IP."""
    if not allowed:
        return True
    return bool(ip) and ip in allowed


def create_app(service: PaymentsService, config: Config) -> web.Application:
    app = web.Application()
    limiter = RateLimiter(config.webhook_rate_limit_per_minute)

    async def qris_webhook(request: web.Request) -> web.Response:
        ip = request.remote
        if not ip_allowed(ip, config.webhook_allowed_ips):
            log.warning("Webhook rechazado: IP no permitida: %s", ip)
            return web.json_response({"error": "forbidden"}, status=403)
        if not limiter.allow(ip or "unknown"):
            log.warning("Webhook limitado por tasa: IP %s", ip)
            return web.json_response({"error": "rate limited"},
                                     status=429)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            log.warning("Webhook con JSON inválido")
            return web.json_response({"error": "invalid json"},
                                     status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid payload"},
                                     status=400)
        token = request.headers.get("x-callback-token")
        status, body = await service.handle_webhook(payload, token)
        return web.json_response(body, status=status)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_post("/webhook/qris", qris_webhook)
    app.router.add_get("/health", health)
    return app


async def start_webhook_server(service: PaymentsService,
                               config: Config) -> web.AppRunner:
    """Arranca el servidor en WEBHOOK_PORT y devuelve el runner
    (para pararlo limpiamente al cerrar)."""
    app = create_app(service, config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.webhook_port)
    await site.start()
    log.info("Webhook escuchando en puerto %s (POST /webhook/qris)",
             config.webhook_port)
    log.info("Configura esta URL en Xendit (Settings -> Callbacks): %s",
             config.webhook_url)
    return runner
