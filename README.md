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

## Scientific sample

`--max-lines` and `extract --limit` only read the FIRST rows of the file, which is not a random sample.
Use a uniform random sample over the whole raw file instead:

```bash
python3 main.py --data arcos_raw.gz --sample-rows 300000 --seed 1 --sample-per-drug 10 --balanced --max-units-per-lot 10
```

- Algorithm R (Vitter 1985) over every valid row, seeded by `--seed`: same seed, same sample.
- `extraction_accounting.json` records the population size, `not_in_random_sample`, and a chi-square
  goodness-of-fit of the sample against the population by year and by drug (with total variation distance).
- `--balanced` still drops drugs with fewer than `--sample-per-drug` rows in the sample; the dropped drugs are
  reported in the scenario accounting.
- Tests: `tests/test_sampling.py` (uniform inclusion probability, reproducibility, accounting).

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

## Tests

`python3 -m unittest discover -s tests -t .` checks that the generator produces each scenario documented in the core repo (`docs/BUSINESS_RULES.md`) and that every `bizStep`, `disposition` and business transaction type is a GS1 CBV 2.0 value. `tests/cbv_vocabulary.json` is a snapshot of the official vocabulary from https://ref.gs1.org/cbv/.
