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
Use a seeded random sample over the whole raw file instead:

```bash
python3 main.py --data arcos_raw.gz --sample-rows 300000 --seed 1 --sample-per-drug 10 --balanced --max-units-per-lot 10
```

Design (default `--sample-design stratified`, after Cochran 1977, *Sampling Techniques*), in ONE pass over the file:
- Strata are the drugs. Each valid row gets a seeded uniform key; rows below a shared threshold are kept, and
  the threshold drops whenever more than 2x the budget is held (nested keys keep the sample uniform). Memory
  is at most 2 x `--sample-rows` rows plus the floor.
- Allocation is proportional to the drug's size, with a floor of `--sample-per-drug` rows. Rare drugs are not
  lost, drugs with fewer rows than the floor are taken whole, and inside each drug the sample is a uniform
  random subset. The total is the budget plus the floor top-ups.
- Each row records `sampling_weight = N_h / n_h` (drug rows in the file / rows sampled, counted in the same
  pass), so population totals can be rebuilt (Horvitz-Thompson). Without weights, the floor over-represents
  rare drugs.
- `--sample-design simple` is a plain uniform sample of all valid rows (rare drugs may vanish).
- `extraction_accounting.json` records the population size, `not_in_random_sample` and the representativeness
  (stratified: weighted year total variation, strata taken whole; simple: chi-square vs population).
- Why the scenario step still takes `--sample-per-drug` rows per drug: equal allocation gives every drug the
  same number of lots on purpose, to test the rules across drugs, not to estimate national totals.
- Tests: `tests/test_sampling.py`.

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
