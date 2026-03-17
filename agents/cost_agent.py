"""
Stage 3: Cost Item Extraction.

CRITICAL: Uses call_bedrock_with_continuation() for the main cost analysis.
Large documents produce extensive cost breakdowns that exceed token limits.
"""
import json
import logging

from agents.base_agent import BaseAgent
from config.prompts import COST_SYSTEM_PROMPT, COST_USER_PROMPT
from utils.artifact_tracker import ArtifactTracker

logger = logging.getLogger(__name__)


class CostAgent(BaseAgent):
    def __init__(self, tracker: ArtifactTracker):
        super().__init__(tracker, "stage3")

    def run(self, scope: dict, page_index: list[dict], doc_name: str) -> dict:
        self.tracker.start_stage("stage3")
        billable = []
        for cat in ["new_scope", "modification", "removal"]:
            for it in scope.get("categories", {}).get(cat, []):
                it["scope_category"] = cat
                billable.append(it)
        logger.info(f"[Stage 3] {len(billable)} billable items")

        cidx = [{"page_number": e.get("page_number"),
                 "heading": e.get("heading"),
                 "page_type": e.get("page_type"),
                 "content_summary": e.get("content_summary"),
                 "tables": e.get("tables", []),
                 "equipment_materials": e.get("equipment_materials", []),
                 "steelworks_content": e.get("steelworks_content", {})}
                for e in page_index]

        user_text = COST_USER_PROMPT.format(
            scope_categories_json=json.dumps(billable, indent=1),
            page_index_json=json.dumps(cidx, indent=1))

        # Use continuation-aware call for large responses
        r = self.call_bedrock_with_continuation(
            system_prompt=COST_SYSTEM_PROMPT,
            user_content=[self.build_text_content(user_text)])

        if r.get("continuations"):
            logger.info(f"[Stage 3] Response needed {r['continuations']} "
                        f"continuation(s)")

        p = self.parse_json_response(r["text"], "cost")
        items = (p.get("cost_items", []) if isinstance(p, dict) else p) if p else []
        if not isinstance(items, list):
            items = []

        logger.info(f"[Stage 3] Extracted {len(items)} cost items")

        steel = [i for i in items if i.get("is_steelworks_item")]
        bd = {}
        for i in items:
            bd.setdefault(i.get("category", "Other"), []).append(i)

        result = {
            "document_name": doc_name, "total_cost_items": len(items),
            "cost_items": items, "steelworks_cost_items": steel,
            "steelworks_count": len(steel), "breakdown": bd,
            "breakdown_summary": {k: len(v) for k, v in bd.items()},
        }

        self.tracker.save_json("stage3", "cost_items.json", items)
        self.tracker.save_json("stage3", "cost_breakdown.json", result)
        self.tracker.save_json("stage3", "steelworks_quote_schedule.json",
                               {"document_name": doc_name,
                                "description": "Steelworks items for manufacturer quoting",
                                "items": steel, "total_items": len(steel)})
        self.tracker.save_markdown("stage3", "stage3_summary.md", self._md(result))

        tokens = self.total_input_tokens + self.total_output_tokens
        self.tracker.complete_stage("stage3", self.api_call_count, tokens)
        logger.info(f"[Stage 3] Done. {len(items)} items ({len(steel)} steelworks)")
        return result

    def _md(self, r):
        lines = [
            "# Stage 3: Cost Items", "",
            f"**Total:** {r['total_cost_items']} | "
            f"**Steelworks:** {r['steelworks_count']}",
            "", "## Breakdown", "",
            "| Category | Count |", "|----------|-------|",
        ]
        for c, n in sorted(r.get("breakdown_summary", {}).items()):
            lines.append(f"| {c} | {n} |")

        if r["steelworks_cost_items"]:
            lines += ["", "## Steelworks Quote Schedule", ""]
            for i, it in enumerate(r["steelworks_cost_items"][:30], 1):
                lines.append(
                    f"{i}. **{it.get('item_description', '?')}** -- "
                    f"Qty: {it.get('quantity', '?')} {it.get('unit', '')}")

        lines += ["", "## All Items", ""]
        for i, it in enumerate(r.get("cost_items", [])[:50], 1):
            sf = " [STEEL]" if it.get("is_steelworks_item") else ""
            mf = " [MFR QUOTE]" if it.get("manufacturer_quote_required") else ""
            lines.append(
                f"{i}. **{it.get('item_description', '?')}**{sf}{mf} | "
                f"{it.get('category', '?')} | "
                f"Qty: {it.get('quantity', '?')} {it.get('unit', '')}")
        return "\n".join(lines)
