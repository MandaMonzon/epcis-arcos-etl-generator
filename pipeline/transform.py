#!/usr/bin/env python3
"""
scenario_builder.py
====================
Task 2 — Builds the scenario CSV that feeds arcos_to_epcis.py.

Takes the clean ARCOS CSV (real data) and enriches each row with synthetic
values needed for the EU→US logistics diversion experiment.

═══════════════════════════════════════════════════════════════════════════════
MATHEMATICAL AND STATISTICAL BASIS FOR SYNTHETIC VALUES
═══════════════════════════════════════════════════════════════════════════════

1. EU MANUFACTURER GLN (eu_manufacturer_gln)
   GS1 assigns company prefixes to German manufacturers in the range 400–440.
   [Source: GS1 General Specifications 24.0, Section 1.4 — GS1 Prefix List]
   We assign one synthetic prefix per distinct NDC using index arithmetic:
       company_prefix = 4000001 + (drug_index mod 40000)
   This ensures each drug maps to a unique, realistic German GLN.
   Format: urn:epc:id:pgln:{company_prefix}.{location_ref}
   [Source: GS1 EPC Tag Data Standard 2.0, Section 6.3.3]

2. EU DEPARTURE DATE (eu_departure_date)
   Ocean freight EU Port → US East Coast transit time follows a normal
   distribution with μ=12 days, σ=2 days (clamped to [7, 21]).
   Seed is set to transaction_id hash for full reproducibility.

3. SSCC — Serial Shipping Container Code (sscc)
   GS1 SSCC is an 18-digit identifier:
       Extension digit (1) + Company prefix (variable) + Serial ref + Check digit
   Check digit computed by GS1 standard Mod-10 Luhn algorithm.
   [Source: GS1 General Specifications 24.0, Section 2.1.3]
   Serial reference derived from SHA-256(transaction_id) for reproducibility.

4. PURCHASE ORDER NUMBER (po_number)
   Encoded as GS1 GDTI (Global Document Type Identifier):
       urn:epc:id:gdti:{company_prefix}.{doc_type}.{serial}
   [Source: GS1 EPC Tag Data Standard 2.0, Section 6.3.6]
   Serial = first 10 chars of transaction_id (order form number is already
   a DEA-issued unique identifier, so it is appropriate as a document serial).

5. EXTRA DISTRIBUTOR IDs (extra_distributor_ids)
   Injected only for the "many_suppliers" anomaly scenario.
   The number of extra distributors is set to reach the 99th percentile
   of supply base complexity (≥9 suppliers), as defined empirically by:
   [Source: Skilton et al. (2024), Table 1 — mean=3.12, SD=1.44, 99th pct=9]
   We inject (9 - 1) = 8 extra distributors so that, combined with the real
   ARCOS reporter_id, the buyer reaches exactly the 99th percentile threshold.
   DEA number format: 2 letters + 7 digits [Source: DEA Registrant Manual, 2023]
   Generated deterministically: HMAC-SHA256("SKILTON-EXTRA", drug_code||i)

6. ANOMALY TYPE (anomaly_type) AND PROPORTION
   Anomaly proportion is set to 30% of transactions (3 clean : 1 anomalous).
   DEA estimates ~1% real diversion [Source: DEA National Drug Threat
   Assessment, 2020, p. 48], but a test dataset requires sufficient anomalous
   cases for rule validation — a 30% injection rate is a declared
   methodological choice following common fault-injection testing practice.
   [Source: Voas & Payne (1990), "Software fault injection testing",
    Proc. COMPASS'90, IEEE]
   Six anomaly types are cycled to cover all smart contract rule branches:
     - recalled:         tests BLOCKED_DISPOSITION rule (Class B, +90)
     - skip_commission:  tests SEQUENCE rule — no commissioning (Class A)
     - out_of_order:     tests TIMING rule — receiving before shipping (Class B, +60)
     - jurisdiction:     tests CROSS_JURISDICTION at dispensing step (Class B, +80)
     - many_suppliers:   tests SUPPLY_BASE_COMPLEXITY ≥ 9 (Class B, +85)
     - fast_transit:     tests TIMING — EU→US in 0 days (Class B, +60)

7. TRANSIT DAYS EU→US (transit_days_eu_us)
   Generic EU Port → US destination, ocean freight (city-agnostic).
   Transit time sampled from Normal(μ=12, σ=2), clamped to [7, 21] days.
   NOTE: In a real deployment, this value would be declared by the initial and
   final distributor via the EPCIS TransactionEvent. It is hardcoded here as a
   synthetic reference value only.
   For "fast_transit" anomaly: 0 days (physically impossible on any route).
   Seed per row = int(SHA-256(transaction_id), 16) mod 2^32 — reproducible.

═══════════════════════════════════════════════════════════════════════════════

Usage:
    python scenario_builder.py --input arcos_clean.csv --output scenario_template.csv
    python scenario_builder.py --input arcos_clean.csv --output scenario_template.csv --drugs 15
"""

import csv
import hmac
import hashlib
import random
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── GS1 German company prefix range ─────────────────────────────────────────
# GS1 Germany assigns prefixes 400–440 to German manufacturers.
# [GS1 General Specifications 24.0, Section 1.4]
DE_PREFIX_BASE = 4000001
DE_PREFIX_RANGE = 40000  # 400–440 = 40,000 distinct 7-digit prefixes

# ─── Waypoints (configurable — do NOT hardcode in functions) ─────────────────
# Each waypoint is a dict with: locode (UN/LOCODE), label, and transit_to_next.
# Functions receive this list as a parameter so it can be extended without
# changing function signatures. Currently 3 waypoints (A→B→C); add more as needed.
#
# Transit leg A→B (EU manufacturer → EU origin port): 1 day assumed (local logistics).
# Transit leg B→C (EU port → US destination): Normal(μ=12, σ=2), clamped [7,21].
# NOTE: In production, transit times would be declared by the distributor via
# EPCIS TransactionEvent. These constants are synthetic reference values only.
WAYPOINTS = [
    {
        "locode": "EU-MFG",    # EU manufacturer site (city-agnostic)
        "label": "EU Manufacturer",
        "transit_to_next_mean":  1,   # days to next waypoint (local logistics)
        "transit_to_next_std":   0.5,
        "transit_to_next_min":   0,
        "transit_to_next_max":   3,
    },
    {
        "locode": "EU-PORT",   # EU origin port (city-agnostic)
        "label": "EU Port",
        "transit_to_next_mean":  12,  # ocean freight, generic EU→US East Coast
        "transit_to_next_std":   2,
        "transit_to_next_min":   7,
        "transit_to_next_max":   21,
    },
    {
        "locode": "US-PORT",   # US destination port (city-agnostic)
        "label": "US Port",
        "transit_to_next_mean":  None,  # last waypoint — no next leg
        "transit_to_next_std":   None,
        "transit_to_next_min":   None,
        "transit_to_next_max":   None,
    },
]

# ─── Anomaly injection cycle ──────────────────────────────────────────────────
# 3 clean : 1 anomalous ~= 33% anomaly rate (declared methodological choice)
# [Voas & Payne, 1990 — fault injection testing practice]
ANOMALY_CYCLE = [
    "none",                   # clean — all waypoints in order, normal timing
    "none",                   # clean
    "none",                   # clean
    "recalled",               # blocked disposition            → Class B +90
    "skip_commission",        # no prior commissioning         → Class A block
    "out_of_order",           # receiving before shipping      → Class B +60
    "jurisdiction",           # EU→US at dispensing step       → Class B +80
    # "many_suppliers" removed — SupplyBaseComplexityRule is disabled in RuleEngine.js.
    # Re-add when the Skilton (2024) threshold is validated for the EU→US context.
    "none",
    "item_location_mismatch", # one box at last waypoint while lot is at first
    "impossible_transit",     # lot moves A→B in 1 day (EU Port→US impossible)
    "quantity_discrepancy",   # AggregationEvent ADD ≠ DELETE (units lost in transit)
    "shipment_divergence",    # seller ships but no receiving confirmation (ghost)
    "transit_diversion",      # shipped to buyer A, received at buyer B
]

# Extra distributors to inject so the buyer reaches exactly the Skilton 99th
# percentile (≥9 suppliers). With 1 real reporter_id already present, we add 8.
# [Skilton et al. 2024, Table 1 — 99th percentile = 9 suppliers]
SKILTON_99TH    = 9
EXTRA_DIST_COUNT = SKILTON_99TH - 1  # 8 extra + 1 real = 9 total


# ─── GS1 Mod-10 check digit (Luhn) ───────────────────────────────────────────

def gs1_check_digit(digits_str):
    # no longer used — kept for reference only
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits_str)))
    return str((10 - (total % 10)) % 10)


# ─── GLN builder ─────────────────────────────────────────────────────────────

def make_eu_gln(drug_index):
    """
    Generates a synthetic German manufacturer GLN.
    company_prefix = 4000001 + (drug_index mod 40000)
    [GS1 General Specifications 24.0, Section 1.4 — German prefix range 400-440]
    """
    prefix    = DE_PREFIX_BASE + (drug_index % DE_PREFIX_RANGE)
    location  = str(drug_index).zfill(5)
    return f"urn:epc:id:pgln:{prefix}.{location}"


# ─── SSCC builder ────────────────────────────────────────────────────────────

# GS1 demo/research company prefix (publicly reserved for examples and testing)
RESEARCH_PREFIX = "0614141"

def make_sscc(transaction_id, _=None):
    """Shipping container ID: one per transaction, deterministic hash."""
    serial = hashlib.sha256(transaction_id.encode()).hexdigest()[:16]
    return f"urn:epc:id:sscc:{RESEARCH_PREFIX}.{serial}"


def make_po_number(transaction_id, _=None):
    """Purchase order reference: reuses transaction_id as the unique PO serial."""
    return f"urn:epc:id:gdti:{RESEARCH_PREFIX}.001.{transaction_id}"


# ─── Waypoint date sampler ────────────────────────────────────────────────────

def sample_waypoint_dates(base_date_str, transaction_id, anomaly_type, waypoints):
    """
    Returns a list of ISO date strings, one per waypoint, in order.
    Each date = previous date + sampled transit days for that leg.

    Parameters
    ----------
    base_date_str : str
        ARCOS transaction_date (YYYY-MM-DD) — used as the date at waypoint[0].
    transaction_id : str
        Used as seed for reproducibility (SHA-256 based).
    anomaly_type : str
        Controls which leg gets an injected anomalous transit time.
    waypoints : list[dict]
        Ordered list of waypoint dicts (each must have transit_to_next_* keys).
        Passed as parameter — do NOT reference WAYPOINTS global inside here.

    Anomaly behaviour
    -----------------
    - "impossible_transit" : leg 0→1 (first leg) gets 1 day instead of normal.
      This places the lot at waypoint[1] only 1 day after production — impossible
      for ocean freight EU Port→US (minimum 7 days).
    - "item_location_mismatch" : dates are NORMAL for the lot; the mismatch is
      expressed via the 'item_mismatch_waypoint_index' column (see build_scenario).
      The lot is at waypoint[0] on day 0 while the anomalous box already reports
      at waypoint[-1] on the same day.
    - All other anomalies: normal date sampling.
    """
    seed = int(hashlib.sha256(transaction_id.encode()).hexdigest(), 16) % (2 ** 32)
    rng  = random.Random(seed)

    try:
        current = datetime.strptime(base_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        current = datetime.now(timezone.utc)

    dates = [current.strftime("%Y-%m-%d")]

    for leg_index, wp in enumerate(waypoints[:-1]):  # all legs except last (no next)
        mean  = wp["transit_to_next_mean"]
        std   = wp["transit_to_next_std"]
        lo    = wp["transit_to_next_min"]
        hi    = wp["transit_to_next_max"]

        if anomaly_type == "impossible_transit" and leg_index == 0:
            # Lot produced on day N, already at waypoint[1] on day N+1.
            # EU port leg normally takes 0–3 days so we inject 1 day on
            # the ocean leg instead: force leg 1 (port→destination) to 1 day.
            days = 1
        else:
            sampled = rng.gauss(mean, std)
            days = int(max(lo, min(hi, sampled)))

        current = current + timedelta(days=days)
        dates.append(current.strftime("%Y-%m-%d"))

    # For impossible_transit: also collapse the ocean leg (leg 1→2) to 1 day
    # The above loop only handles leg 0→1. Override leg 1→2 date if present.
    if anomaly_type == "impossible_transit" and len(waypoints) >= 3:
        wp1_date = datetime.strptime(dates[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dates[2] = (wp1_date + timedelta(days=1)).strftime("%Y-%m-%d")

    return dates


# ─── Extra distributor generator ─────────────────────────────────────────────

# NOTE: not called while many_suppliers is absent from ANOMALY_CYCLE.
# Keep for when SupplyBaseComplexityRule is re-enabled in the chaincode.
def make_extra_distributors(drug_code):
    """
    Generates EXTRA_DIST_COUNT synthetic DEA distributor IDs so that this buyer
    reaches the 99th percentile supply base complexity (≥9 suppliers).
    [Skilton et al. 2024, Table 1 — 99th percentile = 9 suppliers/pharmacy]
    DEA registration format: 2 letters + 7 alphanumeric chars [DEA Manual 2023]
    Generated via HMAC-SHA256("SKILTON-EXTRA", drug_code||i) — deterministic.
    """
    ids = []
    for i in range(EXTRA_DIST_COUNT):
        msg    = f"{drug_code}|{i}".encode()
        digest = hmac.new(b"SKILTON-EXTRA", msg, hashlib.sha256).hexdigest()[:7].upper()
        ids.append(f"urn:epc:id:pgln:us.dea.RM{digest}")
    return ";".join(ids)


# ─── Builder ─────────────────────────────────────────────────────────────────

def build_scenario(input_path, output_path, max_drugs=None, sample_per_drug=None,
                   max_lines=None, balanced=True):
    """
    Reads arcos_clean.csv in a single streaming pass and selects rows per drug.

    If sample_per_drug=N: uses reservoir sampling to keep at most N rows per drug
    without loading the full file into memory.
    [Algorithm R — Vitter, 1985]

    If balanced=True (default): only outputs drugs that have seen at least
    sample_per_drug rows, so every drug in the output has exactly the same
    number of transactions. Drugs with fewer rows are silently dropped.

    If max_lines=N: stops reading after N data rows (fast preview mode).
    """
    import hashlib as _hashlib
    import random  as _random

    input_file  = Path(input_path)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # reservoir[drug] = list of kept rows (at most sample_per_drug)
    # counts[drug]    = total rows seen for this drug (for reservoir probability)
    reservoir: dict[str, list] = {}
    counts:    dict[str, int]  = {}

    with open(input_file, newline="", encoding="utf-8") as f:
        lines_read = 0
        for row in csv.DictReader(f):
            if max_lines and lines_read >= max_lines:
                break
            lines_read += 1
            drug = (row.get("drug_code") or "").strip()
            if not drug:
                continue

            if drug not in reservoir:
                if max_drugs and len(reservoir) >= max_drugs:
                    continue
                reservoir[drug] = []
                counts[drug]    = 0

            counts[drug] += 1
            n = counts[drug]

            if sample_per_drug is None:
                if n == 1:
                    reservoir[drug].append(row)
            else:
                # Reservoir sampling (Algorithm R): replace with decreasing probability
                if len(reservoir[drug]) < sample_per_drug:
                    reservoir[drug].append(row)
                else:
                    seed = int(_hashlib.sha256(f"{drug}|{n}".encode()).hexdigest(), 16) % (2**32)
                    rng  = _random.Random(seed)
                    j    = rng.randint(0, n - 1)
                    if j < sample_per_drug:
                        reservoir[drug][j] = row

    # Proportionality filter: drop drugs that didn't accumulate a full sample,
    # so every drug in the output has exactly the same number of transactions.
    if balanced and sample_per_drug:
        seen_drugs = {d: rows for d, rows in reservoir.items()
                      if len(rows) >= sample_per_drug}
    else:
        seen_drugs = reservoir

    output_columns = [
        "transaction_id", "reporter_id", "buyer_id", "transaction_date",
        "drug_code", "dosage_unit",
        "eu_manufacturer_gln", "sscc", "po_number",
        "anomaly_type",
        # Waypoint dates: semicolon-separated, one date per waypoint in WAYPOINTS order.
        # e.g. "2006-01-01;2006-01-02;2006-01-15" for a 3-waypoint route.
        "waypoint_dates",
        # For item_location_mismatch: index of the waypoint where the isolated box
        # is found while the lot is still at waypoint[0]. Empty for other anomalies.
        "item_mismatch_waypoint_index",
    ]

    # Build enriched rows (used both for in-memory and file output)
    enriched_rows = []
    global_index  = 0
    for drug_code, rows in seen_drugs.items():
        for row in rows:
            raw_txn_id   = row["transaction_id"].strip()
            reporter_id  = row["reporter_id"].strip()
            buyer_id     = row["buyer_id"].strip()
            date_str     = row["transaction_date"].strip()
            # Fallback: ~50% of ARCOS rows have no transaction_id (blank DEA form field).
            # Use composite key to guarantee uniqueness and determinism.
            txn_id = raw_txn_id if raw_txn_id else f"{reporter_id}|{buyer_id}|{date_str}|{drug_code}"
            anomaly_type = ANOMALY_CYCLE[global_index % len(ANOMALY_CYCLE)]

            wp_dates = sample_waypoint_dates(date_str, txn_id, anomaly_type, WAYPOINTS)

            mismatch_index = ""
            if anomaly_type == "item_location_mismatch":
                mismatch_index = len(WAYPOINTS) - 1

            enriched_rows.append({
                "transaction_id":               txn_id,
                "reporter_id":                  reporter_id,
                "buyer_id":                     buyer_id,
                "transaction_date":             date_str,
                "drug_code":                    drug_code,
                "dosage_unit":                  row.get("dosage_unit", "1").strip() or "1",
                "eu_manufacturer_gln":          make_eu_gln(global_index),
                "sscc":                         make_sscc(txn_id, global_index),
                "po_number":                    make_po_number(txn_id, global_index),
                "anomaly_type":                 anomaly_type,
                "waypoint_dates":               ";".join(wp_dates),
                "item_mismatch_waypoint_index": mismatch_index,
            })
            global_index += 1

    if output_path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=output_columns)
            writer.writeheader()
            writer.writerows(enriched_rows)

    total_rows = sum(len(v) for v in seen_drugs.values())
    return {"drugs": len(seen_drugs), "transactions": total_rows,
            "output": str(output_file) if output_path else None,
            "rows": enriched_rows}


def main():
    BASE = Path(__file__).parent.parent
    parser = argparse.ArgumentParser(
        description="Build scenario_template.csv — enriches ARCOS transactions with "
                    "synthetic supply-chain values (GLNs, SSCCs, waypoint dates, anomaly labels)."
    )
    parser.add_argument(
        "--input", "-i",
        default=str(BASE / "data/normalized/arcos_clean.csv"),
        help="Clean ARCOS CSV produced by extract.py (default: data/normalized/arcos_clean.csv)",
    )
    parser.add_argument(
        "--output", "-o",
        default=str(BASE / "data/enriched/scenario_template.csv"),
        help="Destination path for the enriched scenario CSV (default: data/enriched/scenario_template.csv)",
    )
    args = parser.parse_args()

    result = build_scenario(args.input, args.output)

    clean_pct   = 100 * ANOMALY_CYCLE.count("none") // len(ANOMALY_CYCLE)
    anomaly_types = sorted(set(ANOMALY_CYCLE) - {"none"})
    waypoint_chain = " → ".join(w["label"] for w in WAYPOINTS)

    print(f"\nScenario built successfully")
    print(f"  Input              {args.input}")
    print(f"  Output             {result['output']}")
    print(f"  Drugs (NDCs)       {result['drugs']:,}")
    print(f"  Transactions       {result['transactions']:,}")
    print(f"  Supply chain       {len(WAYPOINTS)} hops: {waypoint_chain}")
    print(f"  Anomaly rate       ~{clean_pct}% clean / ~{100 - clean_pct}% anomalous")
    print(f"  Anomaly types      {', '.join(anomaly_types)}")
    print(f"  Skilton threshold  ≥{SKILTON_99TH} suppliers = 99th percentile [Skilton et al. 2024]")


if __name__ == "__main__":
    main()

