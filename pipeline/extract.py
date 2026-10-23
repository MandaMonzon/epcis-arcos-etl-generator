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
import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from datetime import datetime

from pipeline.sampling import representativeness

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


def extract(input_path, output_path, limit=None, chunk_size=50_000, sample_size=None, seed=0):
    """Converts the raw file. With sample_size, keeps a uniform random sample of the valid rows
    (Algorithm R, Vitter 1985) over the WHOLE file, instead of the first rows. limit keeps the
    legacy head-of-file behaviour and cannot be combined with sample_size."""
    if sample_size and limit:
        raise ValueError("limit (head of file) and sample_size (random sample) are mutually exclusive")
    input_file  = Path(input_path)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    open_fn = (lambda p: gzip.open(p, "rt", encoding="utf-8", errors="replace")
               if input_file.suffix == ".gz"
               else open(input_file, "r", encoding="utf-8", errors="replace"))

    # Detect delimiter from first 4KB
    with open_fn(input_file) as f:
        sample = f.read(4096)
    delim = "\t" if sample.count("\t") > sample.count(",") else ","

    converted, skipped, rows_read = 0, 0, 0
    skip_reasons = {}
    transaction_occurrences = {}
    rng = random.Random(seed)
    reservoir = []
    population_valid = 0
    population_year, population_drug = Counter(), Counter()

    with open_fn(input_file) as f_in, \
         open(output_file, "w", newline="", encoding="utf-8") as f_out:

        reader = csv.DictReader(f_in, delimiter=delim)
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]

        # Resolve actual column names from the file
        col = {field: find_column(headers, candidates)
               for field, candidates in COLUMN_MAP.items()}

        missing = [f for f, c in col.items() if c is None and f != "quantity_grams"]
        if missing:
            print(f"WARNING: columns not found in source: {missing}", file=sys.stderr)

        writer = csv.DictWriter(f_out, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()

        for row in reader:
            rows_read += 1
            # Normalize header keys to lowercase — skip None keys (extra columns beyond header)
            row = {k.strip().lower(): v for k, v in row.items() if k is not None}

            try:
                order_form = row.get("order_form_no", "").strip()
                txn_id_raw = row.get("transaction_id", "").strip()
                transaction_date = normalize_date(row.get(col["transaction_date"], ""))
                dosage_unit = normalize_positive_integer(row.get(col["dosage_unit"], ""))

                record = {
                    "transaction_id":   order_form or txn_id_raw,
                    "reporter_id":      row.get(col["reporter_id"], "").strip().upper(),
                    "buyer_id":         row.get(col["buyer_id"], "").strip().upper(),
                    "transaction_date": transaction_date,
                    "drug_code":        normalize_ndc(row.get(col["drug_code"], "")),
                    "quantity_grams":   row.get(col["quantity_grams"], "0").strip() or "0",
                    "dosage_unit":      dosage_unit,
                }
                missing = [field for field in ("reporter_id", "buyer_id", "drug_code",
                                                "transaction_date", "dosage_unit") if not record[field]]
                if record["drug_code"] and not re.fullmatch(r"\d{11}", record["drug_code"]):
                    missing.append("drug_code")
                if missing:
                    skipped += 1
                    skip_reasons[missing[0]] = skip_reasons.get(missing[0], 0) + 1
                    continue

                record["transaction_id"] = "|".join((
                    record["transaction_id"], record["drug_code"],
                    record["reporter_id"], record["buyer_id"],
                    record["transaction_date"],
                ))

                if sample_size:
                    population_valid += 1
                    population_year[record["transaction_date"][:4]] += 1
                    population_drug[record["drug_code"]] += 1
                    if len(reservoir) < sample_size:
                        reservoir.append((population_valid, record))
                    else:
                        j = rng.randrange(population_valid)
                        if j < sample_size:
                            reservoir[j] = (population_valid, record)
                    continue

                number_occurrences(record, transaction_occurrences)
                writer.writerow(record)
                converted += 1

            except Exception:
                skipped += 1
                skip_reasons["unparseable_row"] = skip_reasons.get("unparseable_row", 0) + 1

            if limit and converted >= limit:
                break

        if sample_size:
            for _, record in sorted(reservoir, key=lambda item: item[0]):
                number_occurrences(record, transaction_occurrences)
                writer.writerow(record)
                converted += 1

    result = {"rows_read": rows_read, "converted": converted, "skipped": skipped, "skip_reasons": skip_reasons,
              "not_in_random_sample": (population_valid - converted) if sample_size else 0,
              "output": str(output_file)}
    if sample_size:
        kept = [record for _, record in reservoir]
        result.update({
            "population_valid": population_valid,
            "sample_seed": seed,
            "representativeness": {
                "year": representativeness(population_year, Counter(r["transaction_date"][:4] for r in kept)),
                "drug": representativeness(population_drug, Counter(r["drug_code"] for r in kept)),
            },
        })
        report = {k: result[k] for k in ("rows_read", "population_valid", "skipped", "skip_reasons", "not_in_random_sample",
                                          "sample_seed", "representativeness")}
        report["sample_size"] = converted
        Path(str(output_file) + ".sampling.json").write_text(json.dumps(report, indent=2))
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
