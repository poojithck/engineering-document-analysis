"""Image compositing, resizing, base64 encoding."""
import base64
import io
import logging
from pathlib import Path
from typing import Optional
from PIL import Image
from config.settings import settings

logger = logging.getLogger(__name__)

def combine_two_pages_vertically(top: Image.Image, bottom: Image.Image,
                                  gap: int = 20, sep_color=(200, 200, 200)) -> Image.Image:
    tw = max(top.width, bottom.width)
    if top.width != tw:
        top = top.resize((tw, int(top.height * tw / top.width)), Image.LANCZOS)
    if bottom.width != tw:
        bottom = bottom.resize((tw, int(bottom.height * tw / bottom.width)), Image.LANCZOS)
    h = top.height + gap + bottom.height
    comp = Image.new("RGB", (tw, h), (255, 255, 255))
    comp.paste(top, (0, 0))
    for x in range(tw):
        comp.putpixel((x, top.height + gap // 2), sep_color)
    comp.paste(bottom, (0, top.height + gap))
    return comp

def resize_to_api_limits(img: Image.Image, mw=None, mh=None) -> Image.Image:
    mw = mw or settings.image.max_image_width
    mh = mh or settings.image.max_image_height
    w, h = img.size
    s = min(mw / w if w > mw else 1.0, mh / h if h > mh else 1.0)
    return img.resize((int(w * s), int(h * s)), Image.LANCZOS) if s < 1.0 else img

def image_to_base64(img: Image.Image, quality: int = 85) -> tuple[str, int]:
    mx = settings.image.max_image_bytes
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    sz = buf.tell()
    if sz > mx:
        for q in [70, 55, 40, 30]:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q)
            sz = buf.tell()
            if sz <= mx:
                break
    if sz > mx:
        sm = img.resize((int(img.width * 0.7), int(img.height * 0.7)), Image.LANCZOS)
        buf = io.BytesIO()
        sm.save(buf, format="JPEG", quality=60)
        sz = buf.tell()
    return base64.b64encode(buf.getvalue()).decode("utf-8"), sz

def prepare_composite(img1: Image.Image, img2: Optional[Image.Image] = None,
                      output_path: Optional[Path] = None) -> tuple[Image.Image, str]:
    comp = combine_two_pages_vertically(img1, img2) if img2 else img1.copy()
    comp = resize_to_api_limits(comp)
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        comp.save(str(output_path), "JPEG", quality=settings.image.jpeg_quality)
    b64, _ = image_to_base64(comp)
    return comp, b64
