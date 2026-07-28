#!/usr/bin/env python3
"""
Task 2 — scenario_template.csv → EPCIS 2.0 Converter
======================================================
Reads scenario_template.csv (output of scenario_builder.py) and generates a
GS1 EPCIS 2.0 EPCISDocument with one event sequence per drug lot.

Event sequence per waypoint (normal flow, 3 waypoints A→B→C):
    waypoint[0] (EU Manufacturer): commissioning + packing
    waypoint[1] (EU Port):         shipping
    waypoint[2] (US Destination):  receiving

Each anomaly_type modifies this sequence:
    none                  → normal sequence, correct dates
    recalled              → commissioning with disposition=recalled (Class B +90)
    skip_commission       → commissioning omitted (Class A block)
    out_of_order          → receiving before shipping (Class B +60)
    jurisdiction          → EU→US at bizStep=dispensing, wrong for this step (Class B +80)
    many_suppliers        → normal sequence + extra_distributor_ids in sourceList
    item_location_mismatch → normal lot + one box (SGTIN[0]) already at last waypoint
                             on the same date the lot was commissioned (production day)
    impossible_transit    → waypoint_dates already show physically impossible timing
                             (e.g. EU Port→US in 1 day); events use those dates as-is

SGTIN generation:
    serial = Truncate_12(HMAC-SHA256("GS1-EPCIS-ARCOS-V1", transaction_id || unit_index))
    SGTIN  = urn:epc:id:sgtin:{company_prefix}.{item_ref}.{serial}
    [Deterministic: same input always produces same identifier]

References:
    GS1 EPCIS 2.0:  https://ref.gs1.org/standards/epcis
    Skilton (2024):  https://doi.org/10.1002/joom.1335

Usage:
    python arcos_to_epcis.py --input scenario_template.csv --output events.json
    python arcos_to_epcis.py --input scenario_template.csv --output events.json --limit 5
"""

import csv
import json
import hmac
import hashlib
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Maximum units per lot
LOT_SIZE_CAP = 100

# US company prefix (DEA/ARCOS side)
COMPANY_PREFIX_US = "0614141"

# Synthetic GLN for the EU origin port (waypoint[1]) — city-agnostic.
# GS1 DE research prefix 4000000, reserved location 99999.
GLN_EU_PORT  = "urn:epc:id:sgln:4000000.99999.0"
PGLN_EU_PORT = "urn:epc:id:pgln:4000000.99999"


# ─── Item identifier ──────────────────────────────────────────────────────────
# Identifier scheme choices:
#   "sgtin" — GS1 SGTIN, preferred for EPCIS 2.0 unit-level pharmaceutical traceability.
#             Format: urn:epc:id:sgtin:{company_prefix}.{item_ref}.{serial}
#   "urn"   — DEA Registration Number as plain URN (simpler, non-GS1).
#             Format: urn:dea:rn:{dea_no}:{serial}
#
# Default used when no scheme is passed at runtime. Override via --identifier-scheme CLI.
IDENTIFIER_SCHEME = "sgtin"

# HMAC key — used by both schemes to make serial numbers deterministic
_HMAC_KEY = b"GS1-EPCIS-ARCOS-V1"


def make_item_identifier(transaction_id, unit_index, drug_code="",
                         dea_no="", company_prefix=COMPANY_PREFIX_US,
                         item_ref="107346", scheme=None):
    """
    Returns the EPC URI for one serialized item.

    Parameters
    ----------
    transaction_id — ARCOS transaction ID (makes the serial deterministic)
    unit_index     — position of this unit within the lot (0-based)
    drug_code      — NDC or drug code (avoids collisions across drugs)
    dea_no         — DEA registration number (used by 'urn' scheme)
    company_prefix — GS1 company prefix (used by 'sgtin' scheme)
    item_ref       — GS1 item reference  (used by 'sgtin' scheme)
    scheme         — 'sgtin' | 'urn' | None (falls back to IDENTIFIER_SCHEME)
    """
    effective = scheme or IDENTIFIER_SCHEME

    msg    = f"{transaction_id}|{drug_code}|{unit_index}".encode()
    digest = hmac.new(_HMAC_KEY, msg, hashlib.sha256).hexdigest()
    serial = str(int(digest[:16], 16))[:12]

    if effective == "sgtin":
        return f"urn:epc:id:sgtin:{company_prefix}.{item_ref}.{serial}"

    if effective == "urn":
        return f"urn:dea:rn:{dea_no}:{serial}"

    raise ValueError(f"Unknown identifier scheme: {effective!r}. Expected 'sgtin' or 'urn'.")


# Alias kept for backward compatibility — passes scheme through
def generate_sgtin(transaction_id, unit_index, drug_code="",
                   company_prefix=COMPANY_PREFIX_US, item_ref="107346",
                   scheme=None):
    return make_item_identifier(transaction_id, unit_index, drug_code,
                                company_prefix=company_prefix, item_ref=item_ref,
                                scheme=scheme)


# ─── Location helpers ─────────────────────────────────────────────────────────

def dea_to_sgln(dea_no):
    return f"urn:epc:id:sgln:us.dea.{dea_no}.0.0"

def dea_to_pgln(dea_no):
    return f"urn:epc:id:pgln:us.dea.{dea_no}"

def gln_to_pgln(sgln):
    """Converts urn:epc:id:sgln:X.Y.Z → urn:epc:id:pgln:X.Y"""
    parts = sgln.replace("urn:epc:id:sgln:", "").split(".")
    return f"urn:epc:id:pgln:{parts[0]}.{parts[1]}"

def parse_date(date_str):
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


# ─── Event builder ────────────────────────────────────────────────────────────

def make_event(sgtin, biz_step, disposition, event_time,
               read_point_sgln, biz_location_sgln,
               source_sgln, source_pgln,
               dest_sgln, dest_pgln,
               transaction_id, extra_sources=None):
    """
    Builds one GS1 EPCIS 2.0 ObjectEvent dict.
    extra_sources: list of {"type":..., "source":...} to append to sourceList.
    """
    ts        = event_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    event_key = f"{sgtin}|{biz_step}|{ts}".encode()
    event_id  = "urn:uuid:" + hashlib.sha256(event_key).hexdigest()[:32]

    sources = [
        {"type": "location",         "source": source_sgln},
        {"type": "possessing_party", "source": source_pgln},
        {"type": "owning_party",     "source": source_pgln},
    ]
    if extra_sources:
        sources.extend(extra_sources)

    return {
        "eventID":             event_id,
        "type":                "ObjectEvent",
        "action":              "OBSERVE",
        "eventTime":           ts,
        "eventTimeZoneOffset": "+00:00",
        "bizStep":             biz_step,
        "disposition":         disposition,
        "epcList":             [sgtin],
        "readPoint":           {"id": read_point_sgln},
        "bizLocation":         {"id": biz_location_sgln},
        "sourceList":          sources,
        "destinationList": [
            {"type": "location",         "destination": dest_sgln},
            {"type": "possessing_party", "destination": dest_pgln},
            {"type": "owning_party",     "destination": dest_pgln},
        ],
        "ilmd": {
            "cbvmda:lotNumber":     transaction_id,
            "cbvmda:transactionID": transaction_id,
        },
    }


# ─── AggregationEvent builder ─────────────────────────────────────────────────

def make_aggregation_event(parent_sscc, child_sgtins, action,
                           biz_step, disposition, event_time,
                           read_point_sgln, biz_location_sgln,
                           source_sgln, source_pgln,
                           dest_sgln, dest_pgln,
                           transaction_id):
    """
    Builds one GS1 EPCIS 2.0 AggregationEvent.

    action = "ADD"    — items packed into container (packing at origin)
    action = "DELETE" — items unpacked from container (receiving at destination)

    GS1 CBV 2.0 §7.3:
      ADD    → items are aggregated into a parent (packing / consolidation)
      DELETE → items are disaggregated from parent (unpacking / deconsolidation)

    Reference: https://ref.gs1.org/standards/epcis
    """
    ts        = event_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    event_key = f"{parent_sscc}|{action}|{biz_step}|{ts}".encode()
    event_id  = "urn:uuid:" + hashlib.sha256(event_key).hexdigest()[:32]

    return {
        "eventID":             event_id,
        "type":                "AggregationEvent",
        "action":              action,
        "eventTime":           ts,
        "eventTimeZoneOffset": "+00:00",
        "bizStep":             biz_step,
        "disposition":         disposition,
        "parentID":            parent_sscc,
        "childEPCs":           list(child_sgtins),
        "readPoint":           {"id": read_point_sgln},
        "bizLocation":         {"id": biz_location_sgln},
        "sourceList": [
            {"type": "location",         "source": source_sgln},
            {"type": "possessing_party", "source": source_pgln},
            {"type": "owning_party",     "source": source_pgln},
        ],
        "destinationList": [
            {"type": "location",         "destination": dest_sgln},
            {"type": "possessing_party", "destination": dest_pgln},
            {"type": "owning_party",     "destination": dest_pgln},
        ],
        "ilmd": {
            "cbvmda:lotNumber":     transaction_id,
            "cbvmda:transactionID": transaction_id,
        },
    }


# ─── TransactionEvent builder ─────────────────────────────────────────────────

def make_transaction_event(sgtins, biz_trans_type, biz_trans_id,
                           biz_step, disposition, event_time,
                           biz_location_sgln,
                           source_pgln, dest_pgln,
                           transaction_id):
    """
    Builds one GS1 EPCIS 2.0 TransactionEvent.

    Links a set of EPCs to a business transaction document (purchase order,
    invoice, despatch advice, etc.).

    Note: Some implementations refer to this as "TransferEvent". In GS1 EPCIS
    2.0 the official type name is "TransactionEvent".

    GS1 CBV 2.0 §7.4 — Business Transaction Types:
      urn:epcglobal:cbv:btt:po       — Purchase Order
      urn:epcglobal:cbv:btt:inv      — Invoice
      urn:epcglobal:cbv:btt:desadv   — Despatch Advice (ASN)
      urn:epcglobal:cbv:btt:recadv   — Receiving Advice

    Reference: https://ref.gs1.org/standards/cbv
    """
    ts        = event_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    event_key = f"{transaction_id}|{biz_trans_type}|{biz_step}|{ts}".encode()
    event_id  = "urn:uuid:" + hashlib.sha256(event_key).hexdigest()[:32]

    return {
        "eventID":             event_id,
        "type":                "TransactionEvent",
        "action":              "OBSERVE",
        "eventTime":           ts,
        "eventTimeZoneOffset": "+00:00",
        "bizStep":             biz_step,
        "disposition":         disposition,
        "epcList":             list(sgtins),
        "bizTransactionList": [
            {"type": biz_trans_type, "bizTransaction": biz_trans_id},
        ],
        "readPoint":           {"id": biz_location_sgln},
        "bizLocation":         {"id": biz_location_sgln},
        "sourceList": [
            {"type": "possessing_party", "source": source_pgln},
            {"type": "owning_party",     "source": source_pgln},
        ],
        "destinationList": [
            {"type": "possessing_party", "destination": dest_pgln},
            {"type": "owning_party",     "destination": dest_pgln},
        ],
        "ilmd": {
            "cbvmda:lotNumber":     transaction_id,
            "cbvmda:transactionID": transaction_id,
        },
    }


# ─── Lot-level event builder (AggregationEvent + TransactionEvent) ────────────

def lot_level_events(row, all_sgtins, wp_dates, lot_events):
    """
    Generates AggregationEvent (ADD + DELETE) and TransactionEvent for a full lot.
    Called ONCE per lot (not per SGTIN) by convert_scenario.

    Normal flow:
      AggregationEvent ADD   (packing at EU origin)     — all SGTINs → SSCC
      TransactionEvent       (shipping PO confirmation) — links PO to lot
      AggregationEvent DELETE (receiving at US dest)    — unpack SSCC
      TransactionEvent        (receiving confirmation)  — buyer confirms receipt
    """
    txn_id       = row["transaction_id"]
    anomaly_type = row.get("anomaly_type", "none")
    # eu_manufacturer_gln is stored as PGLN by transform.py — derive SGLN from it
    eu_pgln      = row.get("eu_manufacturer_gln", PGLN_EU_PORT)
    eu_gln       = eu_pgln.replace("urn:epc:id:pgln:", "urn:epc:id:sgln:") + ".0"
    buyer_id     = row["buyer_id"]
    us_sgln      = dea_to_sgln(buyer_id)
    us_pgln      = dea_to_pgln(buyer_id)
    sscc         = row.get("sscc", f"urn:epc:id:sscc:0000000.0000000000")
    po_number    = row.get("po_number", txn_id)

    date_a = parse_date(wp_dates[0])
    date_b = parse_date(wp_dates[1]) if len(wp_dates) > 1 else date_a + timedelta(days=1)
    date_c = parse_date(wp_dates[2]) if len(wp_dates) > 2 else date_b + timedelta(days=12)

    # For transit_diversion: receiving comes to a synthetic "wrong" DEA location
    # derived by incrementing the last digit of buyer_id
    wrong_buyer_id = buyer_id[:-1] + str((int(buyer_id[-1]) + 1) % 10)
    wrong_sgln     = dea_to_sgln(wrong_buyer_id)
    wrong_pgln     = dea_to_pgln(wrong_buyer_id)

    # For quantity_discrepancy: DELETE only has 70% of the SGTINs
    import math
    diverted_count = max(1, math.floor(len(all_sgtins) * 0.70))
    received_sgtins = all_sgtins[:diverted_count]  # 70% arrive, 30% "disappeared"

    if anomaly_type == "quantity_discrepancy":
        # ADD: full lot packed into container
        lot_events.append(make_aggregation_event(
            sscc, all_sgtins, "ADD", "packing", "active",
            date_a + timedelta(hours=2),
            eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT, txn_id,
        ))
        # TransactionEvent: seller ships full lot
        lot_events.append(make_transaction_event(
            all_sgtins, "urn:epcglobal:cbv:btt:desadv", po_number,
            "shipping", "in_transit", date_b,
            GLN_EU_PORT, eu_pgln, us_pgln, txn_id,
        ))
        # DELETE: only 70% arrive — quantity discrepancy detected here
        lot_events.append(make_aggregation_event(
            sscc, received_sgtins, "DELETE", "receiving", "active",
            date_c - timedelta(hours=1),
            us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln, txn_id,
        ))
        # TransactionEvent: buyer confirms receipt of partial lot
        lot_events.append(make_transaction_event(
            received_sgtins, "urn:epcglobal:cbv:btt:recadv", po_number,
            "receiving", "active", date_c,
            us_sgln, us_pgln, us_pgln, txn_id,
        ))

    elif anomaly_type == "shipment_divergence":
        # Seller packs and emits despatch advice — but buyer never confirms receipt
        lot_events.append(make_aggregation_event(
            sscc, all_sgtins, "ADD", "packing", "active",
            date_a + timedelta(hours=2),
            eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT, txn_id,
        ))
        # TransactionEvent: seller emits despatch advice (seller side only)
        lot_events.append(make_transaction_event(
            all_sgtins, "urn:epcglobal:cbv:btt:desadv", po_number,
            "shipping", "in_transit", date_b,
            GLN_EU_PORT, eu_pgln, us_pgln, txn_id,
        ))
        # No AggregationEvent DELETE — no receiving confirmation from buyer
        # TransactionEvent recadv is intentionally absent → SHIPMENT_DIVERGENCE

    elif anomaly_type == "transit_diversion":
        # Normal packing at origin
        lot_events.append(make_aggregation_event(
            sscc, all_sgtins, "ADD", "packing", "active",
            date_a + timedelta(hours=2),
            eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT, txn_id,
        ))
        # TransactionEvent: PO says destination = buyer (us_pgln)
        lot_events.append(make_transaction_event(
            all_sgtins, "urn:epcglobal:cbv:btt:desadv", po_number,
            "shipping", "in_transit", date_b,
            GLN_EU_PORT, eu_pgln, us_pgln, txn_id,
        ))
        # DELETE: arrives at WRONG location (wrong_sgln ≠ us_sgln from PO)
        lot_events.append(make_aggregation_event(
            sscc, all_sgtins, "DELETE", "receiving", "active",
            date_c - timedelta(hours=1),
            wrong_sgln, wrong_sgln,
            GLN_EU_PORT, PGLN_EU_PORT, wrong_sgln, wrong_pgln, txn_id,
        ))
        # TransactionEvent: receiving at wrong location
        lot_events.append(make_transaction_event(
            all_sgtins, "urn:epcglobal:cbv:btt:recadv", po_number,
            "receiving", "active", date_c,
            wrong_sgln, wrong_pgln, wrong_pgln, txn_id,
        ))

    else:
        # Normal flow (none, recalled, out_of_order, etc.) — full lot events
        lot_events.append(make_aggregation_event(
            sscc, all_sgtins, "ADD", "packing", "active",
            date_a + timedelta(hours=2),
            eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT, txn_id,
        ))
        lot_events.append(make_transaction_event(
            all_sgtins, "urn:epcglobal:cbv:btt:desadv", po_number,
            "shipping", "in_transit", date_b,
            GLN_EU_PORT, eu_pgln, us_pgln, txn_id,
        ))
        lot_events.append(make_aggregation_event(
            sscc, all_sgtins, "DELETE", "receiving", "active",
            date_c - timedelta(hours=1),
            us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln, txn_id,
        ))
        lot_events.append(make_transaction_event(
            all_sgtins, "urn:epcglobal:cbv:btt:recadv", po_number,
            "receiving", "active", date_c,
            us_sgln, us_pgln, us_pgln, txn_id,
        ))


# ─── Per-anomaly event sequence builders ─────────────────────────────────────

def events_for_lot(row, sgtin, wp_dates, lot_events, unit_index=0):
    """
    Dispatches to the correct sequence builder based on anomaly_type.
    Appends generated events to lot_events (mutates in place).
    """
    txn_id       = row["transaction_id"]
    reporter_id  = row["reporter_id"]
    buyer_id     = row["buyer_id"]
    anomaly_type = row.get("anomaly_type", "none")
    # eu_manufacturer_gln is stored as PGLN by transform.py — derive SGLN from it
    eu_pgln      = row.get("eu_manufacturer_gln", PGLN_EU_PORT)
    eu_gln       = eu_pgln.replace("urn:epc:id:pgln:", "urn:epc:id:sgln:") + ".0"

    # Waypoint dates: A=wp_dates[0], B=wp_dates[1], C=wp_dates[2]
    date_a = parse_date(wp_dates[0])                        # EU Manufacturer
    date_b = parse_date(wp_dates[1]) if len(wp_dates) > 1 else date_a + timedelta(days=1)
    date_c = parse_date(wp_dates[2]) if len(wp_dates) > 2 else date_b + timedelta(days=12)

    # US destination locations
    us_sgln = dea_to_sgln(buyer_id)
    us_pgln = dea_to_pgln(buyer_id)

    # Extra sourceList entries for many_suppliers
    extra_sources = []
    if anomaly_type == "many_suppliers":
        for extra_id in row.get("extra_distributor_ids", "").split(";"):
            extra_id = extra_id.strip()
            if extra_id:
                extra_sources.append({"type": "possessing_party", "source": extra_id})

    def evt(biz_step, disposition, t, r_pt, biz_loc, src_sgln, src_pgln, dst_sgln, dst_pgln):
        lot_events.append(make_event(
            sgtin, biz_step, disposition, t,
            r_pt, biz_loc, src_sgln, src_pgln, dst_sgln, dst_pgln,
            txn_id, extra_sources if anomaly_type == "many_suppliers" else None,
        ))

    if anomaly_type == "skip_commission":
        # Class A: commissioning event is missing — start from shipping
        evt("shipping",  "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT,
            eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("receiving", "active",     date_c, us_sgln, us_sgln,
            GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln)

    elif anomaly_type == "out_of_order":
        # receiving before shipping — dates stay correct but order is swapped
        evt("commissioning", "active",     date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("receiving",     "active",     date_c, us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)

    elif anomaly_type == "recalled":
        # commissioned with recalled disposition → blocked
        evt("commissioning", "recalled",   date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("receiving",     "active",     date_c, us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln)

    elif anomaly_type == "jurisdiction":
        # dispensing as bizStep at receiving — wrong step for EU→US commercial transfer
        evt("commissioning", "active",     date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("dispensing",    "active",     date_c, us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln)

    elif anomaly_type == "item_location_mismatch":
        # Normal lot events at correct waypoints
        evt("commissioning", "active",     date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("receiving",     "active",     date_c, us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln)
        # Anomalous extra event: ONLY for unit_index == 0 (one rogue box).
        # This box is already at the last waypoint on the production date (date_a),
        # while the rest of the lot hasn't left the manufacturer yet.
        if unit_index == 0:
            try:
                mismatch_idx = int(row.get("item_mismatch_waypoint_index", len(wp_dates) - 1))
            except (ValueError, TypeError):
                mismatch_idx = len(wp_dates) - 1
            mismatch_date = parse_date(wp_dates[mismatch_idx]) if mismatch_idx < len(wp_dates) else date_c
            lot_events.append(make_event(
                sgtin, "receiving", "active", mismatch_date,
                us_sgln, us_sgln,
                eu_gln, eu_pgln, us_sgln, us_pgln,
                txn_id,
            ))

    elif anomaly_type == "quantity_discrepancy":
        # ObjectEvents: normal sequence — discrepancy visible in AggregationEvents (lot_level_events)
        evt("commissioning", "active",     date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("receiving",     "active",     date_c, us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln)

    elif anomaly_type == "shipment_divergence":
        # Seller emits commissioning + shipping — buyer never confirms receiving
        evt("commissioning", "active",     date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        # receiving intentionally absent — product shipped but never confirmed received

    elif anomaly_type == "transit_diversion":
        # Shipped to correct buyer; ObjectEvent receiving arrives from wrong location
        wrong_buyer_id = buyer_id[:-1] + str((int(buyer_id[-1]) + 1) % 10)
        wrong_sgln     = dea_to_sgln(wrong_buyer_id)
        wrong_pgln     = dea_to_pgln(wrong_buyer_id)
        evt("commissioning", "active",     date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("receiving",     "active",     date_c, wrong_sgln, wrong_sgln, GLN_EU_PORT, PGLN_EU_PORT, wrong_sgln, wrong_pgln)

    else:
        # "none", "many_suppliers", "impossible_transit" — normal sequence
        # For impossible_transit: waypoint_dates already carry anomalous timing
        evt("commissioning", "active",     date_a, eu_gln, eu_gln, eu_gln, eu_pgln, GLN_EU_PORT, PGLN_EU_PORT)
        evt("packing",       "active",     date_a + timedelta(hours=1), eu_gln, eu_gln, eu_gln, eu_pgln, eu_gln, eu_pgln)
        evt("shipping",      "in_transit", date_b, GLN_EU_PORT, GLN_EU_PORT, eu_gln, eu_pgln, us_sgln, us_pgln)
        evt("receiving",     "active",     date_c, us_sgln, us_sgln, GLN_EU_PORT, PGLN_EU_PORT, us_sgln, us_pgln)


# ─── Main converter ───────────────────────────────────────────────────────────

def convert_scenario(input_path, output_path, limit=None, max_units_per_lot=None, max_events=None):
    """
    Reads scenario_template.csv and writes a GS1 EPCIS 2.0 EPCISDocument JSON.

    max_units_per_lot: cap SGTIN units per lot (None = module default).
    max_events:        cap total events. When set, picks 1 transaction per drug
                       in round-robin order so all drugs are represented evenly.
    """
    unit_cap = max_units_per_lot if max_units_per_lot is not None else LOT_SIZE_CAP

    # Load all rows into memory (CSV is small — at most a few thousand rows)
    with open(input_path, newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    if limit:
        all_rows = all_rows[:limit]

    # When capping events, interleave 1 transaction per drug so every drug appears
    if max_events:
        from collections import defaultdict
        by_drug = defaultdict(list)
        for row in all_rows:
            by_drug[row.get("drug_code", "unknown")].append(row)

        # Round-robin: take 1 row per drug per pass until we have enough rows
        interleaved = []
        drug_queues = list(by_drug.values())
        i = 0
        while len(interleaved) < len(all_rows):
            added = False
            for q in drug_queues:
                if i < len(q):
                    interleaved.append(q[i])
                    added = True
            if not added:
                break
            i += 1
        all_rows = interleaved

    events = []
    for row_count, row in enumerate(all_rows):
        if max_events and len(events) >= max_events:
            break

        txn_id = row["transaction_id"].strip() or f"row-{row_count}"
        row["transaction_id"] = txn_id

        wp_dates = [d.strip() for d in row.get("waypoint_dates", "").split(";") if d.strip()]
        if not wp_dates:
            wp_dates = [row.get("transaction_date", "").strip()]

        try:
            lot_size = min(int(float(row.get("dosage_unit", "1") or "1")), unit_cap)
        except ValueError:
            lot_size = 1
        lot_size = max(lot_size, 1)

        for unit_index in range(lot_size):
            sgtin      = generate_sgtin(txn_id, unit_index, row.get("drug_code", ""))
            lot_events = []
            events_for_lot(row, sgtin, wp_dates, lot_events, unit_index)
            events.extend(lot_events)

        all_sgtins = [generate_sgtin(txn_id, i, row.get("drug_code", "")) for i in range(lot_size)]
        agg_events = []
        lot_level_events(row, all_sgtins, wp_dates, agg_events)
        events.extend(agg_events)

    if max_events:
        events = events[:max_events]

    document = {
        "@context":      ["https://ref.gs1.org/standards/epcis/epcis-context.jsonld"],
        "type":          "EPCISDocument",
        "schemaVersion": "2.0",
        "creationDate":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epcisBody":     {"eventList": events},
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, ensure_ascii=False)

    by_type = {}
    sgtins  = set()
    for e in events:
        t = e.get("type", "Unknown")
        by_type[t] = by_type.get(t, 0) + 1
        if "epcList" in e:
            for epc in e["epcList"]: sgtins.add(epc)
        if "childEPCs" in e:
            for epc in e["childEPCs"]: sgtins.add(epc)

    return {
        "transactions": row_count,
        "sgtins":       len(sgtins),
        "events":       len(events),
        "by_type":      by_type,
        "output":       output_path,
    }


def main():
    BASE = Path(__file__).parent.parent
    parser = argparse.ArgumentParser(
        description="Task 2 — Convert scenario_template.csv to EPCIS 2.0 JSON"
    )
    parser.add_argument("--input",  "-i", default=str(BASE / "data/enriched/scenario_template.csv"), help="scenario_template.csv from transform.py")
    parser.add_argument("--output", "-o", default=str(BASE / "data/epcis/events.json"), help="Output EPCIS 2.0 JSON file")
    parser.add_argument("--limit",  "-n", type=int, default=None, help="Max transactions to process")
    args = parser.parse_args()

    result = convert_scenario(args.input, args.output, args.limit)
    print(f"Transactions: {result['transactions']:,}")
    print(f"SGTINs:       {result['sgtins']:,}  (cap {LOT_SIZE_CAP}/lot)")
    print(f"Events:       {result['events']:,}")
    print(f"Output:       {result['output']}")


if __name__ == "__main__":
    main()
