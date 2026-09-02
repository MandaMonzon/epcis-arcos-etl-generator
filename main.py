#!/usr/bin/env python3
"""
main.py — Entry point for epcis-arcos-etl-generator
=====================================================
Usage:
    python3 main.py --data data/arcos_raw.gz

Configuration:
    Edit the constants below before running.

    MAX_LINES         — Maximum rows to read from arcos_clean.csv.
                        Set to None to process the full dataset (26M rows, ~5 min).
                        Set to an integer (e.g. 100_000) for a fast run (~10 sec).

    SAMPLE_PER_DRUG   — Maximum transactions to keep per distinct drug (reservoir sampling).
                        Set to None to keep all transactions for each drug.

    BALANCED          — If True, drops drugs that have fewer than SAMPLE_PER_DRUG rows
                        in the window read. This ensures every drug in the output has
                        exactly SAMPLE_PER_DRUG transactions — no drug over-represented.
                        Set to False to keep all drugs even if their sample is incomplete.

    MAX_UNITS_PER_LOT — Maximum EPCIS units (SGTINs) generated per lot transaction.
                        Each unit produces ~6 events (one per waypoint step).
                        Set to 1 for compact experiments. Set to None for full dosage_unit.
"""

import argparse
import os
import sys
import importlib.util
from pathlib import Path

BASE = Path(__file__).parent

def _load_module(name, rel_path):
    path = BASE / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

extract   = _load_module("extract",   "pipeline/extract.py")
transform = _load_module("transform", "pipeline/transform.py")
load      = _load_module("load",      "pipeline/load.py")

NORM_DIR = BASE / "data/normalized"

# ── Configuration ─────────────────────────────────────────────────────────────
# ── Defaults (override via CLI args) ──────────────────────────────────────────
MAX_LINES         = 100_000
SAMPLE_PER_DRUG   = 10
BALANCED          = True
MAX_UNITS_PER_LOT = 10
# ──────────────────────────────────────────────────────────────────────────────

def _run_id(lines, drugs, max_drugs, units):
    l = f"L{lines // 1000}k" if lines    else "Lfull"
    d = f"D{drugs}"          if drugs    else "Dall"
    m = f"_M{max_drugs}"     if max_drugs else ""
    u = f"U{units}"          if units    else "Ufull"
    return f"{l}_{d}{m}_{u}"

def _update_current_symlink(target_dir: Path):
    current = BASE / "data/epcis/current"
    if current.is_symlink():
        current.unlink()
    current.symlink_to(target_dir.name)

def main():
    parser = argparse.ArgumentParser(description="ARCOS -> EPCIS 2.0 Pipeline")
    parser.add_argument("--data",  "-d", default=str(BASE.parent / "raw_data_original.gz"),
                        help="Path to ARCOS .gz file")
    parser.add_argument("--lines", "-l", type=int, default=MAX_LINES,
                        help=f"Max rows to read from ARCOS (default: {MAX_LINES})")
    parser.add_argument("--drugs",     "-n", type=int, default=SAMPLE_PER_DRUG,
                        help=f"Transactions per drug (default: {SAMPLE_PER_DRUG})")
    parser.add_argument("--max-drugs", "-m", type=int, default=None,
                        help="Max distinct drugs to keep (default: all found)")
    parser.add_argument("--units",      "-u", type=int, default=MAX_UNITS_PER_LOT,
                        help=f"Max EPCIS units per lot (default: {MAX_UNITS_PER_LOT})")
    parser.add_argument("--max-events", "-e", type=int, default=None,
                        help="Cap total EPCIS events in output (default: all)")
    args = parser.parse_args()

    lines      = args.lines
    drugs      = args.drugs
    max_drugs  = args.max_drugs
    units      = args.units
    max_events = args.max_events

    run_id = _run_id(lines, drugs, max_drugs, units)
    data_path    = Path(args.data)
    clean_csv    = NORM_DIR / "arcos_clean.csv"
    enriched_dir = BASE / "data/enriched" / run_id
    epcis_dir    = BASE / "data/epcis"    / run_id
    scenario_csv = enriched_dir / "scenario_template.csv"
    events_json  = epcis_dir    / "events.json"

    NORM_DIR.mkdir(parents=True, exist_ok=True)
    enriched_dir.mkdir(parents=True, exist_ok=True)
    epcis_dir.mkdir(parents=True, exist_ok=True)

    print(f"Run: {run_id}")

    # Step 1 — Extract (shared, never regenerated if exists)
    if clean_csv.exists():
        print(f"[1/3] arcos_clean.csv already exists — skipping extraction.")
    else:
        print(f"[1/3] Extracting {data_path.name} -> data/normalized/arcos_clean.csv ...")
        result = extract.extract(str(data_path), str(clean_csv))
        print(f"      {result['converted']:,} rows extracted.")

    # Step 2 — Transform
    mode         = f"(first {lines:,} rows)" if lines else "(all rows)"
    balance_note = ", balanced" if BALANCED else ""
    print(f"[2/3] Building scenario_template.csv {mode}, {drugs} tx/drug{balance_note} ...")
    result = transform.build_scenario(
        str(clean_csv), str(scenario_csv),
        sample_per_drug=drugs,
        max_lines=lines,
        max_drugs=max_drugs,
        balanced=BALANCED,
    )
    print(f"      {result['drugs']:,} drugs × {drugs} tx = {result['transactions']:,} transactions.")

    # Step 3 — Load
    units_info = f" ({units} units/lot)" if units else ""
    print(f"[3/3] Generating events.json{units_info} ...")
    result = load.convert_scenario(
        str(scenario_csv), str(events_json),
        max_units_per_lot=units,
        max_events=max_events,
    )
    print(f"      {result['events']:,} EPCIS events ({result['sgtins']:,} SGTINs).")
    for etype, count in sorted(result.get('by_type', {}).items()):
        print(f"        {etype:<25} {count:>7,}")

    # Generate sample file (300 of each type) for quick benchmarks
    _generate_sample(events_json, epcis_dir / "events_sample.json")

    # Update current/ symlink so Caliper always uses the latest run
    _update_current_symlink(epcis_dir)

    print(f"\nDone! Outputs saved to: data/epcis/{run_id}/")
    print(f"  data/normalized/arcos_clean.csv")
    print(f"  data/enriched/{run_id}/scenario_template.csv")
    print(f"  data/epcis/{run_id}/events.json")
    print(f"  data/epcis/{run_id}/events_sample.json")
    print(f"  data/epcis/current -> {run_id}  (symlink updated)")
    print(f"\nTo benchmark this run:")
    print(f"  docker run ... \\")
    print(f"    -v $(pwd)/data/epcis/{run_id}:/epcis-output \\")
    print(f"    ...")


def _generate_sample(source: Path, dest: Path, per_type: int = 300):
    import json
    LIMITS = {"ObjectEvent": per_type, "AggregationEvent": per_type, "TransactionEvent": per_type}
    counts = {k: 0 for k in LIMITS}
    sample = []
    MARKER = '"eventList"'
    done   = False

    with open(source, "r", encoding="utf-8") as f:
        carry = ""
        depth = 0; capturing = False; obj_buf = ""; in_list = False
        while not done:
            chunk = f.read(131072)
            if not chunk:
                break
            data = carry + chunk; carry = ""; i = 0; n = len(data)
            while i < n:
                if not in_list:
                    pos = data.find(MARKER, i)
                    if pos == -1: carry = data[max(0, n - len(MARKER)):]; break
                    in_list = True; i = pos + len(MARKER); continue
                ch = data[i]
                if not capturing:
                    if ch == '{': capturing = True; depth = 1; obj_buf = '{'
                    i += 1; continue
                obj_buf += ch
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        capturing = False
                        try:
                            obj = json.loads(obj_buf)
                            t = obj.get("type")
                            if t in LIMITS and counts[t] < LIMITS[t]:
                                sample.append(obj); counts[t] += 1
                                if all(counts[k] >= LIMITS[k] for k in LIMITS):
                                    done = True; break
                        except Exception: pass
                        obj_buf = ""
                i += 1

    wrapper = {"@context": "https://ref.gs1.org/standards/epcis/epcis-context.jsonld",
               "type": "EPCISDocument", "schemaVersion": "2.0",
               "epcisBody": {"eventList": sample}}
    with open(dest, "w") as f:
        json.dump(wrapper, f, indent=2)
    size_mb = dest.stat().st_size / 1e6
    print(f"      Sample: {sum(counts.values())} events → {dest.name} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
