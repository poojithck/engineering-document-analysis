"""
Stage 1: Intelligent Page Indexing.

Phase 1: Claude vision orientation check (replaces dimension heuristics)
Phase 2: Pre-scan for steelworks + New/Proposed/To-be scope keywords
Phase 3: Index pages in pairs via Claude
"""
import io
import base64
import logging
from pathlib import Path
from typing import Optional

from PIL import Image

from agents.base_agent import BaseAgent
from config.prompts import (
    INDEXING_JSON_SCHEMA, INDEXING_SINGLE_PAGE_USER_PROMPT,
    INDEXING_SYSTEM_PROMPT, INDEXING_USER_PROMPT,
    ORIENTATION_CHECK_SYSTEM, ORIENTATION_CHECK_USER,
)
from config.settings import settings
from utils.artifact_tracker import ArtifactTracker
from utils.image_utils import prepare_composite
from utils.pdf_processor import PDFProcessor
from utils.text_extractor import TextExtractor

logger = logging.getLogger(__name__)


class IndexingAgent(BaseAgent):
    def __init__(self, tracker: ArtifactTracker, pdf: PDFProcessor, text: TextExtractor):
        super().__init__(tracker, "stage1")
        self.pdf = pdf
        self.text = text
        self.page_index: list[dict] = []
        self.cross_references: list[dict] = []
        self.steelworks_pages: list[int] = []

    def run(self, doc_name: str) -> tuple[list[dict], list[dict]]:
        self.tracker.start_stage("stage1")
        total = self.pdf.total_pages
        logger.info(f"[Stage 1] Indexing {total} pages")

        img_dir = self.tracker.stage_dirs["stage1"] / "page_images"
        comp_dir = self.tracker.stage_dirs["stage1"] / "composite_images"
        rot_dir = self.tracker.stage_dirs["stage1"] / "rotated_images"

        # Phase 1: Claude orientation check
        logger.info("[Stage 1] Phase 1: Checking page orientations with Claude...")
        rotation_map = self._check_orientations_with_claude()
        self.pdf.set_rotation_map(rotation_map)
        self.tracker.save_json("stage1", "orientation_analysis.json",
                               self.pdf.get_orientation_summary())
        self.pdf.save_all_corrected_images(img_dir)
        self.pdf.save_rotated_only(rot_dir)

        # Phase 2: Pre-scan
        logger.info("[Stage 1] Phase 2: Pre-scanning for steelworks + scope keywords...")
        prescan = {}
        for pn in range(1, total + 1):
            steel = self.text.detect_steelworks_content(pn)
            scope = self.text.scan_new_scope_keywords(pn)
            prescan[pn] = {"steelworks": steel, "scope_keywords": scope}
            if steel["has_steelworks"]:
                self.steelworks_pages.append(pn)
                logger.info(f"  Page {pn}: STEELWORKS -- {steel['matched_keywords'][:5]}")
            if scope["has_new_scope_indicators"]:
                logger.info(f"  Page {pn}: NEW SCOPE -- {len(scope['all_matches'])} items")
        self.tracker.save_json("stage1", "prescan_results.json", prescan)

        # Phase 3: Index in pairs
        logger.info("[Stage 1] Phase 3: Indexing pages with Claude...")
        pn = 1
        while pn <= total:
            if pn + 1 <= total:
                self._pair(pn, pn + 1, doc_name, comp_dir, prescan)
                pn += 2
            else:
                self._single(pn, doc_name, comp_dir, prescan)
                pn += 1

        self.cross_references = [
            {"from_page": e.get("page_number", 0),
             "reference": r.get("reference", ""), "context": r.get("context", "")}
            for e in self.page_index for r in e.get("cross_references", [])
        ]
        self.page_index.sort(key=lambda x: x.get("page_number", 0))

        self.tracker.save_json("stage1", "page_index.json", self.page_index)
        self.tracker.save_json("stage1", "cross_references.json", self.cross_references)
        self.tracker.save_json("stage1", "steelworks_pages.json",
                               {"steelworks_pages": self.steelworks_pages,
                                "count": len(self.steelworks_pages)})
        self.tracker.save_markdown("stage1", "stage1_summary.md", self._summary(doc_name))

        tokens = self.total_input_tokens + self.total_output_tokens
        self.tracker.complete_stage("stage1", self.api_call_count, tokens)
        logger.info(f"[Stage 1] Done. {len(self.page_index)} pages, "
                     f"{len(self.steelworks_pages)} steelworks, "
                     f"{self.api_call_count} API calls")
        return self.page_index, self.cross_references

    # ─── Phase 1: Claude orientation detection ──────────────────────

    def _check_orientations_with_claude(self) -> dict[int, int]:
        total = self.pdf.total_pages
        batch_size = settings.image.orientation_batch_size
        rotation_map: dict[int, int] = {}

        for batch_start in range(0, total, batch_size):
            batch_pages = list(range(
                batch_start + 1, min(batch_start + batch_size + 1, total + 1)))
            logger.info(f"  Orientation check: pages {batch_pages}")

            content = []
            for pn in batch_pages:
                thumb = self.pdf.get_thumbnail(pn)
                buf = io.BytesIO()
                thumb.save(buf, format="JPEG",
                           quality=settings.image.thumbnail_quality)
                b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                content.append(self.build_text_content(f"--- Page {pn} ---"))
                content.append(self.build_image_content(b64))

            page_nums_str = ", ".join(str(p) for p in batch_pages)
            content.append(self.build_text_content(
                ORIENTATION_CHECK_USER.format(
                    count=len(batch_pages), page_numbers=page_nums_str)))

            try:
                result = self.call_bedrock(
                    system_prompt=ORIENTATION_CHECK_SYSTEM,
                    user_content=content,
                    max_tokens=2048, temperature=0.0)
                parsed = self.parse_json_response(
                    result["text"], f"orientation {batch_pages}")

                if parsed and isinstance(parsed, list):
                    for entry in parsed:
                        pn = entry.get("page_number")
                        rot = entry.get("rotation_cw",
                                        entry.get("rotation_needed_cw", 0))
                        readable = entry.get("text_readable", True)
                        reason = entry.get("reason", "")
                        if not readable and rot in (90, 180, 270):
                            rotation_map[pn] = rot
                            logger.info(f"  Page {pn}: NEEDS {rot} CW "
                                        f"rotation -- {reason}")
            except Exception as e:
                logger.warning(f"  Orientation check failed for {batch_pages}: {e}")

        logger.info(f"  Orientation check complete: "
                     f"{len(rotation_map)} pages need rotation")
        return rotation_map

    # ─── Phase 3: Page indexing ─────────────────────────────────────

    def _pair(self, p1, p2, doc, comp_dir, prescan):
        logger.info(f"[Stage 1] Pages {p1} & {p2}")
        i1, i2 = self.pdf.get_corrected_image(p1), self.pdf.get_corrected_image(p2)
        _, b64 = prepare_composite(
            i1, i2, comp_dir / f"composite_p{p1:03d}_p{p2:03d}.jpg")
        t1, t2 = self._txt(p1), self._txt(p2)
        hints = self._hints(p1, p2, prescan)
        rot_note = self._rotation_note(p1, p2)

        user = INDEXING_USER_PROMPT.format(
            page_n=p1, page_n_plus_1=p2, document_name=doc,
            landscape_note=rot_note, text_page_n=t1, text_page_n_plus_1=t2)
        if hints:
            user += f"\n\nPRE-SCAN HINTS:\n{hints}"
        user += "\n\n" + INDEXING_JSON_SCHEMA

        content = [self.build_image_content(b64), self.build_text_content(user)]
        try:
            r = self.call_bedrock(
                system_prompt=INDEXING_SYSTEM_PROMPT, user_content=content)
            entries = self.parse_json_response(r["text"], f"pages {p1}-{p2}")
            if entries and isinstance(entries, list):
                for e in entries:
                    self._enrich(e, p1, p2, prescan)
                    self.page_index.append(e)
            else:
                self._fallback(p1, p2, t1, t2, prescan)
        except Exception as e:
            logger.error(f"[Stage 1] Error pages {p1}-{p2}: {e}")
            self.tracker.log_error("stage1", "processing_error", str(e), [p1, p2])
            self._fallback(p1, p2, t1, t2, prescan)

    def _single(self, p, doc, comp_dir, prescan):
        logger.info(f"[Stage 1] Page {p} (single)")
        img = self.pdf.get_corrected_image(p)
        _, b64 = prepare_composite(img, output_path=comp_dir / f"composite_p{p:03d}.jpg")
        t = self._txt(p)
        hints = self._hints(p, None, prescan)
        rot_note = self._rotation_note(p)

        user = INDEXING_SINGLE_PAGE_USER_PROMPT.format(
            page_n=p, document_name=doc, landscape_note=rot_note, text_page_n=t)
        if hints:
            user += f"\n\nPRE-SCAN HINTS:\n{hints}"
        user += "\n\n" + INDEXING_JSON_SCHEMA

        content = [self.build_image_content(b64), self.build_text_content(user)]
        try:
            r = self.call_bedrock(
                system_prompt=INDEXING_SYSTEM_PROMPT, user_content=content)
            entries = self.parse_json_response(r["text"], f"page {p}")
            if entries and isinstance(entries, list):
                for e in entries:
                    self._enrich(e, p, None, prescan)
                    self.page_index.append(e)
            else:
                self._fallback(p, None, t, None, prescan)
        except Exception as e:
            logger.error(f"[Stage 1] Error page {p}: {e}")
            self._fallback(p, None, t, None, prescan)

    # ─── Helpers ────────────────────────────────────────────────────

    def _hints(self, p1, p2, prescan):
        parts = []
        for pn in [p1, p2]:
            if pn is None or pn not in prescan:
                continue
            s, sc = prescan[pn]["steelworks"], prescan[pn]["scope_keywords"]
            if s["has_steelworks"]:
                parts.append(
                    f"Page {pn} STEELWORKS: kw={s['matched_keywords'][:5]}, "
                    f"items=[{'; '.join(s['steel_items_found'][:8])}]")
            if sc["has_new_scope_indicators"]:
                parts.append(
                    f"Page {pn} NEW SCOPE: [{'; '.join(sc['new_items'][:8])}] "
                    f"To-be: [{'; '.join(sc['to_be_items'][:5])}]")
        return "\n".join(parts)

    def _rotation_note(self, p1, p2=None):
        notes = []
        for pn in [p1, p2]:
            if pn is None:
                continue
            deg = self.pdf.get_rotation_degrees(pn)
            if deg:
                w, h = self.pdf.get_rendered_dimensions(pn)
                cw, ch = self.pdf.get_corrected_dimensions(pn)
                heading = self.text.get_page_heading(pn) or "Unknown"
                notes.append(
                    f"Note: Page {pn} had sideways text and was rotated "
                    f"{deg} deg CW ({w}x{h} -> {cw}x{ch}). "
                    f"Heading: '{heading}'.")
        return "\n".join(notes)

    def _txt(self, pn, mx=3000):
        t = self.text.extract_page_text(pn)
        return t[:mx] if t else "(No text)"

    def _enrich(self, entry, p1, p2, prescan):
        ep = entry.get("page_number")
        pn = p1 if (ep == p1 or p2 is None) else (p2 if ep == p2 else p1)
        deg = self.pdf.get_rotation_degrees(pn)
        if deg:
            entry["orientation"] = "rotated"
            entry["rotation_applied"] = f"{deg}deg_cw"
            entry["rotation_method"] = "claude_vision"
        else:
            entry["orientation"] = "native"

        if pn in prescan:
            st = prescan[pn]["steelworks"]
            if st["has_steelworks"]:
                sc = entry.setdefault("steelworks_content", {})
                sc["prescan_confirmed"] = True
                sc["prescan_keywords"] = st["matched_keywords"][:10]
                existing = sc.get("steelworks_items", [])
                sc["steelworks_items"] = list(
                    set(existing + st["steel_items_found"][:15]))
            sk = prescan[pn]["scope_keywords"]
            if sk["has_new_scope_indicators"]:
                si = entry.setdefault("scope_indicators", {
                    "new_items": [], "modifications": [],
                    "removals": [], "existing_no_change": []})
                existing_new = set(si.get("new_items", []))
                for item in sk["new_items"] + sk["to_be_items"]:
                    if item not in existing_new:
                        si["new_items"].append(item)
                        existing_new.add(item)

    def _fallback(self, p1, p2, t1, t2, prescan):
        for pn, txt in [(p1, t1), (p2, t2)]:
            if pn is None:
                continue
            st = prescan.get(pn, {}).get("steelworks", {})
            sc = prescan.get(pn, {}).get("scope_keywords", {})
            deg = self.pdf.get_rotation_degrees(pn)
            self.page_index.append({
                "page_number": pn, "heading": "INDEXING FAILED",
                "drawing_number": None,
                "page_type": ("steelworks_table"
                              if st.get("has_steelworks") else "other"),
                "orientation": "rotated" if deg else "native",
                "content_summary": f"Failed. Text: {(txt or '')[:200]}",
                "tables": [], "images_drawings": [], "equipment_materials": [],
                "scope_indicators": {
                    "new_items": sc.get("all_matches", [])[:10],
                    "modifications": [], "removals": [],
                    "existing_no_change": []},
                "steelworks_content": {
                    "is_steelworks": st.get("has_steelworks", False),
                    "steelworks_type": ("schedule_table"
                                        if st.get("has_steelworks") else "none"),
                    "steelworks_items": st.get("steel_items_found", [])[:10],
                    "manufacturer_quotable": st.get("has_steelworks", False)},
                "cross_references": [], "notes_annotations": [],
                "confidence": "low",
                "indexing_notes": "Fallback -- API/parse failed",
            })

    def _summary(self, doc_name):
        """Markdown summary. ASCII only (no emojis)."""
        tokens = self.total_input_tokens + self.total_output_tokens
        rot_pages = sorted(self.pdf._rotation_map.keys())

        lines = [
            "# Stage 1: Page Index Summary", "",
            f"**Document:** {doc_name}  ",
            f"**Pages:** {len(self.page_index)} | "
            f"**API Calls:** {self.api_call_count} | "
            f"**Tokens:** {tokens:,}",
            f"**Orientation method:** Claude vision check  ", "",
        ]

        lines += ["## Orientation Analysis", ""]
        if rot_pages:
            lines.append(f"**Rotated pages:** {rot_pages}")
            lines += ["", "| Page | Original | Corrected | Degrees |",
                      "|------|----------|-----------|---------|"]
            for pn in rot_pages:
                w, h = self.pdf.get_rendered_dimensions(pn)
                cw, ch = self.pdf.get_corrected_dimensions(pn)
                deg = self.pdf.get_rotation_degrees(pn)
                lines.append(f"| {pn} | {w}x{h} | {cw}x{ch} | {deg} CW |")
        else:
            lines.append("All pages OK. No rotation needed.")

        lines += ["", "## Steelworks Pages", ""]
        if self.steelworks_pages:
            for e in self.page_index:
                sc = e.get("steelworks_content", {})
                if sc.get("is_steelworks"):
                    n = len(sc.get("steelworks_items", []))
                    lines.append(
                        f"- Page {e['page_number']}: "
                        f"{sc.get('steelworks_type', '?')} -- {n} items")
        else:
            lines.append("None detected.")

        lines += ["", "## Page Summary", "",
                  "| Page | Type | Heading | Rotated | Steel |",
                  "|------|------|---------|---------|-------|"]
        for e in sorted(self.page_index, key=lambda x: x.get("page_number", 0)):
            r = "YES" if e.get("orientation") == "rotated" else ""
            s = ("YES" if e.get("steelworks_content", {}).get("is_steelworks")
                 else "")
            heading = (e.get("heading") or "?")[:50]
            lines.append(
                f"| {e.get('page_number', '?')} | "
                f"{e.get('page_type', '?')} | {heading} | {r} | {s} |")

        ni = [it for e in self.page_index
              for it in e.get("scope_indicators", {}).get("new_items", [])]
        lines += ["", f"## New/Proposed/To-be Items ({len(ni)})", ""]
        for i in ni[:40]:
            lines.append(f"- {i}")

        return "\n".join(lines)
