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
import re
import sys
from pathlib import Path
from datetime import datetime

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


def extract(input_path, output_path, limit=None, chunk_size=50_000):
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

    converted, skipped = 0, 0
    skip_reasons = {}
    transaction_occurrences = {}

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
                                                "transaction_id", "transaction_date",
                                                "dosage_unit") if not record[field]]
                if record["drug_code"] and not re.fullmatch(r"\d{11}", record["drug_code"]):
                    missing.append("drug_code")
                if missing:
                    skipped += 1
                    for field in missing:
                        skip_reasons[field] = skip_reasons.get(field, 0) + 1
                    continue

                record["transaction_id"] = "|".join((
                    record["transaction_id"], record["drug_code"],
                    record["reporter_id"], record["buyer_id"],
                    record["transaction_date"],
                ))
                occurrence_key = (record["transaction_id"], record["drug_code"],
                                  record["reporter_id"], record["buyer_id"],
                                  record["transaction_date"])
                occurrence = transaction_occurrences.get(occurrence_key, 0) + 1
                transaction_occurrences[occurrence_key] = occurrence
                if occurrence > 1:
                    record["transaction_id"] += f"|line-{occurrence}"

                writer.writerow(record)
                converted += 1

            except Exception:
                skipped += 1

            if limit and converted >= limit:
                break

    return {"converted": converted, "skipped": skipped, "skip_reasons": skip_reasons,
            "output": str(output_file)}


def main():
    BASE = Path(__file__).parent.parent
    parser = argparse.ArgumentParser(description="Task 1 — Extract ARCOS .gz to clean CSV")
    parser.add_argument("--input",  "-i", default=str(BASE.parent / "raw_data_original.gz"), help=".gz or .csv ARCOS file")
    parser.add_argument("--output", "-o", default=str(BASE / "data/normalized/arcos_clean.csv"), help="Output CSV path")
    parser.add_argument("--limit",  "-n", type=int,   default=None, help="Max rows to extract")
    args = parser.parse_args()

    result = extract(args.input, args.output, args.limit)
    print(f"Extracted: {result['converted']:,} rows  |  Skipped: {result['skipped']:,}")
    print(f"Output:    {result['output']}")


if __name__ == "__main__":
    main()
