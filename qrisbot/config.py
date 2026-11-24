"""Configuración del bot: todo sale de variables de entorno / .env.

NUNCA pongas claves reales en el código. Copia `.env.example` a `.env`
y rellena tus valores ahí. El archivo `.env` no se incluye en el ZIP.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """Toda la configuración del proyecto, cargada desde el entorno."""

    bot_token: str
    xendit_api_key: str
    xendit_callback_token: str
    webhook_url: str  # URL pública del webhook, p. ej. https://tu-dominio/webhook/qris
    webhook_port: int = 8080
    admin_id: int | None = None  # Telegram user id del admin (avisos de pagos)
    db_path: str = "qrisbot.db"
    xendit_base_url: str = "https://api.xendit.co"
    # Versión de la API de Xendit para QR Codes (2022-07-31 es la actual:
    # usa reference_id + currency; la callback URL se configura en el
    # panel de Xendit, no en cada petición).
    xendit_api_version: str = "2022-07-31"
    # Fuente de la tasa IDR -> USDT (CoinGecko, API pública sin clave).
    coingecko_url: str = "https://api.coingecko.com/api/v3/simple/price"
    # IPs permitidas para llamar al webhook (vacío = todas). Si se
    # conocen las IPs de salida de Xendit, listarlas aquí reduce la
    # superficie de ataque del callback a casi cero.
    webhook_allowed_ips: tuple[str, ...] = ()
    # Límite de peticiones por minuto y por IP en el webhook.
    webhook_rate_limit_per_minute: int = 120

    @property
    def is_test_mode(self) -> bool:
        """True con claves xnd_development_ (sandbox de Xendit)."""
        return self.xendit_api_key.startswith("xnd_development_")


def load_config(env_path: str | None = None) -> Config:
    """Carga la configuración desde el entorno (y .env si existe).

    Falla rápido con un mensaje claro si falta algo obligatorio.
    """
    load_dotenv(dotenv_path=env_path)

    missing: list[str] = []
    values: dict[str, str] = {}
    for name in ("BOT_TOKEN", "XENDIT_API_KEY", "XENDIT_CALLBACK_TOKEN",
                 "WEBHOOK_URL"):
        value = os.environ.get(name, "").strip()
        if not value:
            missing.append(name)
        values[name] = value
    if missing:
        raise ValueError(
            "Faltan variables de entorno obligatorias: "
            + ", ".join(missing)
            + ". Copia .env.example a .env y rellénalas."
        )

    admin_raw = os.environ.get("ADMIN_ID", "").strip()
    admin_id = int(admin_raw) if admin_raw else None

    webhook_url = values["WEBHOOK_URL"].rstrip("/")
    # En producción el webhook DEBE ser HTTPS: por HTTP el token de
    # callback viajaría en claro y cualquiera podría falsificar pagos.
    # En modo test se permite http (túneles y pruebas locales).
    if (values["XENDIT_API_KEY"].startswith("xnd_production_")
            and not webhook_url.startswith("https://")):
        raise ValueError(
            "En producción (clave xnd_production_) WEBHOOK_URL debe "
            "empezar por https:// — por HTTP el token de callback "
            "viajaría sin cifrar.")

    allowed_ips = tuple(
        ip.strip() for ip in
        os.environ.get("XENDIT_WEBHOOK_IPS", "").split(",")
        if ip.strip())
    rate_limit_raw = os.environ.get(
        "WEBHOOK_RATE_LIMIT_PER_MINUTE", "120").strip()
    try:
        rate_limit = int(rate_limit_raw)
    except ValueError:
        raise ValueError(
            "WEBHOOK_RATE_LIMIT_PER_MINUTE debe ser un número entero, "
            f"no {rate_limit_raw!r}.") from None
    if rate_limit <= 0:
        raise ValueError(
            "WEBHOOK_RATE_LIMIT_PER_MINUTE debe ser mayor que 0.")

    return Config(
        bot_token=values["BOT_TOKEN"],
        xendit_api_key=values["XENDIT_API_KEY"],
        xendit_callback_token=values["XENDIT_CALLBACK_TOKEN"],
        webhook_url=webhook_url,
        webhook_port=int(os.environ.get("WEBHOOK_PORT", "8080")),
        admin_id=admin_id,
        db_path=os.environ.get("DB_PATH", "qrisbot.db"),
        xendit_base_url=os.environ.get(
            "XENDIT_BASE_URL", "https://api.xendit.co").rstrip("/"),
        xendit_api_version=os.environ.get(
            "XENDIT_API_VERSION", "2022-07-31"),
        coingecko_url=os.environ.get(
            "COINGECKO_URL",
            "https://api.coingecko.com/api/v3/simple/price"),
        webhook_allowed_ips=allowed_ips,
        webhook_rate_limit_per_minute=rate_limit,
    )
