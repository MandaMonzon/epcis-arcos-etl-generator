"""Independent injection labels. Never infer ground truth from detector output."""
import csv
import hashlib
import json
from collections import Counter


EXPECTED_CODES = {
    "out_of_order": "LOGISTICS_DIVERSION_SEQUENCE",
    "timestamp_mismatch": "LOGISTICS_DIVERSION_TIMING",
    "jurisdiction": "LOGISTICS_DIVERSION_JURISDICTION",
    "many_suppliers": "SUPPLY_BASE_COMPLEXITY",
}


def write_ground_truth(scenario_path, events_path, output_path):
    with open(scenario_path, encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    with open(events_path, encoding="utf-8") as stream:
        events = json.load(stream)["epcisBody"]["eventList"]
    labels = {}
    for row in rows:
        txn = row["transaction_id"]
        if txn in labels:
            raise ValueError(f"Ambiguous transaction_id in ground truth: {txn}")
        labels[txn] = row
    grouped = {txn: [] for txn in labels}
    ids = set()
    for event in events:
        if event["eventID"] in ids:
            raise ValueError(f"Duplicate eventID: {event['eventID']}")
        ids.add(event["eventID"])
        grouped[event["ilmd"]["cbvmda:transactionID"]].append(event)
    records = []
    for txn, row in labels.items():
        kind = row.get("anomaly_type", "none")
        history = grouped[txn]
        target_ids = [e["eventID"] for e in history
                      if e["type"] == "ObjectEvent" and (
                          (kind in ("out_of_order", "timestamp_mismatch", "many_suppliers") and e["bizStep"] == "receiving") or
                          (kind == "jurisdiction" and e["bizStep"] == "dispensing"))]
        records.append({
            "transaction_id": txn, "drug_code": row["drug_code"],
            "injected": kind != "none", "anomaly_type": kind,
            "diversion_anomaly": kind not in ("none", "recalled"),
            "status_control_event_ids": [e["eventID"] for e in history
                if kind == "recalled" and e.get("disposition") == "recalled"],
            "history_event_ids": [e["eventID"] for e in history],
            "history_epcs": sorted({epc for e in history
                                    for epc in e.get("epcList", e.get("childEPCs", []))}),
            "target_event_ids": target_ids,
            "expected_rule_code": EXPECTED_CODES.get(kind),
            "coverage_status": "clean_control" if kind == "none" else "negative_control" if kind == "recalled" else "mapped" if kind in EXPECTED_CODES else "unsupported",
            "expectation_scope": "ObjectEvents in generated submission order; fresh item state",
        })
    result = {
        "label_schema": "diversion-v2-status-controls",
        "label_unit": "transaction", "transactions": len(records),
        "injected_transactions": sum(r["injected"] for r in records),
        "diversion_anomaly_transactions": sum(r["diversion_anomaly"] for r in records),
        "status_control_transactions": sum(r["anomaly_type"] == "recalled" for r in records),
        "counts_by_type": dict(Counter(r["anomaly_type"] for r in records)),
        "events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
        "scenario_sha256": hashlib.sha256(scenario_path.read_bytes()).hexdigest(),
        "caveat": "Clean means no deliberate injection, not verified rule compliance. "
                  "History events/EPCs are context, not individual anomaly labels. "
                  "Recall is a negative status control, not a diversion anomaly. "
                  "Null expected_rule_code otherwise denotes an unverified detector expectation.",
        "records": records,
    }
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
