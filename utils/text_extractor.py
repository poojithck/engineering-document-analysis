"""Text/table extraction via pdfplumber. Steelworks + New/Proposed/To-be scanning."""
import logging
import re
from pathlib import Path
from typing import Optional
import pdfplumber
from config.settings import settings

logger = logging.getLogger(__name__)

class TextExtractor:
    def __init__(self, pdf_path: str):
        self.pdf_path = Path(pdf_path)
        self._pdf = pdfplumber.open(str(self.pdf_path))
        self.total_pages = len(self._pdf.pages)

    def extract_page_text(self, pn: int) -> str:
        try:
            return (self._pdf.pages[pn - 1].extract_text() or "").strip()
        except Exception:
            return ""

    def extract_page_tables(self, pn: int) -> list:
        try:
            return self._pdf.pages[pn - 1].extract_tables() or []
        except Exception:
            return []

    def get_page_heading(self, pn: int) -> Optional[str]:
        text = self.extract_page_text(pn)
        if not text:
            return None
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        return lines[0] if lines else None

    def detect_steelworks_content(self, pn: int) -> dict:
        text = self.extract_page_text(pn).lower()
        tables = self.extract_page_tables(pn)
        matched = [kw for kw in settings.processing.steelworks_keywords if kw.lower() in text]
        tables_with_steel = []
        for idx, table in enumerate(tables):
            ttext = " ".join(str(c).lower() for row in table for c in row if c)
            if any(kw.lower() in ttext for kw in settings.processing.steelworks_keywords):
                tables_with_steel.append(idx)
        steel_items = []
        for line in text.split("\n"):
            ll = line.strip().lower()
            if any(kw in ll for kw in ["rhs", "chs", "shs", "uab", "flat bar", "angle",
                    "plate", "bracket", "channel", "beam", "column", "headframe",
                    "collar", "monopole mount", "unistrut"]):
                if len(line.strip()) > 5:
                    steel_items.append(line.strip())
        return {"has_steelworks": bool(matched) or bool(tables_with_steel),
                "matched_keywords": matched[:10], "tables_with_steel": tables_with_steel,
                "steel_items_found": steel_items[:20]}

    def scan_new_scope_keywords(self, pn: int) -> dict:
        text = self.extract_page_text(pn)
        if not text:
            return {"has_new_scope_indicators": False, "new_items": [], "to_be_items": [], "all_matches": []}
        new_items, to_be_items = [], []
        for line in text.split("\n"):
            s = line.strip()
            if not s or len(s) < 4:
                continue
            m = re.match(r'(?i)\b(new\s+\S.{3,80})', s)
            if m:
                new_items.append(m.group(1).strip())
            m = re.match(r'(?i)\b(proposed\s+\S.{3,80})', s)
            if m:
                new_items.append(m.group(1).strip())
            m = re.search(
                r'(?i)(to\s+be\s+(?:installed|supplied|constructed|fabricated|erected|'
                r'mounted|connected|provisioned|provided|fitted|run|laid|terminated|'
                r'commissioned|tested)\S*(?:\s+\S+){0,8})', s)
            if m:
                to_be_items.append(m.group(1).strip())
        all_m = new_items + to_be_items
        return {"has_new_scope_indicators": bool(all_m),
                "new_items": new_items[:30], "to_be_items": to_be_items[:20], "all_matches": all_m[:40]}

    def close(self):
        if self._pdf:
            self._pdf.close()

    def __enter__(self):
        return self
    def __exit__(self, *a):
        self.close()
    def __del__(self):
        try: self.close()
        except Exception: pass
