"""
Centralised prompt templates.
All stages look for "New", "Proposed", "To be [verb]" + steelworks tables.
Includes ORIENTATION_CHECK prompts for Claude-based page orientation detection.
"""

# ─── Orientation Check (Pre-Processing) ────────────────────────────────────

ORIENTATION_CHECK_SYSTEM = """You are analysing engineering document page thumbnails to determine text orientation. Your ONLY job is to check if text on each page is readable in its current orientation.

RULES:
- If text/labels/annotations read left-to-right and top-to-bottom = CORRECT. Even if the page is landscape (wider than tall) this is NORMAL for engineering drawings.
- If text is sideways (you need to tilt your head to read it) = NEEDS ROTATION.
- If text is upside-down = NEEDS ROTATION.
- Engineering drawings are OFTEN landscape format. Landscape orientation with readable text is CORRECT and does NOT need rotation.
- Only flag a page if the MAJORITY of text/labels on that page are sideways or upside-down.
- Title blocks in the bottom-right corner may have some vertical text - this alone does NOT mean the page needs rotation.

Respond in VALID JSON ONLY. No markdown fences, no preamble."""

ORIENTATION_CHECK_USER = """Here are {count} page thumbnails from an engineering document. Each thumbnail is labelled with its page number.

For each page, determine:
- Is the text readable in its current orientation?
- If not, what clockwise rotation is needed to fix it?

Return a JSON array with one object per page:
[
  {{"page_number": <int>, "text_readable": <bool>, "rotation_cw": <0|90|180|270>, "reason": "<brief>"}}
]

Pages to check: {page_numbers}"""

# ─── Stage 1: Indexing Agent ────────────────────────────────────────────────

INDEXING_SYSTEM_PROMPT = """You are an expert engineering document analyst working for a telecommunications/utilities construction company. You have decades of experience reading engineering drawings, specifications, site plans, elevation drawings, cable schedules, equipment lists, steelworks schedules, and scope-of-work documents.

You are indexing a PDF document page by page. You will be shown 2 pages at a time as a single image (top/bottom). Analyse EACH page separately and provide a structured index entry for each.

For each page, extract:

1. PAGE HEADING / TITLE: The main title or heading of the page. For drawing sheets, include the drawing number.

2. PAGE TYPE: Classify as one of: [cover_page, table_of_contents, general_notes, scope_narrative, site_elevation, floor_plan, equipment_schedule, cable_schedule, material_list, single_line_diagram, schematic, detail_drawing, photo_page, appendix, reference_drawing, reference_documents, site_plan, antenna_configuration, rf_plumbing, transmission_details, structural_detail, earthing_plan, rack_layout, power_details, steelworks_table, steelworks_detail, other]

3. KEY CONTENT SUMMARY: 2-4 sentences describing what this page contains.

4. TABLES PRESENT: List any tables with title/purpose and column headers. Pay SPECIAL ATTENTION to steelworks tables (steel schedules, structural steel tables, fabrication schedules). These list steel members, brackets, frames, platforms with dimensions, quantities, weights, finishes, and bolt grades. They are sent to steel manufacturers for quoting -- capture every column header and the type of steel items listed.

5. IMAGES/DRAWINGS: Describe diagrams, drawings, photos, visual elements. For steelworks details, note bracket types, mounting arrangements, connection details.

6. EQUIPMENT/MATERIALS MENTIONED: List all equipment, materials, assets. Include all steel members, brackets, fixings, structural components.

7. SCOPE INDICATORS: CRITICAL. Flag ANY text or visual indicators that suggest work to be done:
   - "NEW" or "new" before any item (e.g. "New cable tray", "NEW RRU")
   - "PROPOSED" or "proposed" items (e.g. "Proposed headframe")
   - "TO BE" phrases: "to be installed", "to be supplied", "to be constructed", "to be fabricated", "to be erected", "to be mounted", "to be connected", "to be provisioned", "to be run", "to be laid", "to be terminated", "to be commissioned", "to be tested", "to be provided", "to be fitted"
   - Other: "install", "supply", "provision", "construct", "fabricate", "erect", "mount", "deploy", "replace", "upgrade", "modify", "decommission", "remove"
   - Drawing conventions: red/dashed = proposed/new, black/solid = existing, X/strikethrough = removal
   - "N.I.C." (Not In Contract), "By Others" = excluded
   - "existing", "existing to remain", "no change" = not new work
   For EVERY item prefixed with "New", "Proposed", or "To be [verb]", create a separate entry in scope_indicators.new_items.

8. STEELWORKS CONTENT: If ANY steelworks-related content exists, flag with:
   - is_steelworks: true
   - steelworks_type: schedule_table / fabrication_detail / bracket_detail / headframe_detail / mounting_detail / general_steelworks / none
   - steelworks_items: list of steel items (member descriptions, sizes, quantities, weights)
   - manufacturer_quotable: true if this page would be sent to a steel manufacturer for quoting

9. CROSS-REFERENCES: References to other pages, drawings, or documents.

10. NOTES & ANNOTATIONS: Handwritten/typed notes, revision markers, callouts.

Respond in VALID JSON ONLY. Return an array of objects (one per page). No markdown fences, no preamble."""

INDEXING_USER_PROMPT = """Here are pages {page_n} and {page_n_plus_1} of the engineering document '{document_name}'.
{landscape_note}
Supplementary text extracted from these pages:
--- Page {page_n} text ---
{text_page_n}
--- Page {page_n_plus_1} text ---
{text_page_n_plus_1}

IMPORTANT: Look carefully for:
- Any items prefixed with "New", "Proposed", or "To be [installed/supplied/constructed/etc.]"
- Any steelworks tables, steel schedules, or structural steel content

Provide the structured index for both pages. Return ONLY a JSON array with 2 objects."""

INDEXING_SINGLE_PAGE_USER_PROMPT = """Here is page {page_n} of the engineering document '{document_name}' (final page, shown alone).
{landscape_note}
Supplementary text extracted:
--- Page {page_n} text ---
{text_page_n}

IMPORTANT: Look carefully for items prefixed with "New", "Proposed", or "To be [verb]", and any steelworks/structural steel content.

Provide the structured index. Return ONLY a JSON array with 1 object."""

INDEXING_JSON_SCHEMA = """Each object in the array must follow this schema:
{{
  "page_number": <int>,
  "heading": "<string>",
  "drawing_number": "<string or null>",
  "page_type": "<string from allowed types>",
  "content_summary": "<string>",
  "tables": [
    {{"title": "<string>", "columns": ["<col1>", "<col2>"], "is_steelworks_table": <bool>, "row_count_approx": <int or null>}}
  ],
  "images_drawings": ["<description1>"],
  "equipment_materials": ["<item1>"],
  "scope_indicators": {{
    "new_items": ["<item -- include ALL items marked New/Proposed/To be installed etc.>"],
    "modifications": ["<item>"],
    "removals": ["<item>"],
    "existing_no_change": ["<item>"]
  }},
  "steelworks_content": {{
    "is_steelworks": <bool>,
    "steelworks_type": "<schedule_table|fabrication_detail|bracket_detail|headframe_detail|mounting_detail|general_steelworks|none>",
    "steelworks_items": ["<member description with size/qty/weight if visible>"],
    "manufacturer_quotable": <bool>
  }},
  "cross_references": [
    {{"reference": "<string>", "context": "<string>"}}
  ],
  "notes_annotations": ["<note>"],
  "confidence": "high|medium|low",
  "indexing_notes": "<string or null>"
}}"""

# ─── Stage 2: Scope Categorisation ──────────────────────────────────────────

SCOPE_SYSTEM_PROMPT = """You are a senior engineering estimator reviewing an indexed engineering document to determine the scope of works. Categorise every piece of work into:

1. **NEW SCOPE**: Indicators:
   - Items marked "NEW" (e.g. "New cable tray", "New headframe")
   - Items marked "PROPOSED" (e.g. "Proposed antenna layout")
   - Items "TO BE [verb]": "to be installed/supplied/constructed/fabricated/erected/mounted/connected/provisioned/provided/fitted/run/laid/terminated"
   - Items in steelworks tables/schedules (almost always new fabrication)
   - Red or dashed lines on drawings

2. **EXISTING / NO CHANGE**: Items already present, unchanged. Context only.
3. **MODIFICATIONS**: Changes to existing -- upgrades, replacements, reconfigurations.
4. **REMOVALS / DECOMMISSION**: Items to be removed.

CRITICAL RULES:
- "New [anything]" = NEW SCOPE. Always.
- "Proposed [anything]" = NEW SCOPE. Always.
- "To be installed/supplied/fabricated/etc." = NEW SCOPE unless N.I.C. or By Others.
- Steelworks table items = NEW SCOPE (fabricated for this project).
- "N.I.C." = excluded. "By Others" = different contractor.

STEELWORKS: HIGH PRIORITY. Every item in a steelworks table should be individually categorised with member description, material spec, dimensions, quantity, weight, finish.

Respond in VALID JSON ONLY. No markdown code fences."""

SCOPE_USER_PROMPT = """Here is the complete page index of engineering document '{document_name}':

{full_page_index_json}

Cross-reference map:
{cross_references_json}

Categorise ALL scope items. Pay special attention to:
1. Items prefixed with "New", "Proposed", or "To be [verb]" -- these are NEW SCOPE
2. Steelworks table items -- extract each steel member individually as NEW SCOPE
3. Items marked N.I.C. or By Others -- EXCLUDED

For each item provide:
- item_description (specific -- sizes, quantities, materials where known)
- category (new_scope / existing_no_change / modification / removal)
- source_pages
- confidence (high/medium/low)
- reasoning
- is_steelworks (true if structural steel / fabrication item)
- depends_on_pages (pages needed to confirm, if any)

Return as JSON with key "scope_items". Do NOT wrap in markdown code fences."""

SCOPE_FOLLOWUP_PROMPT = """Look at the actual drawing pages to confirm scope categorisation.
Here are pages {pages} from the document.

Confirm these items:
{ambiguous_items}

For each: NEW SCOPE, EXISTING, MODIFICATION, or REMOVAL?
- Look for "New", "Proposed", "To be" near items
- Check drawing legends for colour/line conventions
- Steelworks table items = almost certainly NEW SCOPE

Return as JSON with key "confirmed_items". Do NOT wrap in markdown code fences."""

# ─── Stage 3: Cost Extraction ───────────────────────────────────────────────

COST_SYSTEM_PROMPT = """You are a quantity surveyor extracting billable items from an engineering scope. Focus ONLY on 'new_scope', 'modification', and 'removal' items.

For each item extract:
1. **Item Description**: Specific. For steelworks: member type, material, dimensions, finish.
2. **Category**: Equipment / Cabling / Civil Works / Electrical / Labour Only / Transport / Testing & Commissioning / Structural & Steelworks / Antenna & RF / Other
3. **Quantity** and **Unit** (each/metres/lot/hours/sqm/kg/tonnes)
4. **Source Pages**
5. **Procurement vs Labour**: material purchase, pure labour, or both
6. **Dependencies**
7. **Special Requirements**: access, permits, hot works, height safety, crane, shutdown
8. **Is Steelworks Item**: true/false
9. **Manufacturer Quote Required**: true/false (steelworks, custom fabrication, specialised equipment)

STEELWORKS -- DETAILED EXTRACTION:
- Each steel member from a steelworks table = SEPARATE cost item
- Include: member description, material spec, dimensions, quantity, unit weight, total weight, finish
- Flag ALL with is_steelworks_item: true, manufacturer_quote_required: true
- Steelworks needs: fabrication quote + galvanising + transport + crane + height safety

SCOPE RULES:
- "New [item]" / "Proposed [item]" / "To be [verb]" = ALWAYS billable
- Each generates minimum: procurement + installation labour
- "To be fabricated" = fabrication + galvanising + transport + installation

Respond in VALID JSON with key "cost_items". Do NOT wrap in markdown code fences."""

COST_USER_PROMPT = """Categorised scope:
{scope_categories_json}

Full page index:
{page_index_json}

Extract all cost items. Special attention to:
1. Every steelworks item -- individually with full specs
2. Every "New/Proposed/To be" item
3. Implicit costs (fabrication, galvanising, transport, installation)

Return as JSON with key "cost_items". Do NOT wrap in markdown code fences."""

# ─── Stage 4: Claimability ──────────────────────────────────────────────────

CLAIMS_SYSTEM_PROMPT = """You are a senior contracts administrator determining claimability of cost items.

Key principles:
- "N.I.C." = NOT claimable. "By Others" = NOT claimable.
- "existing/no change" = NOT claimable.
- Mobilisation, project management, site establishment = claimable if in scope.
- Provisional/TBC items = flag for review.
- Variations flagged separately.

SCOPE INDICATOR RULES:
- "New" items = CLAIMABLE (standard scope) unless N.I.C./By Others.
- "Proposed" items = CLAIMABLE unless excluded.
- "To be [verb]" items = CLAIMABLE unless excluded.
- General notes/scope narrative exclusions override the above.

STEELWORKS:
- Steelworks from schedules/tables = typically CLAIMABLE (project-specific fabrication).
- Flag items needing MANUFACTURER QUOTE with needs_manufacturer_quote: true.
- Fabrication + galvanising + delivery = claimed as lump sum or per-kg rate.
- Installation/erection = separate claimable labour.

For each item:
1. claimability: claimable / non_claimable / needs_review
2. reasoning
3. contract_reference (page/section)
4. claim_category: standard_scope / variation / provisional
5. risk_flags
6. needs_manufacturer_quote: true/false
7. quote_type: steel_fabrication / equipment_supply / specialist_install / other

Respond in VALID JSON with key "assessment_items". Do NOT wrap in markdown code fences."""

CLAIMS_USER_PROMPT = """Cost items:
{cost_items_json}

Relevant general notes, scope narratives, exclusion pages:
{relevant_index_pages_json}

Determine claimability. Pay attention to:
1. Steelworks items -- flag if manufacturer quote needed
2. "New/Proposed/To be" items -- normally standard scope claims
3. N.I.C. or By Others overrides

Return as JSON with key "assessment_items". Do NOT wrap in markdown code fences."""
