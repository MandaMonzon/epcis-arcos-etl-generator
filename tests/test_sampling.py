import csv
import gzip
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from pipeline.extract import extract
from pipeline.sampling import chi_square_p_value, representativeness

HEADER = ["reporter_dea_no", "buyer_dea_no", "transaction_date", "ndc_no", "calc_base_wt_in_gm", "quantity", "order_form_no"]


def population(n):
    rows = []
    for i in range(n):
        year = 2006 + (i * 14) // n
        drug = f"{(i % 7) + 10000000000}"
        rows.append(["AB1234567", f"CD{i:07d}", f"{year}0615", drug, "1.5", "10", f"OF-{i}"])
    return rows


def write_raw(path, rows):
    with gzip.open(path, "wt", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)


def run(rows, **kwargs):
    tmp = Path(tempfile.mkdtemp())
    write_raw(tmp / "raw.gz", rows)
    summary = extract(tmp / "raw.gz", tmp / "clean.csv", **kwargs)
    with open(tmp / "clean.csv", newline="") as f:
        return summary, list(csv.DictReader(f)), tmp


class RandomSampleTest(unittest.TestCase):
    def test_sample_has_requested_size_and_is_not_the_head_of_the_file(self):
        _, rows, _ = run(population(2000), sample_size=200, seed=1)
        self.assertEqual(len(rows), 200)
        years = {r["transaction_date"][:4] for r in rows}
        self.assertGreater(len(years), 10)

    def test_same_seed_is_reproducible_and_other_seed_differs(self):
        _, a, _ = run(population(1000), sample_size=100, seed=7)
        _, b, _ = run(population(1000), sample_size=100, seed=7)
        _, c, _ = run(population(1000), sample_size=100, seed=8)
        ids = lambda rows: [r["transaction_id"] for r in rows]
        self.assertEqual(ids(a), ids(b))
        self.assertNotEqual(ids(a), ids(c))

    def test_every_row_has_the_same_inclusion_probability(self):
        total, size, runs = 50, 10, 400
        inclusion = Counter()
        tmp = Path(tempfile.mkdtemp())
        write_raw(tmp / "raw.gz", population(total))
        for seed in range(runs):
            extract(tmp / "raw.gz", tmp / "clean.csv", sample_size=size, seed=seed)
            with open(tmp / "clean.csv", newline="") as f:
                inclusion.update(r["buyer_id"] for r in csv.DictReader(f))
        counts = [inclusion[f"CD{i:07d}"] for i in range(total)]
        expected = runs * size / total
        statistic = sum((c - expected) ** 2 / expected for c in counts)
        self.assertGreater(chi_square_p_value(statistic, total - 1), 0.001)

    def test_summary_reports_the_population_and_sample(self):
        summary, _, _ = run(population(1000), sample_size=100, seed=3)
        self.assertEqual(summary["population_valid"], 1000)
        self.assertEqual(summary["converted"], 100)
        self.assertEqual(summary["sample_seed"], 3)

    def test_every_valid_row_is_either_sampled_or_counted_as_not_sampled(self):
        summary, _, _ = run(population(1000), sample_size=100, seed=3)
        self.assertEqual(summary["rows_read"], summary["converted"] + summary["skipped"] + summary["not_in_random_sample"])

    def test_sample_larger_than_population_keeps_everything(self):
        summary, rows, _ = run(population(30), sample_size=100, seed=1)
        self.assertEqual(len(rows), 30)
        self.assertEqual(summary["population_valid"], 30)

    def test_invalid_rows_are_not_sampled_and_are_counted(self):
        rows = population(100) + [["AB1234567", "", "20200101", "12345678901", "1", "10", "X"]] * 20
        summary, sample, _ = run(rows, sample_size=50, seed=1)
        self.assertEqual(summary["population_valid"], 100)
        self.assertEqual(summary["skipped"], 20)
        self.assertTrue(all(r["buyer_id"] for r in sample))


class RepresentativenessTest(unittest.TestCase):
    def test_representative_sample_is_not_rejected(self):
        summary, _, _ = run(population(5000), sample_size=500, seed=2)
        report = summary["representativeness"]
        self.assertGreater(report["year"]["p_value"], 0.01)
        self.assertGreater(report["drug"]["p_value"], 0.01)

    def test_head_sample_is_rejected_by_the_year_test(self):
        pop = population(5000)
        counts_pop = Counter(r[2][:4] for r in pop)
        head = Counter(r[2][:4] for r in pop[:500])
        report = representativeness(counts_pop, head)
        self.assertLess(report["p_value"], 1e-6)

    def test_chi_square_p_value_matches_known_values(self):
        self.assertAlmostEqual(chi_square_p_value(3.841458820694124, 1), 0.05, places=4)
        self.assertAlmostEqual(chi_square_p_value(18.307038053275146, 10), 0.05, places=4)
        self.assertAlmostEqual(chi_square_p_value(0.0, 5), 1.0, places=6)

    def test_report_is_written_next_to_the_output(self):
        _, _, tmp = run(population(1000), sample_size=100, seed=3)
        data = json.loads((tmp / "clean.csv.sampling.json").read_text())
        self.assertEqual(data["sample_size"], 100)
        self.assertIn("year", data["representativeness"])


if __name__ == "__main__":
    unittest.main()


class PerDrugReservoirTest(unittest.TestCase):
    def test_every_row_of_a_drug_has_the_same_chance_of_being_kept(self):
        from pipeline.transform import build_scenario
        total, size, runs = 60, 10, 300
        tmp = Path(tempfile.mkdtemp())
        with open(tmp / "clean.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["transaction_id", "reporter_id", "buyer_id", "transaction_date", "drug_code", "quantity_grams", "dosage_unit"])
            for i in range(total):
                writer.writerow([f"T{i}", "AB1234567", f"B{i}", "2020-01-01", "12345678901", "1", "10"])
        inclusion = Counter()
        for seed in range(1, runs + 1):
            result = build_scenario(str(tmp / "clean.csv"), str(tmp / "s.csv"), sample_per_drug=size, seed=seed)
            inclusion.update(r["buyer_id"] for r in result["rows"])
        expected = runs * size / total
        statistic = sum((inclusion[f"B{i}"] - expected) ** 2 / expected for i in range(total))
        self.assertGreater(chi_square_p_value(statistic, total - 1), 0.001)
