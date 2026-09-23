"""
pipeline_config.py
===================
Single source of truth for the ETL pipeline's configuration interface and
its safe defaults. Centralized here (instead of hardcoded inside main.py)
so every run can be parametrized from the CLI and still have one place to
review/recalibrate the defaults — same principle applied to
RuleThresholdsConfig.js on the chaincode side.

Interface
---------
PipelineConfig is a plain dataclass — the "contract" every run must satisfy:
    max_lines          : int | None
    sample_per_drug     : int | None
    balanced            : bool
    max_units_per_lot   : int | None
    max_mem_mb          : int

Memory safety
-------------
node003 (the shared academic cluster this project runs on) has 16GB total
RAM shared with Fabric/Docker/Caliper containers running at the same time.
A runaway ETL process here has already caused the OOM killer to freeze SSH
on that machine (see September troubleshooting history). DEFAULT_MAX_MEM_MB
below is deliberately well under 16GB to leave headroom for those other
processes, and is enforced in-process via resource.setrlimit — it does not
depend on the operator remembering to wrap the command with `ulimit -v`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json
import platform

# Node003 total RAM is 16GB, shared with Docker/Fabric/Caliper. Keep the ETL
# step's own ceiling well under that so it never starves the rest of the
# stack. Override with --max-mem-mb if you know what else is running.
DEFAULT_MAX_MEM_MB = 8_000

DEFAULT_MAX_LINES         = 100_000
DEFAULT_SAMPLE_PER_DRUG   = 10
DEFAULT_BALANCED          = True
DEFAULT_MAX_UNITS_PER_LOT = 10


@dataclass(frozen=True)
class PipelineConfig:
    """Contract every ETL run must satisfy — see module docstring."""
    max_lines: Optional[int]
    sample_per_drug: Optional[int]
    balanced: bool
    max_units_per_lot: Optional[int]
    max_mem_mb: int

    def as_manifest_dict(self) -> dict:
        return {
            "max_lines": self.max_lines,
            "sample_per_drug": self.sample_per_drug,
            "balanced": self.balanced,
            "max_units_per_lot": self.max_units_per_lot,
            "max_mem_mb": self.max_mem_mb,
        }


def apply_memory_limit(max_mem_mb: int) -> None:
    """
    Caps this process's virtual memory in-process (RLIMIT_AS), so the ETL
    step is self-protecting on node003 even if the operator forgets an
    external `ulimit -v` wrapper. No-op on platforms without `resource`
    (e.g. native Windows) — falls back to relying on an external limit there.
    """
    try:
        import resource
        limit_bytes = max_mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ImportError, ValueError, OSError):
        print(f"[warn] Could not set in-process memory limit on this "
              f"platform ({platform.system()}) — rely on an external "
              f"`ulimit -v {max_mem_mb * 1024}` wrapper instead.")


def write_run_manifest(output_dir: Path, config: PipelineConfig, data_path: str,
                        result_stats: dict) -> Path:
    """
    Writes run_manifest.json next to events.json recording exactly which
    parameters produced this output. This is what was missing and caused
    events.json / experiment_results.xlsx from different runs to be
    silently mixed together (found 22/09) — every future run is now
    self-describing and traceable.
    """
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_data": str(data_path),
        "config": config.as_manifest_dict(),
        "result_stats": result_stats,
    }
    manifest_path = output_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path
