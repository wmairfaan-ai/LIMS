"""QR codes as inline SVG (no image libraries needed)."""

import qrcode
import qrcode.image.svg
from flask import request
from markupsafe import Markup

from . import config


def container_url(code):
    base = config.BASE_URL or request.url_root.rstrip("/")
    return f"{base}/c/{code}"


def qr_svg(data):
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    svg = img.to_string(encoding="unicode")
    # Let CSS size it
    svg = svg.replace('width="', 'data-w="', 1).replace('height="', 'data-h="', 1)
    return Markup(svg.replace("<svg ", '<svg class="qr" role="img" aria-label="QR code" ', 1))
