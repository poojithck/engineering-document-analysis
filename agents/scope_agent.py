"""
Stage 2: Scope Categorisation.

CRITICAL: Uses call_bedrock_with_continuation() for the main analysis call.
For 52-page documents, the scope JSON easily exceeds 16K tokens and gets
truncated mid-JSON. The continuation logic detects this and asks Claude to
continue, then repairs any remaining truncation.
"""
import json
import logging
from typing import Optional

from agents.base_agent import BaseAgent
from config.prompts import SCOPE_FOLLOWUP_PROMPT, SCOPE_SYSTEM_PROMPT, SCOPE_USER_PROMPT
from utils.artifact_tracker import ArtifactTracker
from utils.image_utils import prepare_composite
from utils.pdf_processor import PDFProcessor

logger = logging.getLogger(__name__)


class ScopeAgent(BaseAgent):
    def __init__(self, tracker: ArtifactTracker, pdf: Optional[PDFProcessor] = None):
        super().__init__(tracker, "stage2")
        self.pdf = pdf

    def run(self, page_index: list[dict], cross_refs: list[dict],
            doc_name: str) -> dict:
        self.tracker.start_stage("stage2")
        logger.info("[Stage 2] Categorising scope")

        items = self._initial(page_index, cross_refs, doc_name) or []
        logger.info(f"[Stage 2] Initial categorisation: {len(items)} items")

        # Resolve ambiguous items with visual confirmation
        amb = [i for i in items
               if i.get("confidence") in ("low", "medium")
               or i.get("depends_on_pages")]
        if amb and self.pdf:
            logger.info(f"[Stage 2] Resolving {len(amb)} ambiguous items...")
            confirmed = self._resolve(amb)
            if confirmed:
                cm = {i.get("item_description", ""): i for i in confirmed}
                for idx, it in enumerate(items):
                    desc = it.get("item_description", "")
                    if desc in cm:
                        items[idx].update(cm[desc])

        cats = {"new_scope": [], "existing_no_change": [],
                "modification": [], "removal": []}
        steel = []
        for it in items:
            cat = it.get("category", "new_scope")
            cats.get(cat, cats["new_scope"]).append(it)
            if it.get("is_steelworks"):
                steel.append(it)

        result = {
            "document_name": doc_name, "total_items": len(items),
            "categories": cats, "all_items": items,
            "steelworks_items": steel, "steelworks_count": len(steel),
            "summary": {k: len(v) for k, v in cats.items()},
        }

        self.tracker.save_json("stage2", "scope_categories.json", cats)
        self.tracker.save_json("stage2", "scope_details.json", result)
        self.tracker.save_json("stage2", "steelworks_scope.json", steel)
        self.tracker.save_markdown("stage2", "stage2_summary.md", self._md(result))

        tokens = self.total_input_tokens + self.total_output_tokens
        self.tracker.complete_stage("stage2", self.api_call_count, tokens)
        logger.info(f"[Stage 2] Done. {result['summary']}. Steel: {len(steel)}")
        return result

    def _initial(self, idx, xrefs, doc):
        """Main scope analysis -- uses continuation for large docs."""
        comp = [{"page_number": e.get("page_number"),
                 "heading": e.get("heading"),
                 "page_type": e.get("page_type"),
                 "content_summary": e.get("content_summary"),
                 "equipment_materials": e.get("equipment_materials", []),
                 "scope_indicators": e.get("scope_indicators", {}),
                 "steelworks_content": e.get("steelworks_content", {})}
                for e in idx]

        user_text = SCOPE_USER_PROMPT.format(
            document_name=doc,
            full_page_index_json=json.dumps(comp, indent=1),
            cross_references_json=json.dumps(xrefs, indent=1))

        # Use continuation-aware call for large responses
        r = self.call_bedrock_with_continuation(
            system_prompt=SCOPE_SYSTEM_PROMPT,
            user_content=[self.build_text_content(user_text)])

        if r.get("continuations"):
            logger.info(f"[Stage 2] Response needed {r['continuations']} "
                        f"continuation(s)")

        p = self.parse_json_response(r["text"], "scope")
        if isinstance(p, dict):
            return p.get("scope_items", p.get("items", []))
        return p if isinstance(p, list) else []

    def _resolve(self, amb):
        pages = set()
        for it in amb:
            for p in (it.get("depends_on_pages", [])
                      + it.get("source_pages", [])):
                if isinstance(p, int):
                    pages.add(p)
        if not pages or not self.pdf:
            return []
        ps = sorted(pages)[:6]
        itxt = "\n".join(f"- {i.get('item_description', '?')}"
                         for i in amb[:15])
        out = []
        for i in range(0, len(ps), 2):
            p1 = ps[i]
            p2 = ps[i + 1] if i + 1 < len(ps) else None
            _, b64 = prepare_composite(
                self.pdf.get_corrected_image(p1),
                self.pdf.get_corrected_image(p2) if p2 else None)
            pstr = f"{p1}" + (f" and {p2}" if p2 else "")
            try:
                r = self.call_bedrock(
                    system_prompt=SCOPE_SYSTEM_PROMPT,
                    user_content=[
                        self.build_image_content(b64),
                        self.build_text_content(
                            SCOPE_FOLLOWUP_PROMPT.format(
                                pages=pstr, ambiguous_items=itxt))])
                p = self.parse_json_response(r["text"], f"amb {pstr}")
                if p:
                    its = (p.get("confirmed_items", p)
                           if isinstance(p, dict) else p)
                    if isinstance(its, list):
                        out.extend(its)
            except Exception as e:
                logger.warning(f"[Stage 2] Resolve failed {pstr}: {e}")
        return out

    def _md(self, r):
        s = r["summary"]
        lines = [
            "# Stage 2: Scope Categorisation", "",
            f"**Total:** {r['total_items']} | New: {s['new_scope']} | "
            f"Existing: {s['existing_no_change']} | "
            f"Mods: {s['modification']} | Remove: {s['removal']}",
            f"**Steelworks:** {r['steelworks_count']}", "",
        ]

        if r["steelworks_items"]:
            lines += ["## Steelworks (for manufacturer quoting)", ""]
            for i in r["steelworks_items"][:30]:
                lines.append(
                    f"- **{i.get('item_description', '?')}** "
                    f"(pages: {i.get('source_pages', [])})")

        lines += ["", "## New Scope", ""]
        for i in r["categories"].get("new_scope", [])[:30]:
            sf = " [STEEL]" if i.get("is_steelworks") else ""
            lines.append(f"- **{i.get('item_description', '?')}**{sf}")

        lines += ["", "## Modifications", ""]
        for i in r["categories"].get("modification", [])[:20]:
            lines.append(f"- {i.get('item_description', '?')}")

        lines += ["", "## Removals", ""]
        for i in r["categories"].get("removal", [])[:20]:
            lines.append(f"- {i.get('item_description', '?')}")

        return "\n".join(lines)
