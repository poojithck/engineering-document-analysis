"""
Trade-categorised report generator.

Takes pipeline outputs (page_index, scope, cost, claims) and produces a
clean, intuitive report grouped by work trade sections:

  Antennas | RRUs | Cable Trays | Electrical Works | Steel Works |
  Footing Works | Internals | Signages | Civil Works | RF & Transmission |
  Testing & Commissioning | Transport & Logistics | Other

Outputs:
  - trade_summary.json   (structured, machine-readable)
  - trade_summary.md     (human-readable report)
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from utils.artifact_tracker import ArtifactTracker

logger = logging.getLogger(__name__)

# ── Category mapping ────────────────────────────────────────────────────────
# Maps the cost_item "category" field (from Claude) to a trade section.
# Order here defines display order in the report.

TRADE_SECTIONS = [
    "Antennas",
    "RRUs",
    "Cable Trays & Ladders",
    "Electrical Works",
    "Steel Works",
    "Footing Works",
    "Civil Works",
    "Internals (Rack & Equipment)",
    "RF & Transmission",
    "Signage",
    "Testing & Commissioning",
    "Transport & Logistics",
    "Other",
]

# Keywords used to auto-classify items into trade sections when the
# category field from Claude doesn't map cleanly.
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "Antennas": [
        "antenna", "panel antenna", "dipole", "antenna mount",
        "antenna frame", "tma", "tower mounted amplifier",
        "radome", "shroud",
    ],
    "RRUs": [
        "rru", "remote radio unit", "radio unit", "bbu",
        "baseband", "rru mount", "rru bracket",
    ],
    "Cable Trays & Ladders": [
        "cable tray", "cable ladder", "cable support", "cable route",
        "cable bridge", "cable pit", "cable duct", "conduit",
        "cable containment", "trunking", "cable runway",
    ],
    "Electrical Works": [
        "electrical", "power", "circuit breaker", "switchboard",
        "power cable", "dc power", "ac power", "battery",
        "rectifier", "mains", "meter", "surge", "earthing",
        "earth bar", "earth cable", "lightning", "spd",
        "power supply", "fuse", "mcb", "distribution board",
    ],
    "Steel Works": [
        "steel", "steelwork", "headframe", "bracket", "rhs",
        "chs", "shs", "uab", "flat bar", "angle bar",
        "gusset", "base plate", "chequer plate", "handrail",
        "kickplate", "platform", "collar", "fabricat",
        "galvanis", "hdg", "hot dip", "weld", "bolt grade",
        "purlin", "beam", "column", "bracing", "splice",
        "monopole mount", "pole mount", "tower mount",
        "unistrut", "u-bolt", "clamp", "anchor bolt",
        "steel channel", "steel ladder", "steel frame",
        "structural",
    ],
    "Footing Works": [
        "footing", "foundation", "concrete", "pier",
        "slab", "pad footing", "excavat", "backfill",
        "formwork", "reinforc", "rebar", "grout",
        "screw pile", "pile",
    ],
    "Civil Works": [
        "civil", "trench", "reinstatement", "landscap",
        "paving", "fence", "gate", "access road",
        "compound", "hardstand", "retaining wall",
    ],
    "Internals (Rack & Equipment)": [
        "rack", "cabinet", "shelf", "subrack",
        "patch panel", "odf", "fibre", "fiber",
        "optical", "splice tray", "equipment install",
        "indoor", "internal",
    ],
    "RF & Transmission": [
        "rf", "coax", "coaxial", "feeder", "jumper",
        "connector", "hybrid", "combiner", "splitter",
        "diplexer", "filter", "transmission", "waveguide",
        "rf plumbing", "sama", "tma", "masthead",
    ],
    "Signage": [
        "sign", "signage", "label", "hazard sign",
        "warning sign", "rf sign", "emf sign",
        "danger sign", "safety sign", "emc sign",
    ],
    "Testing & Commissioning": [
        "test", "commission", "integration", "sweep",
        "pim test", "vswr", "certification", "acceptance",
        "inspection", "audit",
    ],
    "Transport & Logistics": [
        "transport", "delivery", "freight", "crane",
        "rigging", "mobilisation", "demobilisation",
        "site establishment", "ewa", "ewp", "elevated work",
        "scaffol", "access", "height safety",
    ],
}


def _classify_item(item: dict) -> str:
    """Determine which trade section an item belongs to.

    Priority:
      1. Explicit is_steelworks_item / is_steelworks flag -> Steel Works
      2. Keyword match on item_description / description
      3. Claude-assigned category mapping
      4. Fallback to 'Other'
    """
    # Steelworks flag takes priority
    if item.get("is_steelworks_item") or item.get("is_steelworks"):
        return "Steel Works"

    # Build search text from description fields
    desc = (
        (item.get("item_description") or item.get("description") or "") + " " +
        (item.get("category") or "")
    ).lower()

    # Keyword match (check most specific sections first)
    best_match = None
    best_count = 0
    for section, keywords in _SECTION_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in desc)
        if hits > best_count:
            best_count = hits
            best_match = section

    if best_match and best_count > 0:
        return best_match

    # Map Claude's broad category to a section
    cat = (item.get("category") or "").lower()
    cat_map = {
        "antenna & rf": "Antennas",
        "antenna": "Antennas",
        "cabling": "Cable Trays & Ladders",
        "cable": "Cable Trays & Ladders",
        "electrical": "Electrical Works",
        "structural & steelworks": "Steel Works",
        "structural": "Steel Works",
        "steelworks": "Steel Works",
        "civil works": "Civil Works",
        "civil": "Civil Works",
        "labour only": "Other",
        "transport": "Transport & Logistics",
        "testing & commissioning": "Testing & Commissioning",
        "equipment": "Internals (Rack & Equipment)",
        "other": "Other",
    }
    for key, section in cat_map.items():
        if key in cat:
            return section

    return "Other"


def _item_summary_line(item: dict, include_claim: bool = False) -> str:
    """One-line summary of a cost/scope item."""
    desc = item.get("item_description", item.get("description", "?"))
    qty = item.get("quantity", "")
    unit = item.get("unit", "")
    qty_str = f" | Qty: {qty} {unit}".rstrip() if qty else ""
    pages = item.get("source_pages", [])
    page_str = f" | pp.{','.join(str(p) for p in pages)}" if pages else ""
    mfr = " [MFR QUOTE]" if item.get("manufacturer_quote_required") or item.get("needs_manufacturer_quote") else ""

    parts = f"**{desc}**{qty_str}{page_str}{mfr}"

    if include_claim:
        claim = item.get("claimability", "")
        if claim:
            tag = {"claimable": "CLAIM", "non_claimable": "NON-CLAIM",
                   "needs_review": "REVIEW"}.get(claim, claim.upper())
            parts += f" [{tag}]"

    return parts


def build_trade_report(
    tracker: ArtifactTracker,
    document_name: str,
    page_index: list[dict] = None,
    scope_result: dict = None,
    cost_result: dict = None,
    claims_result: dict = None,
) -> dict:
    """Build the trade-categorised report from all pipeline outputs.

    Returns the structured report dict and saves both JSON and Markdown
    to the run root directory.
    """
    logger.info("Building trade-categorised report...")

    # ── Gather all cost items (richest data, from Stage 3) ──────────
    cost_items = []
    if cost_result:
        cost_items = list(cost_result.get("cost_items", []))

    # ── Enrich cost items with claimability from Stage 4 ────────────
    if claims_result:
        # Build lookup by description
        claim_lookup: dict[str, dict] = {}
        for bucket in ["claimable_items", "non_claimable_items", "needs_review_items"]:
            for ci in claims_result.get(bucket, []):
                key = ci.get("item_description", ci.get("description", ""))
                if key:
                    claim_lookup[key] = ci

        for item in cost_items:
            desc = item.get("item_description", item.get("description", ""))
            if desc in claim_lookup:
                cl = claim_lookup[desc]
                item["claimability"] = cl.get("claimability", "needs_review")
                item["claim_category"] = cl.get("claim_category", "")
                item["claim_reasoning"] = cl.get("reasoning", "")
                item["needs_manufacturer_quote"] = cl.get("needs_manufacturer_quote", False)

    # ── Classify into trade sections ────────────────────────────────
    trade_buckets: dict[str, list[dict]] = defaultdict(list)
    for item in cost_items:
        section = _classify_item(item)
        item["_trade_section"] = section
        trade_buckets[section].append(item)

    # ── Build structured report ─────────────────────────────────────
    has_claims = claims_result is not None
    sections = []
    total_items = 0
    total_claimable = 0
    total_mfr_quotes = 0

    for section_name in TRADE_SECTIONS:
        items = trade_buckets.get(section_name, [])
        if not items:
            continue
        total_items += len(items)
        claimable = [i for i in items if i.get("claimability") == "claimable"]
        non_claimable = [i for i in items if i.get("claimability") == "non_claimable"]
        needs_review = [i for i in items if i.get("claimability") == "needs_review"]
        mfr = [i for i in items if i.get("manufacturer_quote_required") or i.get("needs_manufacturer_quote")]
        total_claimable += len(claimable)
        total_mfr_quotes += len(mfr)

        # Clean items for JSON (remove internal key)
        clean_items = []
        for item in items:
            ci = {k: v for k, v in item.items() if not k.startswith("_")}
            clean_items.append(ci)

        section_data = {
            "section": section_name,
            "item_count": len(items),
            "items": clean_items,
        }
        if has_claims:
            section_data["claimable_count"] = len(claimable)
            section_data["non_claimable_count"] = len(non_claimable)
            section_data["needs_review_count"] = len(needs_review)
            section_data["manufacturer_quotes_needed"] = len(mfr)

        sections.append(section_data)

    report = {
        "document_name": document_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": tracker.run_id,
        "summary": {
            "total_items": total_items,
            "sections_with_items": len(sections),
            "total_claimable": total_claimable if has_claims else None,
            "total_manufacturer_quotes": total_mfr_quotes,
        },
        "sections": sections,
    }

    # ── Save outputs ────────────────────────────────────────────────
    tracker.save_root_json("trade_summary.json", report)

    md = _build_markdown(report, page_index, scope_result, cost_result, claims_result)
    md_path = tracker.base_dir / "trade_summary.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    logger.info(f"Saved: {md_path}")

    logger.info(
        f"Trade report: {len(sections)} sections, {total_items} items, "
        f"{total_mfr_quotes} manufacturer quotes needed"
    )
    return report


def _build_markdown(
    report: dict,
    page_index: list[dict] = None,
    scope_result: dict = None,
    cost_result: dict = None,
    claims_result: dict = None,
) -> str:
    """Generate clean, readable Markdown grouped by trade section."""
    lines = []
    doc = report["document_name"]
    summ = report["summary"]
    has_claims = claims_result is not None

    # ── Header ──────────────────────────────────────────────────────
    lines += [
        f"# {doc} -- Scope of Works Summary",
        "",
        f"**Run:** {report['run_id']}  ",
        f"**Generated:** {report['generated_at'][:19]}Z  ",
        f"**Total Items:** {summ['total_items']}  ",
    ]
    if has_claims:
        lines.append(f"**Claimable:** {summ['total_claimable']}  ")
    if summ["total_manufacturer_quotes"]:
        lines.append(
            f"**Manufacturer Quotes Needed:** {summ['total_manufacturer_quotes']}  "
        )
    lines.append("")

    # ── Quick count table ───────────────────────────────────────────
    lines += [
        "## Section Overview",
        "",
        "| Section | Items | " + ("Claimable | MFR Quote |" if has_claims else ""),
        "|---------|------:|" + ("----------:|----------:|" if has_claims else ""),
    ]
    for sec in report["sections"]:
        row = f"| {sec['section']} | {sec['item_count']} |"
        if has_claims:
            row += f" {sec.get('claimable_count', '-')} | {sec.get('manufacturer_quotes_needed', 0)} |"
        lines.append(row)
    lines.append("")

    # ── Per-section detail ──────────────────────────────────────────
    lines.append("---")
    lines.append("")

    for sec in report["sections"]:
        name = sec["section"]
        items = sec["items"]
        lines += [f"## {name}", ""]

        count_parts = [f"**{sec['item_count']} items**"]
        if has_claims:
            cl = sec.get("claimable_count", 0)
            nc = sec.get("non_claimable_count", 0)
            rv = sec.get("needs_review_count", 0)
            mfr = sec.get("manufacturer_quotes_needed", 0)
            count_parts.append(f"Claimable: {cl}")
            if nc:
                count_parts.append(f"Non-claimable: {nc}")
            if rv:
                count_parts.append(f"Review: {rv}")
            if mfr:
                count_parts.append(f"MFR Quotes: {mfr}")
        lines.append(" | ".join(count_parts))
        lines.append("")

        for item in items:
            lines.append(f"- {_item_summary_line(item, include_claim=has_claims)}")

        lines += ["", ""]

    # ── Manufacturer Quotes Needed (consolidated) ───────────────────
    mfr_items = []
    for sec in report["sections"]:
        for item in sec["items"]:
            if item.get("manufacturer_quote_required") or item.get("needs_manufacturer_quote"):
                mfr_items.append((sec["section"], item))

    if mfr_items:
        lines += [
            "---",
            "",
            "## Manufacturer Quotes Required (All Sections)",
            "",
        ]
        for section, item in mfr_items:
            desc = item.get("item_description", item.get("description", "?"))
            qty = item.get("quantity", "")
            unit = item.get("unit", "")
            qty_str = f" | Qty: {qty} {unit}".rstrip() if qty else ""
            qt = item.get("quote_type", "")
            qt_str = f" ({qt})" if qt else ""
            lines.append(f"- **[{section}]** {desc}{qty_str}{qt_str}")
        lines.append("")

    # ── Source pages reference ──────────────────────────────────────
    if page_index:
        lines += [
            "---",
            "",
            "## Source Page Reference",
            "",
            "| Page | Type | Heading | Steel |",
            "|------|------|---------|-------|",
        ]
        for e in sorted(page_index, key=lambda x: x.get("page_number", 0)):
            s = "YES" if e.get("steelworks_content", {}).get("is_steelworks") else ""
            heading = (e.get("heading") or "?")[:50]
            lines.append(
                f"| {e.get('page_number', '?')} | "
                f"{e.get('page_type', '?')} | {heading} | {s} |"
            )
        lines.append("")

    return "\n".join(lines)
