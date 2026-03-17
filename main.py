#!/usr/bin/env python3
"""
Engineering Document Analysis Pipeline -- Main Orchestrator.

Stages:
  1. Intelligent Page Indexing (Claude vision orientation + steelworks + scope)
  2. Scope Categorisation (with truncation-safe continuation)
  3. Cost Item Extraction (with truncation-safe continuation)
  4. Claimability Determination (with truncation-safe continuation)

Post-pipeline: generates consolidated steelworks.json at run root.
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents.claims_agent import ClaimsAgent
from agents.cost_agent import CostAgent
from agents.indexing_agent import IndexingAgent
from agents.scope_agent import ScopeAgent
from config.settings import settings
from utils.artifact_tracker import ArtifactTracker
from utils.pdf_processor import PDFProcessor
from utils.text_extractor import TextExtractor


def setup_logging(verbose: bool = False):
    """Configure logging. Prevents duplicate handlers on repeated calls."""
    root = logging.getLogger()
    # Remove existing handlers to prevent duplicates
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)


def build_steelworks_report(
    tracker: ArtifactTracker,
    document_name: str,
    page_index: list[dict] = None,
    scope_result: dict = None,
    cost_result: dict = None,
    claims_result: dict = None,
) -> dict:
    """Build consolidated steelworks.json from all completed stages."""
    logger = logging.getLogger("steelworks_report")
    logger.info("Building consolidated steelworks.json...")

    report = {
        "document_name": document_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": tracker.run_id,
        "description": (
            "Consolidated steelworks report for manufacturer quoting. "
            "Contains all structural steel, brackets, headframes, mounting "
            "hardware, platforms, and fabrication items."),
    }

    # Section 1: Pages with steelworks (Stage 1)
    steelworks_pages = []
    if page_index:
        for entry in page_index:
            sc = entry.get("steelworks_content", {})
            if sc.get("is_steelworks"):
                steelworks_pages.append({
                    "page_number": entry.get("page_number"),
                    "heading": entry.get("heading"),
                    "page_type": entry.get("page_type"),
                    "steelworks_type": sc.get("steelworks_type"),
                    "steelworks_items": sc.get("steelworks_items", []),
                    "manufacturer_quotable": sc.get("manufacturer_quotable", False),
                    "tables": [t for t in entry.get("tables", [])
                               if t.get("is_steelworks_table")],
                })
    report["source_pages"] = {
        "count": len(steelworks_pages), "pages": steelworks_pages}

    # Section 2: Scope items (Stage 2)
    scope_steel = []
    if scope_result:
        scope_steel = list(scope_result.get("steelworks_items", []))
        for cat in ["new_scope", "modification", "removal"]:
            for item in scope_result.get("categories", {}).get(cat, []):
                if item.get("is_steelworks") and item not in scope_steel:
                    scope_steel.append(item)
    report["scope_items"] = {
        "count": len(scope_steel), "items": scope_steel}

    # Section 3: Cost line items (Stage 3)
    cost_steel = (cost_result.get("steelworks_cost_items", [])
                  if cost_result else [])
    report["quote_schedule"] = {
        "description": "Cost line items -- send to steel fabricator for pricing",
        "count": len(cost_steel), "items": cost_steel}

    # Section 4: Claimability (Stage 4)
    claims_steel, mfr_quotes = [], []
    if claims_result:
        for bucket in ["claimable_items", "non_claimable_items",
                       "needs_review_items"]:
            for item in claims_result.get(bucket, []):
                if (item.get("needs_manufacturer_quote")
                    or item.get("quote_type") in (
                        "steel_fabrication", "specialist_install")):
                    claims_steel.append(item)
        mfr_quotes = claims_result.get("needs_manufacturer_quote", [])
    report["claimability"] = {
        "steelworks_assessed_count": len(claims_steel),
        "items": claims_steel}
    report["manufacturer_quotes_required"] = {
        "description": "Items needing manufacturer quote before claiming",
        "count": len(mfr_quotes), "items": mfr_quotes}

    # Summary
    report["summary"] = {
        "pages_with_steelworks": len(steelworks_pages),
        "scope_items": len(scope_steel),
        "cost_line_items": len(cost_steel),
        "manufacturer_quotes_needed": len(mfr_quotes),
        "page_numbers": sorted(set(
            p["page_number"] for p in steelworks_pages)),
    }

    tracker.save_root_json("steelworks.json", report)
    logger.info(
        f"steelworks.json: {len(steelworks_pages)} pages, "
        f"{len(scope_steel)} scope, {len(cost_steel)} cost, "
        f"{len(mfr_quotes)} mfr quotes")
    return report


def run_pipeline(input_path: str, output_dir: str, stages=None,
                 resume_from=None, run_id=None, verbose=False):
    setup_logging(verbose)
    logger = logging.getLogger("main")

    input_path = Path(input_path)
    output_dir = Path(output_dir)
    document_name = input_path.stem

    if not input_path.exists():
        logger.error(f"Not found: {input_path}")
        sys.exit(1)

    if stages is None:
        if resume_from:
            start = {"stage1": 1, "stage2": 2, "stage3": 3,
                     "stage4": 4}.get(resume_from, 1)
            stages = list(range(start, 5))
        else:
            stages = [1, 2, 3, 4]

    logger.info("=" * 60)
    logger.info("  Engineering Document Analysis Pipeline")
    logger.info(f"  Document: {document_name}")
    logger.info(f"  Stages: {stages}")
    logger.info(f"  Model: {settings.aws.model_id}")
    logger.info(f"  DPI: {settings.image.dpi}")
    logger.info(f"  Max tokens (large): {settings.aws.max_tokens_large}")
    logger.info(f"  Max continuations: {settings.aws.max_continuations}")
    logger.info(f"  Orientation: Claude vision check")
    logger.info(f"  Features: steelworks, New/Proposed/To-be, "
                f"truncation recovery")
    logger.info("=" * 60)

    tracker = ArtifactTracker(output_dir, run_id)
    tracker.metadata.update({
        "document_name": document_name,
        "model_used": settings.aws.model_id,
        "dpi": settings.image.dpi,
    })

    pdf = PDFProcessor(str(input_path))
    tracker.metadata["total_pages"] = pdf.total_pages

    page_index = None
    cross_references = None
    scope_result = None
    cost_result = None
    claims_result = None

    try:
        # Stage 1
        if 1 in stages:
            logger.info("\n" + "=" * 50)
            logger.info("  STAGE 1: Intelligent Page Indexing")
            logger.info("=" * 50)
            text_extractor = TextExtractor(str(input_path))
            indexing_agent = IndexingAgent(tracker, pdf, text_extractor)
            page_index, cross_references = indexing_agent.run(document_name)
            text_extractor.close()
        elif resume_from and run_id:
            page_index = tracker.load_json("stage1", "page_index.json")
            cross_references = tracker.load_json("stage1", "cross_references.json")

        # Stage 2
        if 2 in stages:
            if not page_index:
                logger.error("Stage 2 needs Stage 1 output")
                sys.exit(1)
            logger.info("\n" + "=" * 50)
            logger.info("  STAGE 2: Scope Categorisation")
            logger.info("=" * 50)
            scope_agent = ScopeAgent(tracker, pdf)
            scope_result = scope_agent.run(
                page_index, cross_references, document_name)
        elif 3 in stages or 4 in stages:
            scope_result = tracker.load_json("stage2", "scope_details.json")

        # Stage 3
        if 3 in stages:
            if not scope_result:
                logger.error("Stage 3 needs Stage 2 output")
                sys.exit(1)
            logger.info("\n" + "=" * 50)
            logger.info("  STAGE 3: Cost Item Extraction")
            logger.info("=" * 50)
            cost_agent = CostAgent(tracker)
            cost_result = cost_agent.run(
                scope_result, page_index, document_name)
        elif 4 in stages:
            cost_result = tracker.load_json("stage3", "cost_breakdown.json")

        # Stage 4
        if 4 in stages:
            if not cost_result:
                logger.error("Stage 4 needs Stage 3 output")
                sys.exit(1)
            logger.info("\n" + "=" * 50)
            logger.info("  STAGE 4: Claimability Determination")
            logger.info("=" * 50)
            claims_agent = ClaimsAgent(tracker)
            claims_result = claims_agent.run(
                cost_result, page_index, document_name)

        # Consolidated steelworks report
        logger.info("\n" + "=" * 50)
        logger.info("  Generating steelworks.json")
        logger.info("=" * 50)
        build_steelworks_report(
            tracker=tracker, document_name=document_name,
            page_index=page_index, scope_result=scope_result,
            cost_result=cost_result, claims_result=claims_result)

        tracker.finalize()

        logger.info("\n" + "=" * 60)
        logger.info("  Pipeline Complete!")
        logger.info(f"  Run ID: {tracker.run_id}")
        logger.info(f"  Output: {tracker.base_dir}")
        logger.info(f"  API Calls: {tracker.metadata['total_api_calls']}")
        logger.info(f"  Tokens: {tracker.metadata['total_tokens']:,}")
        logger.info(f"  Errors: {tracker.metadata['errors_count']}")
        logger.info(f"  Steelworks: {tracker.base_dir / 'steelworks.json'}")
        logger.info("=" * 60)
        return tracker.run_id

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        tracker.finalize()
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Pipeline failed: {e}")
        tracker.log_error("main", "pipeline_error", str(e))
        try:
            tracker.finalize()
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Engineering Document Analysis Pipeline")
    parser.add_argument("--input", "-i", required=True, help="Input PDF")
    parser.add_argument("--output", "-o", default="output/",
                        help="Output directory")
    parser.add_argument("--stages", nargs="+", type=int,
                        choices=[1, 2, 3, 4])
    parser.add_argument("--resume-from",
                        choices=["stage1", "stage2", "stage3", "stage4"])
    parser.add_argument("--run-id", help="Previous run ID for resumption")
    parser.add_argument("--model", help="Override Bedrock model ID")
    parser.add_argument("--dpi", type=int, help="Override image DPI")
    parser.add_argument("--region", help="Override AWS region")
    parser.add_argument("--max-tokens", type=int,
                        help="Override max_tokens_large (default 32768)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.model:
        settings.aws.model_id = args.model
    if args.dpi:
        settings.image.dpi = args.dpi
    if args.region:
        settings.aws.region = args.region
    if args.max_tokens:
        settings.aws.max_tokens_large = args.max_tokens

    run_pipeline(args.input, args.output, args.stages,
                 args.resume_from, args.run_id, args.verbose)


if __name__ == "__main__":
    main()
