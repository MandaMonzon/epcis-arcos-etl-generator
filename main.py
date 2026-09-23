#!/usr/bin/env python3
"""
main.py — Entry point for epcis-arcos-etl-generator
=====================================================
Usage:
    python3 main.py --data data/arcos_raw.gz
    python3 main.py --data data/arcos_raw.gz --max-lines 100000 --sample-per-drug 10 \
                     --max-units-per-lot 10 --balanced --max-mem-mb 8000

All run parameters are CLI flags now (see pipeline_config.py for the
PipelineConfig contract and defaults) — nothing is hand-edited in this file
before a run anymore. Every run also writes output/run_manifest.json
recording exactly which parameters produced that output, so two different
runs (e.g. events.json vs experiment_results.xlsx) can never again be
silently compared as if they were the same experiment.

Memory: the ETL process caps its own virtual memory (RLIMIT_AS) at
--max-mem-mb (default 8000MB) because node003 has only 16GB total RAM
shared with Fabric/Docker/Caliper. See pipeline_config.py for details.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.extract import extract
from pipeline.transform  import build_scenario
from pipeline.load    import convert_scenario
from pipeline_config import (
    PipelineConfig,
    DEFAULT_MAX_LINES,
    DEFAULT_SAMPLE_PER_DRUG,
    DEFAULT_BALANCED,
    DEFAULT_MAX_UNITS_PER_LOT,
    DEFAULT_MAX_MEM_MB,
    apply_memory_limit,
    write_run_manifest,
)

OUTPUT_DIR = Path(__file__).parent / "output"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARCOS -> EPCIS 2.0 Pipeline")
    parser.add_argument("--data", "-d", required=True,
                         help="Path to ARCOS .gz file (e.g. data/arcos_raw.gz)")
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                         help=f"Rows to read from arcos_clean.csv. "
                              f"Use 0 for the full dataset (~26M rows, ~5 min). "
                              f"Default: {DEFAULT_MAX_LINES}")
    parser.add_argument("--sample-per-drug", type=int, default=DEFAULT_SAMPLE_PER_DRUG,
                         help=f"Transactions per distinct drug (reservoir sampling, "
                              f"reproducible). Use 0 to keep all. Default: {DEFAULT_SAMPLE_PER_DRUG}")
    balanced_group = parser.add_mutually_exclusive_group()
    balanced_group.add_argument("--balanced", dest="balanced", action="store_true",
                                 help="Drop drugs with fewer than --sample-per-drug rows "
                                      "(equal representation across drugs).")
    balanced_group.add_argument("--no-balanced", dest="balanced", action="store_false",
                                 help="Keep all drugs even with an incomplete sample.")
    parser.set_defaults(balanced=DEFAULT_BALANCED)
    parser.add_argument("--max-units-per-lot", type=int, default=DEFAULT_MAX_UNITS_PER_LOT,
                         help=f"Max EPCIS units (SGTINs) generated per lot transaction. "
                              f"Use 0 for full dosage_unit. Default: {DEFAULT_MAX_UNITS_PER_LOT}")
    parser.add_argument("--max-mem-mb", type=int, default=DEFAULT_MAX_MEM_MB,
                         help=f"Hard cap on this process's virtual memory, enforced "
                              f"in-process. node003 has 16GB total RAM shared with "
                              f"Fabric/Docker/Caliper — keep this well under that. "
                              f"Default: {DEFAULT_MAX_MEM_MB}")
    args = parser.parse_args()
    # 0 means "no limit" on the CLI, but None internally (clearer contract).
    args.max_lines = None if args.max_lines == 0 else args.max_lines
    args.sample_per_drug = None if args.sample_per_drug == 0 else args.sample_per_drug
    args.max_units_per_lot = None if args.max_units_per_lot == 0 else args.max_units_per_lot
    return args


def main():
    args = parse_args()
    config = PipelineConfig(
        max_lines=args.max_lines,
        sample_per_drug=args.sample_per_drug,
        balanced=args.balanced,
        max_units_per_lot=args.max_units_per_lot,
        max_mem_mb=args.max_mem_mb,
    )

    apply_memory_limit(config.max_mem_mb)
    print(f"[safety] Memory capped at {config.max_mem_mb:,}MB for this process "
          f"(node003 has 16GB total RAM shared with Fabric/Docker/Caliper).")

    data_path    = Path(args.data)
    clean_csv    = OUTPUT_DIR / "arcos_clean.csv"
    scenario_csv = OUTPUT_DIR / "scenario_template.csv"
    events_json  = OUTPUT_DIR / "events.json"

    OUTPUT_DIR.mkdir(exist_ok=True)

    # Step 1
    if clean_csv.exists():
        print(f"[1/3] arcos_clean.csv already exists - skipping extraction.")
    else:
        print(f"[1/3] Extracting {data_path.name} -> arcos_clean.csv ...")
        result = extract(str(data_path), str(clean_csv))
        print(f"      {result['converted']:,} rows extracted.")

    # Step 2
    mode         = f"(first {config.max_lines:,} rows)" if config.max_lines else "(all rows)"
    balance_note = ", balanced (equal transactions/drug)" if config.balanced else ""
    print(f"[2/3] Building scenario_template.csv {mode}, {config.sample_per_drug} tx/drug{balance_note} ...")
    scenario_result = build_scenario(
        str(clean_csv), str(scenario_csv),
        sample_per_drug=config.sample_per_drug,
        max_lines=config.max_lines,
        balanced=config.balanced,
    )
    print(f"      {scenario_result['drugs']:,} distinct drugs x {config.sample_per_drug} tx "
          f"= {scenario_result['transactions']:,} transactions.")

    # Step 3
    units_info = f" ({config.max_units_per_lot} unit/lot)" if config.max_units_per_lot else ""
    print(f"[3/3] Generating events.json{units_info} ...")
    convert_result = convert_scenario(
        str(scenario_csv), str(events_json),
        max_units_per_lot=config.max_units_per_lot,
    )
    print(f"      {convert_result['events']:,} EPCIS events generated "
          f"({convert_result['sgtins']:,} unique SGTINs).")
    for etype, count in sorted(convert_result.get('by_type', {}).items()):
        print(f"        {etype:<25} {count:>7,}")

    manifest_path = write_run_manifest(
        OUTPUT_DIR, config, str(data_path),
        result_stats={
            "scenario": scenario_result,
            "conversion": convert_result,
        },
    )

    print(f"\nDone! Files in {OUTPUT_DIR}/")
    print(f"  arcos_clean.csv        - real ARCOS data extracted")
    print(f"  scenario_template.csv  - EU->US scenario with anomalies")
    print(f"  events.json            - EPCIS 2.0 events ready for the chaincode")
    print(f"  run_manifest.json      - exact parameters that produced this run "
          f"({manifest_path.name})")


if __name__ == "__main__":
    main()
