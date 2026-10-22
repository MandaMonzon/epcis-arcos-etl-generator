import csv
import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pipeline.load import convert_scenario

HERE = Path(__file__).parent
MIN_TRANSIT_DAYS = 3
CBV = json.loads((HERE / "cbv_vocabulary.json").read_text())


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def generate_events():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "events.json"
        convert_scenario(str(HERE / "scenario_fixture.csv"), str(out), max_units_per_lot=10)
        return json.loads(out.read_text())["epcisBody"]["eventList"]


def load_rows():
    with open(HERE / "scenario_fixture.csv", newline="") as f:
        return {r["anomaly_type"]: r for r in csv.DictReader(f)}


def jurisdiction_of(urn):
    return "US" if ".us." in urn or "us.dea" in urn else "EU"


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_rows()
        events = generate_events()
        cls.by_tx = {}
        for e in events:
            cls.by_tx.setdefault(e["ilmd"]["cbvmda:transactionID"].strip(), []).append(e)
        cls.events = events

    def lot(self, anomaly):
        return self.by_tx[self.rows[anomaly]["transaction_id"].strip()]

    def unit_events(self, anomaly, index=0):
        lot = self.lot(anomaly)
        epc = next(e for e in lot if e["type"] == "ObjectEvent")["epcList"][0] if index == 0 else None
        return sorted((e for e in lot if e["type"] == "ObjectEvent" and epc in e["epcList"]),
                      key=lambda e: e["eventTime"])

    def steps(self, anomaly):
        return [e["bizStep"] for e in self.lot(anomaly) if e["type"] == "ObjectEvent"]

    def first_time(self, anomaly, step):
        return min(parse_time(e["eventTime"]) for e in self.lot(anomaly)
                   if e["type"] == "ObjectEvent" and e["bizStep"] == step)

    def test_every_scenario_in_the_docs_is_generated(self):
        documented = {"none", "recalled", "skip_commission", "out_of_order", "impossible_transit",
                      "transit_diversion", "jurisdiction", "quantity_discrepancy",
                      "shipment_divergence", "many_suppliers", "item_location_mismatch"}
        self.assertEqual(documented, set(self.rows))
        for anomaly in documented:
            self.assertTrue(self.lot(anomaly), anomaly)

    def test_none_follows_the_full_lifecycle_in_order(self):
        unit = self.unit_events("none")
        self.assertEqual(["commissioning", "packing", "shipping", "receiving"], [e["bizStep"] for e in unit])

    def test_recalled_keeps_a_normal_lifecycle_with_recall_disposition(self):
        unit = self.unit_events("recalled")
        self.assertEqual(["commissioning", "packing", "shipping", "receiving"], [e["bizStep"] for e in unit])
        self.assertEqual("recalled", unit[0]["disposition"])

    def test_skip_commission_receives_without_commissioning(self):
        self.assertNotIn("commissioning", self.steps("skip_commission"))
        self.assertIn("receiving", self.steps("skip_commission"))

    def test_out_of_order_records_receiving_before_shipping(self):
        self.assertLess(self.first_time("out_of_order", "receiving"), self.first_time("out_of_order", "shipping"))

    def test_impossible_transit_receives_in_less_than_minimum_days(self):
        days = (self.first_time("impossible_transit", "receiving")
                - self.first_time("impossible_transit", "shipping")).days
        self.assertLess(days, MIN_TRANSIT_DAYS)

    def test_normal_transit_respects_the_minimum_days(self):
        days = (self.first_time("none", "receiving") - self.first_time("none", "shipping")).days
        self.assertGreaterEqual(days, MIN_TRANSIT_DAYS)

    def test_transit_diversion_receives_at_a_different_location_than_declared(self):
        lot = self.lot("transit_diversion")
        declared = next(d["destination"] for e in lot if e["bizStep"] == "shipping" and e["type"] == "ObjectEvent"
                        for d in e["destinationList"] if d["type"] == "possessing_party")
        received = next(e["bizLocation"]["id"] for e in lot if e["bizStep"] == "receiving" and e["type"] == "ObjectEvent")
        buyer = self.rows["transit_diversion"]["buyer_id"]
        self.assertIn(buyer, declared)
        self.assertNotIn(buyer, received)

    def test_jurisdiction_dispenses_across_eu_and_us(self):
        event = next(e for e in self.lot("jurisdiction") if e["bizStep"] == "dispensing")
        source = jurisdiction_of(json.dumps(event["sourceList"]))
        destination = jurisdiction_of(json.dumps(event["destinationList"]))
        self.assertNotEqual(source, destination)

    def test_quantity_discrepancy_unpacks_fewer_units_than_packed(self):
        aggregations = [e for e in self.lot("quantity_discrepancy") if e["type"] == "AggregationEvent"]
        packed = next(len(e["childEPCs"]) for e in aggregations if e["action"] == "ADD")
        unpacked = next(len(e["childEPCs"]) for e in aggregations if e["action"] == "DELETE")
        self.assertLess(unpacked, packed * 0.9)

    def test_normal_lot_unpacks_every_unit(self):
        aggregations = [e for e in self.lot("none") if e["type"] == "AggregationEvent"]
        packed = next(len(e["childEPCs"]) for e in aggregations if e["action"] == "ADD")
        unpacked = next(len(e["childEPCs"]) for e in aggregations if e["action"] == "DELETE")
        self.assertEqual(packed, unpacked)

    def test_shipment_divergence_has_desadv_without_recadv(self):
        types = {b["type"] for e in self.lot("shipment_divergence") for b in e.get("bizTransactionList", [])}
        self.assertIn("urn:epcglobal:cbv:btt:desadv", types)
        self.assertNotIn("urn:epcglobal:cbv:btt:recadv", types)

    def test_many_suppliers_lists_at_least_nine_distinct_distributors(self):
        distributors = {s["source"] for e in self.lot("many_suppliers") for s in e.get("sourceList", [])
                        if "us.dea" in s["source"]}
        self.assertGreaterEqual(len(distributors), 9)

    def test_item_location_mismatch_has_one_unit_received_without_shipping(self):
        lot = self.lot("item_location_mismatch")
        units = {}
        for e in lot:
            if e["type"] == "ObjectEvent":
                units.setdefault(e["epcList"][0], []).append(e["bizStep"])
        rogue = [steps for steps in units.values() if steps.count("receiving") > 1]
        self.assertEqual(1, len(rogue))

    def test_event_vocabulary_follows_gs1_cbv_2_0(self):
        biz_steps = set(CBV["BizStep"])
        dispositions = set(CBV["Disp"])
        transaction_types = {f"urn:epcglobal:cbv:btt:{name}" for name in (n.split("-")[-1] for n in CBV["BTT"])}
        for e in self.events:
            self.assertIn(e["bizStep"], biz_steps)
            self.assertIn(e["disposition"], dispositions)
            for b in e.get("bizTransactionList", []):
                self.assertIn(b["type"], transaction_types)

    def test_event_times_are_iso_8601_utc(self):
        for e in self.events:
            self.assertRegex(e["eventTime"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


if __name__ == "__main__":
    unittest.main()
