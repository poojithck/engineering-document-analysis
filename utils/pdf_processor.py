"""
PDF processing via pypdfium2. No auto-rotation. Rotation set externally after Claude check.
"""
import logging
from pathlib import Path
from typing import Optional

import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader

from config.settings import settings

logger = logging.getLogger(__name__)


class PDFProcessor:
    def __init__(self, pdf_path: str, dpi: Optional[int] = None):
        self.pdf_path = Path(pdf_path)
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        self.dpi = dpi or settings.image.dpi
        self.reader = PdfReader(str(self.pdf_path))
        self.total_pages = len(self.reader.pages)
        logger.info(f"Loaded PDF: {self.pdf_path.name} ({self.total_pages} pages)")
        logger.info(f"Rendering at {self.dpi} DPI via pypdfium2...")
        self._rendered_images: list[Image.Image] = self._render_all()
        logger.info(f"Rendered {len(self._rendered_images)} pages")
        self._rotation_map: dict[int, int] = {}

    def _render_all(self) -> list[Image.Image]:
        scale = self.dpi / 72.0
        images = []
        pdf = pdfium.PdfDocument(str(self.pdf_path))
        for i in range(len(pdf)):
            bmp = pdf[i].render(scale=scale)
            img = bmp.to_pil()
            if img.mode != "RGB":
                img = img.convert("RGB")
            images.append(img)
        pdf.close()
        return images

    def set_rotation_map(self, rotation_map: dict[int, int]):
        self._rotation_map = {int(pn): deg for pn, deg in rotation_map.items() if deg in (90, 180, 270)}
        rotated = sorted(self._rotation_map.keys())
        if rotated:
            logger.info(f"Rotation set for {len(rotated)} pages: {rotated}")
        else:
            logger.info("No pages need rotation")

    def needs_rotation(self, pn: int) -> bool:
        return pn in self._rotation_map

    def get_rotation_degrees(self, pn: int) -> int:
        return self._rotation_map.get(pn, 0)

    def get_raw_image(self, pn: int) -> Image.Image:
        return self._rendered_images[pn - 1].copy()

    def get_corrected_image(self, pn: int) -> Image.Image:
        img = self._rendered_images[pn - 1].copy()
        deg = self._rotation_map.get(pn, 0)
        if deg:
            img = img.rotate(-deg, expand=True)
        return img

    def get_thumbnail(self, pn: int, max_px: Optional[int] = None) -> Image.Image:
        max_px = max_px or settings.image.thumbnail_max_px
        img = self._rendered_images[pn - 1].copy()
        w, h = img.size
        scale = max_px / max(w, h)
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        return img

    def get_rendered_dimensions(self, pn: int) -> tuple[int, int]:
        img = self._rendered_images[pn - 1]
        return img.width, img.height

    def get_corrected_dimensions(self, pn: int) -> tuple[int, int]:
        w, h = self.get_rendered_dimensions(pn)
        deg = self._rotation_map.get(pn, 0)
        return (h, w) if deg in (90, 270) else (w, h)

    def save_all_corrected_images(self, out_dir: Path) -> list[Path]:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        paths = []
        for pn in range(1, self.total_pages + 1):
            p = Path(out_dir) / f"page_{pn:03d}.jpg"
            self.get_corrected_image(pn).save(str(p), "JPEG", quality=settings.image.jpeg_quality)
            paths.append(p)
        return paths

    def save_rotated_only(self, out_dir: Path) -> list[Path]:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        paths = []
        for pn in sorted(self._rotation_map.keys()):
            p = Path(out_dir) / f"page_{pn:03d}_rotated.jpg"
            self.get_corrected_image(pn).save(str(p), "JPEG", quality=settings.image.jpeg_quality)
            paths.append(p)
        return paths

    def get_orientation_summary(self) -> dict:
        return {
            "total_pages": self.total_pages, "dpi": self.dpi, "renderer": "pypdfium2",
            "detection_method": "claude_vision",
            "rotated_count": len(self._rotation_map),
            "rotated_pages": sorted(self._rotation_map.keys()),
            "rotation_details": {str(pn): deg for pn, deg in sorted(self._rotation_map.items())},
            "pages": [{"page": i + 1,
                        "rendered": f"{self._rendered_images[i].width}x{self._rendered_images[i].height}",
                        "rotation_cw": self._rotation_map.get(i + 1, 0),
                        "needs_rotation": (i + 1) in self._rotation_map}
                       for i in range(self.total_pages)],
        }
