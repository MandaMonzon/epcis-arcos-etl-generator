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
