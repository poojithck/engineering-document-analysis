"""
Configuration settings for the Engineering Document Analysis Pipeline.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AWSConfig:
    region: str = os.environ.get("AWS_REGION", "ap-southeast-2")
    model_id: str = "global.anthropic.claude-opus-4-6-v1"
    max_retries: int = 3
    base_backoff_seconds: float = 2.0
    # Default max tokens for short calls (orientation check, etc.)
    max_tokens: int = 30000
    # Max tokens for large analysis calls (scope, cost, claims on full docs)
    # Claude Opus supports up to 128k output tokens
    max_tokens_large: int = 52768
    # Max continuation attempts when response is truncated
    max_continuations: int = 3


@dataclass
class ImageConfig:
    dpi: int = 200
    jpeg_quality: int = 85
    max_image_width: int = 1568
    max_image_height: int = 4000
    max_image_bytes: int = 5 * 1024 * 1024
    fallback_dpi: int = 150
    pages_per_composite: int = 2
    thumbnail_max_px: int = 600
    thumbnail_quality: int = 50
    orientation_batch_size: int = 6


@dataclass
class ProcessingConfig:
    supported_page_types: list = field(default_factory=lambda: [
        "cover_page", "table_of_contents", "general_notes", "scope_narrative",
        "site_elevation", "floor_plan", "equipment_schedule", "cable_schedule",
        "material_list", "single_line_diagram", "schematic", "detail_drawing",
        "photo_page", "appendix", "reference_drawing", "reference_documents",
        "site_plan", "antenna_configuration", "rf_plumbing", "transmission_details",
        "structural_detail", "earthing_plan", "rack_layout", "power_details",
        "steelworks_table", "steelworks_detail", "other"
    ])
    landscape_keywords: list = field(default_factory=lambda: [
        "elevation", "site plan", "layout", "floor plan", "section",
        "detail", "schematic", "diagram", "site elevation", "earthing plan",
        "single line diagram", "cable ladder", "rack layout", "antenna",
        "rf plumbing", "transmission", "general arrangement"
    ])
    new_scope_keywords: list = field(default_factory=lambda: [
        "new", "proposed", "to be installed", "to be supplied",
        "to be constructed", "to be provisioned", "to be mounted",
        "to be connected", "to be fabricated", "to be erected",
        "to be provided", "to be fitted", "to be run", "to be laid",
        "to be terminated", "to be commissioned", "to be tested",
        "install", "supply", "provision", "construct", "fabricate",
        "erect", "mount", "deploy"
    ])
    steelworks_keywords: list = field(default_factory=lambda: [
        "steelwork", "steel works", "steel schedule", "steel table",
        "steel fabrication", "structural steel", "steel member",
        "steel bracket", "steel frame", "headframe", "collar",
        "monopole", "pole mount", "tower mount", "mounting bracket",
        "steel platform", "steel ladder", "steel channel",
        "unistrut", "u-bolt", "clamp", "hot dip galvanised",
        "hdg", "galvanised", "rhs", "chs", "shs", "uab",
        "flat bar", "angle bar", "plate", "gusset", "base plate",
        "anchor bolt", "chequer plate", "handrail", "kickplate",
        "purlin", "beam", "column", "bracing", "splice",
        "weld", "bolt grade", "m16", "m20", "m24",
        "weight", "kg", "length", "finish", "surface treatment"
    ])


@dataclass
class Settings:
    aws: AWSConfig = field(default_factory=AWSConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)


settings = Settings()
