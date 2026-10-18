# epcis-arcos-etl-generator

Converts raw ARCOS DEA opioid transaction data into GS1 EPCIS 2.0 events for blockchain benchmarking.

## Quick start

```bash
python3 main.py
```

Default run: **10 drugs, 10 transactions each, max 100 EPCIS events**.

## Changing the dataset size

Edit the parameters directly in the terminal:

```bash
# Smaller — quick test
python3 main.py --max-drugs 10 --drugs 10 --units 1 --max-events 100

# Larger — more representative benchmark
python3 main.py --max-drugs 100 --drugs 10 --units 1

# Full dataset (26M rows, takes ~10 min)
python3 main.py
```

| Parameter | Description |
|---|---|
| `--max-drugs N` | How many distinct drug products to include |
| `--drugs N` | Transactions per drug |
| `--units N` | SGTIN units per lot (1 = compact, 10 = realistic) |
| `--max-events N` | Hard cap on total EPCIS events in output |
| `--lines N` | Max rows to read from ARCOS (default: 100,000) |

Each run is saved to `data/epcis/<run-id>/` — previous runs are never overwritten.

## Pipeline

```
data/raw/arcos_raw.gz
  → pipeline/extract.py   → data/normalized/arcos_clean.csv   (7 columns)
  → pipeline/transform.py → data/enriched/<run>/scenario_template.csv
  → pipeline/load.py      → data/epcis/<run>/events.json
```

## SGTIN generation

Each transaction gets a deterministic serialized identifier:

```
serial = Truncate_12(HMAC-SHA256("GS1-EPCIS-ARCOS-V1", transaction_id || unit_index))
SGTIN  = urn:epc:id:sgtin:{company_prefix}.{item_ref}.{serial}
```

Same input → same SGTIN every time (reproducible across runs).

### Controlled anomaly experiments

The pipeline entry point now defaults to 1% injected transactions, replacing the
legacy high-prevalence cycle. Run a clean control first, then a separate mixed run:

```bash
python3 main.py --data data/raw/arcos_raw.gz --anomaly-rate 0
# Archive output/ before generating the next dataset.
python3 main.py --data data/raw/arcos_raw.gz --anomaly-rate 0.01 --anomaly-seed 42 --anomaly-types recalled out_of_order
```

The count is `floor(selected_transactions * anomaly_rate)`, sampled without
replacement. Thus 1,000 transactions produce exactly 10 injected transactions;
fewer than 100 produce zero at 1%. Types are distributed evenly (counts differ by
at most one) across the selected transactions. The seed controls injection
placement; repeatability requires the same source rows and selection parameters.
This rate is transaction prevalence, not item/event prevalence or real counterfeit
prevalence. All items in the default recalled/out_of_order transactions undergo
the scenario; unequal transaction sizes can change item prevalence.

`ground_truth.json` contains every selected transaction, its injection label,
history EPCs and event IDs, target event IDs for the default scenarios, expected
rule code, and SHA-256 hashes of the scenario and event files. Labels are kept
outside the EPCIS payload. Ambiguous transaction IDs and duplicate event IDs
abort ground-truth generation; do not benchmark such output. Other selectable
scenario types have unverified expectations and null expected rule codes.

For validation, submit complete ObjectEvent histories in generated order against
fresh item state and preserve event IDs (or record a mapping). Count a supported
injection as correctly detected only when its target event produces its expected
rule code. Also measure false positives among non-injected histories. Do not
count timeouts, generic rejections, or unrelated rule failures as correct anomaly
detection. "No injection" does not guarantee a valid history: the existing clean
timestamp, mixed-event schema/order, supplier mapping, and workload compatibility
issues still need correction before a full Fabric round. This change does not
alter chaincode rules or certify that the clean control passes.

The existing extraction cache in `output/arcos_clean.csv` is reused by main.py;
verify its provenance before changing source inputs. Archive each output directory
and record the deployed chaincode revision alongside the generated manifest.

Verification: `python3 -m unittest discover -s tests -v`.


### Intentional timestamp anomalies

Normal sampled transit starts at least one day after origin. Legacy same-day
waypoints place shipping at +3 hours, after packing (+1h) and aggregation (+2h).
Invalid dates and backward waypoint dates fail generation instead of using the
current clock or silently reversing normal histories.

To inject timestamp violations explicitly:

```bash
python3 main.py --data data/raw/arcos_raw.gz --anomaly-rate 0.01 --anomaly-seed 42 --anomaly-types timestamp_mismatch
```

This preserves commissioning → packing → shipping → receiving submission order,
but sets the receiving timestamp one second before shipping. Ground truth targets
that receiving ObjectEvent and expects LOGISTICS_DIVERSION_TIMING. The default
scenario selection is unchanged; timestamp_mismatch is opt-in. Do not sort this
history by eventTime before submission, as that changes the test.

The test suite exercises normal and anomalous ObjectEvent histories through the
current sibling teste-smartcontract checkout using Node.js and an in-memory
Fabric state stub. It does not establish mixed-event ordering correctness or
Fabric throughput. Running this integration check requires that sibling repo.
