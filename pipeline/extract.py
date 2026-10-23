#!/usr/bin/env python3
"""
Task 1 — ARCOS Extractor
========================
Reads the raw ARCOS .gz file and produces a clean CSV with only the
columns needed for EPCIS event generation.

Output columns (Task 1 spec):
    transaction_id   → unique identifier for the transaction (order_form_no)
    reporter_id      → DEA number of the seller/distributor
    buyer_id         → DEA number of the buyer/pharmacy
    transaction_date → date of the transaction (ISO 8601)
    drug_code        → NDC number of the drug (normalized, no dashes)
    quantity_grams   → total weight in grams (calc_base_wt_gm)

Usage:
    python arcos_extract.py --input path/to/arcos.gz --output arcos_clean.csv
    python arcos_extract.py --input arcos.gz --output arcos_clean.csv --limit 5000
"""

import csv
import gzip
import heapq
import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from datetime import datetime

from pipeline.sampling import representativeness, weighted_total_variation

# ARCOS column names in the real file (lowercase)
COLUMN_MAP = {
    "transaction_id":   ["order_form_no", "transaction_id"],
    "reporter_id":      ["reporter_dea_no"],
    "buyer_id":         ["buyer_dea_no"],
    "transaction_date": ["transaction_date"],
    "drug_code":        ["ndc_no", "drug_code"],
    "quantity_grams":   ["calc_base_wt_in_gm", "calc_base_wt_gm"],
    "dosage_unit":      ["quantity"],
}

OUTPUT_COLUMNS = ["transaction_id", "reporter_id", "buyer_id",
                  "transaction_date", "drug_code", "quantity_grams", "dosage_unit"]


def find_column(headers, candidates):
    """Returns the first matching column name from the candidates list."""
    for c in candidates:
        if c in headers:
            return c
    return None


def normalize_date(raw):
    """Tries common ARCOS date formats and returns YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def normalize_ndc(raw):
    """Strips dashes and spaces, zero-pads to 11 digits."""
    ndc = raw.replace("-", "").replace(" ", "").strip()
    return ndc.zfill(11) if ndc.isdigit() else ndc


def normalize_positive_integer(raw):
    """Returns a positive integer string, or an empty value if invalid."""
    value = raw.strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not number.is_integer() or number <= 0:
        return ""
    return str(int(number))


def number_occurrences(record, occurrences):
    key = (record["transaction_id"], record["drug_code"], record["reporter_id"],
           record["buyer_id"], record["transaction_date"])
    occurrences[key] = occurrences.get(key, 0) + 1
    if occurrences[key] > 1:
        record["transaction_id"] += f"|line-{occurrences[key]}"


def open_text(input_file):
    if input_file.suffix == ".gz":
        return gzip.open(input_file, "rt", encoding="utf-8", errors="replace")
    return open(input_file, "r", encoding="utf-8", errors="replace")


def iter_records(input_file, stats):
    """Yields every valid normalized record; rows_read, skipped and skip_reasons accumulate in stats."""
    with open_text(input_file) as f:
        sample = f.read(4096)
    delim = "\t" if sample.count("\t") > sample.count(",") else ","

    with open_text(input_file) as f_in:
        reader = csv.DictReader(f_in, delimiter=delim)
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]
        col = {field: find_column(headers, candidates) for field, candidates in COLUMN_MAP.items()}
        missing = [f for f, c in col.items() if c is None and f != "quantity_grams"]
        if missing:
            print(f"WARNING: columns not found in source: {missing}", file=sys.stderr)

        for row in reader:
            stats["rows_read"] += 1
            row = {k.strip().lower(): v for k, v in row.items() if k is not None}
            try:
                order_form = row.get("order_form_no", "").strip()
                txn_id_raw = row.get("transaction_id", "").strip()
                record = {
                    "transaction_id":   order_form or txn_id_raw,
                    "reporter_id":      row.get(col["reporter_id"], "").strip().upper(),
                    "buyer_id":         row.get(col["buyer_id"], "").strip().upper(),
                    "transaction_date": normalize_date(row.get(col["transaction_date"], "")),
                    "drug_code":        normalize_ndc(row.get(col["drug_code"], "")),
                    "quantity_grams":   row.get(col["quantity_grams"], "0").strip() or "0",
                    "dosage_unit":      normalize_positive_integer(row.get(col["dosage_unit"], "")),
                }
                missing = [field for field in ("reporter_id", "buyer_id", "drug_code",
                                                "transaction_date", "dosage_unit") if not record[field]]
                if record["drug_code"] and not re.fullmatch(r"\d{11}", record["drug_code"]):
                    missing.append("drug_code")
                if missing:
                    stats["skipped"] += 1
                    stats["skip_reasons"][missing[0]] = stats["skip_reasons"].get(missing[0], 0) + 1
                    continue
                record["transaction_id"] = "|".join((
                    record["transaction_id"], record["drug_code"],
                    record["reporter_id"], record["buyer_id"], record["transaction_date"],
                ))
            except Exception:
                stats["skipped"] += 1
                stats["skip_reasons"]["unparseable_row"] = stats["skip_reasons"].get("unparseable_row", 0) + 1
                continue
            yield record


def new_stats():
    return {"rows_read": 0, "skipped": 0, "skip_reasons": {}}


def simple_sample(input_file, sample_size, rng, stats):
    """Algorithm R over all valid rows (Vitter 1985)."""
    reservoir, population_valid = [], 0
    population_year, population_drug = Counter(), Counter()
    for record in iter_records(input_file, stats):
        population_valid += 1
        population_year[record["transaction_date"][:4]] += 1
        population_drug[record["drug_code"]] += 1
        if len(reservoir) < sample_size:
            reservoir.append((population_valid, record))
        else:
            j = rng.randrange(population_valid)
            if j < sample_size:
                reservoir[j] = (population_valid, record)
    kept = sorted(reservoir, key=lambda item: item[0])
    records = [record for _, record in kept]
    report = {
        "year": representativeness(population_year, Counter(r["transaction_date"][:4] for r in records)),
        "drug": representativeness(population_drug, Counter(r["drug_code"] for r in records)),
    }
    return records, population_valid, report


def stratified_sample(input_file, sample_size, min_per_stratum, rng, stats):
    """Single-pass stratified sample by drug: proportional allocation with a floor.

    Every row gets a seeded uniform key. Rows with key below a shared threshold tau are kept; when
    more than 2 * sample_size rows are held, tau drops to the (sample_size + 1)-th smallest key and the
    rest are discarded (nested keys keep the sample uniform). Per drug, the min_per_stratum smallest
    keys are also kept, so rare drugs survive. Inside every drug the sample is a uniform random subset;
    across drugs it is proportional to size, except the floor. Each row records its inclusion weight
    N_h / n_h from counts taken in the same pass. Memory: at most 2 * sample_size + floor * drugs rows.
    """
    population_year, population_drug = Counter(), Counter()
    population_valid = 0
    tau = 1.0
    held = {}
    floor_sets = {}
    cap = 2 * sample_size
    peak = 0

    for index, record in enumerate(iter_records(input_file, stats)):
        population_valid += 1
        drug = record["drug_code"]
        population_year[record["transaction_date"][:4]] += 1
        population_drug[drug] += 1
        key = rng.random()

        heap = floor_sets.setdefault(drug, [])
        in_floor = False
        if len(heap) < min_per_stratum:
            heapq.heappush(heap, (-key, index))
            in_floor = True
        elif key < -heap[0][0]:
            heapq.heapreplace(heap, (-key, index))
            in_floor = True

        if key < tau or in_floor:
            held[index] = (key, record)
        if len(held) > cap:
            keys = sorted(k for k, _ in held.values())
            tau = keys[sample_size]
            protected = {i for heap in floor_sets.values() for _, i in heap}
            held = {i: kr for i, kr in held.items() if kr[0] < tau or i in protected}
        peak = max(peak, len(held))

    protected = {i for heap in floor_sets.values() for _, i in heap}
    if len(held) > sample_size:
        keys = sorted(k for k, _ in held.values())
        below = [k for k in keys if k < tau]
        if len(below) > sample_size:
            tau = below[sample_size]
    chosen = {i: kr for i, kr in held.items() if kr[0] < tau or i in protected}

    per_drug = Counter(record["drug_code"] for _, record in chosen.values())
    records = []
    for index in sorted(chosen):
        record = chosen[index][1]
        record["sampling_weight"] = repr(population_drug[record["drug_code"]] / per_drug[record["drug_code"]])
        records.append(record)

    weighted_year = Counter()
    for record in records:
        weighted_year[record["transaction_date"][:4]] += float(record["sampling_weight"])
    report = {
        "year": {"total_variation": weighted_total_variation(population_year, weighted_year), "weighted": True},
        "drug": {"strata": len(population_drug),
                 "taken_whole": sum(1 for d, n in population_drug.items() if per_drug[d] == n),
                 "min_per_stratum": min_per_stratum, "peak_kept": peak},
    }
    return records, population_valid, report


def extract(input_path, output_path, limit=None, chunk_size=50_000, sample_size=None, seed=0,
            design="stratified", min_per_stratum=10):
    """Converts the raw file.

    With sample_size, draws a seeded random sample over the WHOLE file instead of the first rows:
    design="stratified" (default) stratifies by drug with proportional allocation and a floor
    (Cochran 1977) and records the inclusion weight; design="simple" is a uniform sample of all
    valid rows. limit keeps the legacy head-of-file behaviour and cannot be combined with sampling.
    """
    if sample_size and limit:
        raise ValueError("limit (head of file) and sample_size (random sample) are mutually exclusive")
    input_file = Path(input_path)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    stats = new_stats()
    occurrences = {}
    population_valid, report = 0, None
    columns = list(OUTPUT_COLUMNS)

    if sample_size:
        rng = random.Random(seed)
        if design == "stratified":
            records, population_valid, report = stratified_sample(input_file, sample_size, min_per_stratum, rng, stats)
            columns.append("sampling_weight")
        elif design == "simple":
            records, population_valid, report = simple_sample(input_file, sample_size, rng, stats)
        else:
            raise ValueError(f"unknown sampling design: {design}")
    else:
        records = iter_records(input_file, stats)

    converted = 0
    with open(output_file, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=columns)
        writer.writeheader()
        for record in records:
            number_occurrences(record, occurrences)
            writer.writerow(record)
            converted += 1
            if limit and converted >= limit:
                break

    result = {"rows_read": stats["rows_read"], "converted": converted, "skipped": stats["skipped"],
              "skip_reasons": stats["skip_reasons"],
              "not_in_random_sample": (population_valid - converted) if sample_size else 0,
              "output": str(output_file)}
    if sample_size:
        result.update({"population_valid": population_valid, "sample_seed": seed,
                       "sampling_design": design, "representativeness": report})
        keys = ("rows_read", "population_valid", "skipped", "skip_reasons", "not_in_random_sample",
                "sample_seed", "sampling_design", "representativeness")
        data = {k: result[k] for k in keys}
        data["sample_size"] = converted
        Path(str(output_file) + ".sampling.json").write_text(json.dumps(data, indent=2))
    return result


def main():
    BASE = Path(__file__).parent.parent
    parser = argparse.ArgumentParser(description="Task 1 — Extract ARCOS .gz to clean CSV")
    parser.add_argument("--input",  "-i", default=str(BASE.parent / "raw_data_original.gz"), help=".gz or .csv ARCOS file")
    parser.add_argument("--output", "-o", default=str(BASE / "data/normalized/arcos_clean.csv"), help="Output CSV path")
    parser.add_argument("--limit",  "-n", type=int,   default=None, help="Max rows to extract (head of file, NOT random)")
    parser.add_argument("--sample-rows", type=int, default=None, help="Uniform random sample of this many valid rows")
    parser.add_argument("--seed", type=int, default=0, help="Seed of the random sample")
    args = parser.parse_args()

    result = extract(args.input, args.output, args.limit, sample_size=args.sample_rows, seed=args.seed)
    print(f"Extracted: {result['converted']:,} rows  |  Skipped: {result['skipped']:,}")
    print(f"Output:    {result['output']}")


if __name__ == "__main__":
    main()
