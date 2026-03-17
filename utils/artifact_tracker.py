"""Artifact tracking. All writes use encoding='utf-8' for Windows compat."""
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

class ArtifactTracker:
    def __init__(self, output_base: Path, run_id: Optional[str] = None):
        self.run_id = run_id or str(uuid.uuid4())[:8]
        self.base_dir = Path(output_base) / self.run_id
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.stage_dirs = {
            f"stage{i}": self.base_dir / name
            for i, name in [(1, "stage1_indexing"), (2, "stage2_categorisation"),
                            (3, "stage3_costing"), (4, "stage4_claimability")]
        }
        for d in self.stage_dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        for sub in ["page_images", "composite_images", "rotated_images"]:
            (self.stage_dirs["stage1"] / sub).mkdir(exist_ok=True)
        self.metadata = {
            "run_id": self.run_id, "document_name": None, "total_pages": 0,
            "model_used": None, "dpi": None, "renderer": "pypdfium2",
            "started_at": datetime.now(timezone.utc).isoformat(), "completed_at": None,
            "stage_timings": {}, "total_api_calls": 0, "total_tokens": 0, "errors_count": 0,
        }
        self.errors: list[dict] = []

    def save_json(self, stage: str, filename: str, data: Any) -> Path:
        fp = self.stage_dirs[stage] / filename
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        return fp

    def load_json(self, stage: str, filename: str) -> Any:
        fp = self.stage_dirs[stage] / filename
        if not fp.exists():
            raise FileNotFoundError(f"Not found: {fp}")
        with open(fp, encoding="utf-8") as f:
            return json.load(f)

    def save_markdown(self, stage: str, filename: str, content: str) -> Path:
        fp = self.stage_dirs[stage] / filename
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        return fp

    def save_root_json(self, filename: str, data: Any) -> Path:
        fp = self.base_dir / filename
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        logger.info(f"Saved: {fp}")
        return fp

    def log_error(self, stage: str, error_type: str, message: str,
                  page_numbers: Optional[list[int]] = None, raw_response: Optional[str] = None):
        self.errors.append({
            "timestamp": datetime.now(timezone.utc).isoformat(), "stage": stage,
            "error_type": error_type, "message": message, "page_numbers": page_numbers,
            "raw_response": raw_response[:500] if raw_response else None,
        })
        self.metadata["errors_count"] = len(self.errors)

    def start_stage(self, stage: str):
        self.metadata["stage_timings"][stage] = {
            "started": datetime.now(timezone.utc).isoformat(), "completed": None,
            "api_calls": 0, "tokens_used": 0,
        }

    def complete_stage(self, stage: str, api_calls: int = 0, tokens: int = 0):
        if stage in self.metadata["stage_timings"]:
            t = self.metadata["stage_timings"][stage]
            t["completed"] = datetime.now(timezone.utc).isoformat()
            t["api_calls"] = api_calls
            t["tokens_used"] = tokens
            self.metadata["total_api_calls"] += api_calls
            self.metadata["total_tokens"] += tokens

    def finalize(self):
        self.metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.base_dir / "run_metadata.json", "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2, default=str, ensure_ascii=False)
        with open(self.base_dir / "error_log.json", "w", encoding="utf-8") as f:
            json.dump(self.errors, f, indent=2, default=str, ensure_ascii=False)
