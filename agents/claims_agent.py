"""
Stage 4: Claimability Determination.

Uses call_bedrock_with_continuation() for large cost item assessments.
"""
import json
import logging

from agents.base_agent import BaseAgent
from config.prompts import CLAIMS_SYSTEM_PROMPT, CLAIMS_USER_PROMPT
from utils.artifact_tracker import ArtifactTracker

logger = logging.getLogger(__name__)


class ClaimsAgent(BaseAgent):
    def __init__(self, tracker: ArtifactTracker):
        super().__init__(tracker, "stage4")

    def run(self, cost: dict, page_index: list[dict], doc_name: str) -> dict:
        self.tracker.start_stage("stage4")
        logger.info("[Stage 4] Assessing claimability")
        citems = cost.get("cost_items", [])
        logger.info(f"[Stage 4] {len(citems)} cost items to assess")

        rel_types = {"general_notes", "scope_narrative", "cover_page",
                     "power_details", "reference_documents", "steelworks_table"}
        rel = [{"page_number": e.get("page_number"),
                "heading": e.get("heading"),
                "page_type": e.get("page_type"),
                "content_summary": e.get("content_summary"),
                "notes_annotations": e.get("notes_annotations", []),
                "scope_indicators": e.get("scope_indicators", {}),
                "steelworks_content": e.get("steelworks_content", {})}
               for e in page_index if e.get("page_type") in rel_types]

        user_text = CLAIMS_USER_PROMPT.format(
            cost_items_json=json.dumps(citems, indent=1),
            relevant_index_pages_json=json.dumps(rel, indent=1))

        # Use continuation-aware call
        r = self.call_bedrock_with_continuation(
            system_prompt=CLAIMS_SYSTEM_PROMPT,
            user_content=[self.build_text_content(user_text)])

        if r.get("continuations"):
            logger.info(f"[Stage 4] Response needed {r['continuations']} "
                        f"continuation(s)")

        p = self.parse_json_response(r["text"], "claims")
        items = (p.get("assessment_items", [])
                 if isinstance(p, dict) else p) if p else []
        if not isinstance(items, list):
            items = []

        logger.info(f"[Stage 4] Assessed {len(items)} items")

        cl, nc, nr, mfr = [], [], [], []
        for it in items:
            st = it.get("claimability", "needs_review")
            (cl if st == "claimable"
             else nc if st == "non_claimable"
             else nr).append(it)
            if it.get("needs_manufacturer_quote"):
                mfr.append(it)

        result = {
            "document_name": doc_name, "total_assessed": len(items),
            "claimable_count": len(cl), "non_claimable_count": len(nc),
            "needs_review_count": len(nr),
            "needs_manufacturer_quote_count": len(mfr),
            "claimable_items": cl, "non_claimable_items": nc,
            "needs_review_items": nr, "needs_manufacturer_quote": mfr,
        }

        self.tracker.save_json("stage4", "claimable_items.json", cl)
        self.tracker.save_json("stage4", "non_claimable_items.json", nc)
        self.tracker.save_json("stage4", "review_needed.json", nr)
        self.tracker.save_json("stage4", "manufacturer_quotes_needed.json", mfr)
        self.tracker.save_markdown("stage4", "stage4_summary.md", self._md(result))

        tokens = self.total_input_tokens + self.total_output_tokens
        self.tracker.complete_stage("stage4", self.api_call_count, tokens)
        logger.info(f"[Stage 4] Done. Claim: {len(cl)}, Non: {len(nc)}, "
                     f"Review: {len(nr)}, MFR: {len(mfr)}")
        return result

    def _md(self, r):
        lines = [
            "# Stage 4: Claimability", "",
            f"**Assessed:** {r['total_assessed']} | "
            f"Claimable: {r['claimable_count']} | "
            f"Non: {r['non_claimable_count']} | "
            f"Review: {r['needs_review_count']}",
            f"**Manufacturer Quotes Needed:** "
            f"{r['needs_manufacturer_quote_count']}", "",
        ]

        if r["needs_manufacturer_quote"]:
            lines += ["## Manufacturer Quotes Needed", ""]
            for it in r["needs_manufacturer_quote"][:20]:
                desc = it.get("item_description",
                              it.get("description", "?"))
                lines.append(
                    f"- **{desc}** -- {it.get('quote_type', '?')}")

        lines += ["", "## Claimable", ""]
        for it in r["claimable_items"][:30]:
            desc = it.get("item_description",
                          it.get("description", "?"))
            mf = " [MFR QUOTE]" if it.get("needs_manufacturer_quote") else ""
            lines.append(
                f"- **{desc}** ({it.get('claim_category', '?')}){mf}")

        lines += ["", "## Non-Claimable", ""]
        for it in r["non_claimable_items"][:20]:
            desc = it.get("item_description",
                          it.get("description", "?"))
            reason = (it.get("reasoning") or "")[:80]
            lines.append(f"- {desc}: {reason}")

        lines += ["", "## Needs Review", ""]
        for it in r["needs_review_items"][:20]:
            desc = it.get("item_description",
                          it.get("description", "?"))
            lines.append(f"- {desc}")

        return "\n".join(lines)
