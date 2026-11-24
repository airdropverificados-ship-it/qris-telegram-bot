"""Renderiza el qr_string de Xendit como imagen PNG.

El qr_string es el contenido del código QRIS: cualquier app de pago
indonesia (GoPay, OVO, DANA, bancos) lo escanea y paga el importe fijo.
"""

from __future__ import annotations

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def qr_png_bytes(qr_string: str, box_size: int = 10,
                 border: int = 4) -> bytes:
    """Genera los bytes de un PNG con el código QR del qr_string."""
    qr = qrcode.QRCode(
        version=None,  # tamaño automático según la longitud
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(qr_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
