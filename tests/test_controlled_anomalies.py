import csv
import json
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.transform import build_scenario
from pipeline.load import convert_scenario, events_for_lot, parse_date
from pipeline.transform import sample_waypoint_dates, WAYPOINTS
from pipeline.ground_truth import write_ground_truth


class ControlledAnomaliesTest(unittest.TestCase):
    def test_counts_reproducibility_and_ground_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'clean.csv'
            with source.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['transaction_id', 'reporter_id',
                    'buyer_id', 'transaction_date', 'drug_code', 'dosage_unit'])
                writer.writeheader()
                for i in range(200):
                    writer.writerow(dict(transaction_id=str(i), reporter_id='AB1234567',
                        buyer_id='BC1234567', transaction_date='2006-01-01',
                        drug_code='00000000001', dosage_unit='1'))
            def generate(rate, seed=42):
                return build_scenario(source, root / 'scenario.csv',
                    sample_per_drug=200, anomaly_rate=rate, anomaly_seed=seed)['rows']
            first = generate(.01)
            self.assertEqual(sum(r['anomaly_type'] != 'none' for r in first), 2)
            self.assertEqual(first, generate(.01))
            self.assertNotEqual(first, generate(.01, 43))
            generate(.01)
            convert_scenario(root / 'scenario.csv', root / 'events.json', max_units_per_lot=1)
            truth = write_ground_truth(root / 'scenario.csv', root / 'events.json', root / 'truth.json')
            self.assertEqual(truth['injected_transactions'], 2)
            generated = json.loads((root / 'events.json').read_text())['epcisBody']['eventList']
            for event in generated:
                self.assertTrue(all(entry.get('jurisdiction') in ('EU', 'US')
                                    for entry in event['sourceList'] + event['destinationList']))
                if event['bizStep'] == 'receiving':
                    owners = [entry['source'] for entry in event['sourceList'] if entry['type'] == 'owning_party']
                    self.assertEqual(owners, ['urn:epc:id:pgln:us.dea.AB1234567'])
            targets = [r for r in truth['records'] if r['injected']]
            self.assertTrue(all(r['target_event_ids'] and r['expected_rule_code'] for r in targets))
            self.assertTrue(all(r['anomaly_type'] == 'none' for r in generate(0)))
            self.assertTrue(all(r['anomaly_type'] != 'none' for r in generate(1)))
            self.assertEqual(sum(r['anomaly_type'] != 'none' for r in generate(.001)), 0)
            with self.assertRaises(ValueError):
                generate(-.1)

    def test_timestamp_scenario_against_current_chaincode(self):
        histories = []
        for index, kind in enumerate(("none", "timestamp_mismatch")):
            row = dict(transaction_id=f"timing-{index}", reporter_id="AB1234567",
                       buyer_id="BC1234567", anomaly_type=kind,
                       eu_manufacturer_gln="urn:epc:id:pgln:4000001.00000")
            events = []
            # Exercise legacy same-day waypoint input as well as new sampling.
            events_for_lot(row, f"urn:epc:id:sgtin:0614141.107346.{index}",
                           ["2006-01-01", "2006-01-01", "2006-01-10"], events)
            histories.append(events)
        for index in range(100):
            dates = sample_waypoint_dates("2006-01-01", str(index), "none", WAYPOINTS)
            self.assertGreater(dates[1], dates[0])
        with self.assertRaises(ValueError):
            parse_date("invalid")
        with self.assertRaises(ValueError):
            sample_waypoint_dates("invalid", "1", "none", WAYPOINTS)
        contract = Path(__file__).resolve().parents[2] / "teste-smartcontract/chaincode/traceability/lib/traceabilityContract.js"
        script = r"""
const fs = require('fs');
const Contract = require(process.argv[1]);
const histories = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const results = [];
  for (const events of histories) {
    const state = new Map();
    const ctx = {stub: {getState: async k => state.get(k) || Buffer.alloc(0),
                       putState: async (k,v) => state.set(k,v)}};
    const contract = new Contract();
    const errors = [];
    for (const event of events) {
      try { await contract.createEpcisEvent(ctx, JSON.stringify(event)); }
      catch (error) { errors.push(error.message.split(':')[0]); }
    }
    results.push(errors);
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => {console.error(error); process.exit(1);});
"""
        result = subprocess.run(["node", "-e", script, str(contract)],
                                input=json.dumps(histories), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [[], ["LOGISTICS_DIVERSION_TIMING"]])

    def test_complete_mixed_histories_preserve_injected_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for i, kind in enumerate(("none", "none", "out_of_order", "timestamp_mismatch")):
                rows.append(dict(transaction_id=f"mixed-{i}", reporter_id="AB1234567",
                    buyer_id="BC1234567", transaction_date="2006-01-01",
                    drug_code="00000000001", dosage_unit="3",
                    eu_manufacturer_gln="urn:epc:id:pgln:4000001.00000",
                    sscc=f"urn:epc:id:sscc:4000001.{i:010d}", po_number=f"po-{i}",
                    anomaly_type=kind, waypoint_dates="2006-01-01;2006-01-01;2006-01-10"))
            scenario = root / "scenario.csv"
            with scenario.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            convert_scenario(scenario, root / "events.json", max_units_per_lot=3)
            truth = write_ground_truth(scenario, root / "events.json", root / "truth.json")
            events = json.loads((root / "events.json").read_text())["epcisBody"]["eventList"]
            contract = Path(__file__).resolve().parents[2] / "teste-smartcontract/chaincode/traceability/lib/traceabilityContract.js"
            script = r"""
const fs = require('fs'); const Contract = require(process.argv[1]);
(async () => {
  const events = JSON.parse(fs.readFileSync(0,'utf8')); const state = new Map();
  const ctx = {stub:{getState:async k=>state.get(k)||Buffer.alloc(0),putState:async(k,v)=>state.set(k,v)}};
  const contract = new Contract(); const errors = {};
  for (const event of events) {
    try { await contract.createEpcisEvent(ctx,JSON.stringify(event)); }
    catch(error) { errors[event.eventID]=error.message.split(':')[0]; }
  }
  process.stdout.write(JSON.stringify({errors,state:Object.fromEntries([...state].map(([k,v])=>[k,JSON.parse(v.toString())]))}));
})().catch(e=>{console.error(e);process.exit(1)});
"""
            response = subprocess.run(["node", "-e", script, str(contract)],
                input=json.dumps(events), capture_output=True, text=True, check=True)
            result = json.loads(response.stdout)
            for record in truth['records']:
                if not record['injected']:
                    self.assertTrue(all(eid not in result['errors'] for eid in record['history_event_ids']))
                    for epc in record['history_epcs']:
                        state = result['state']['product_state_' + epc]
                        self.assertEqual(state['lastBizStep'], 'receiving')
                        self.assertEqual(len(state['history']), 8)
                        self.assertEqual(set(state['observationsByType']), {'AggregationEvent', 'TransactionEvent'})
                else:
                    self.assertEqual(len(record['target_event_ids']), 3)
                    for target in record['target_event_ids']:
                        self.assertEqual(result['errors'].get(target), record['expected_rule_code'])
            self.assertEqual(len(events), 64)


if __name__ == '__main__':
    unittest.main()
