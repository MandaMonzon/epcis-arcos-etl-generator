import csv
import gzip
import tempfile
import unittest
from pathlib import Path

from pipeline.extract import extract

HEADER = ["reporter_dea_no", "buyer_dea_no", "transaction_date", "ndc_no", "calc_base_wt_in_gm", "quantity", "order_form_no"]


def write_raw(path, rows):
    with gzip.open(path, "wt", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)


class ExtractTest(unittest.TestCase):
    def run_extract(self, rows):
        tmp = Path(tempfile.mkdtemp())
        write_raw(tmp / "raw.gz", rows)
        summary = extract(tmp / "raw.gz", tmp / "clean.csv")
        with open(tmp / "clean.csv", newline="") as f:
            return summary, list(csv.DictReader(f))

    def test_blank_order_form_rows_are_kept_with_unique_ids(self):
        rows = [["AB1234567", "CD7654321", "20200101", "12345678901", "1.5", "10", ""],
                ["AB1234567", "CD7654321", "20200101", "12345678901", "1.5", "10", ""],
                ["AB1234567", "EF1111111", "20200102", "12345678901", "1.5", "10", "OF-1"]]
        summary, kept = self.run_extract(rows)
        self.assertEqual((summary["rows_read"], summary["converted"], summary["skipped"]), (3, 3, 0))
        ids = [r["transaction_id"] for r in kept]
        self.assertEqual(len(set(ids)), 3)

    def test_rows_missing_required_fields_are_counted_with_a_reason(self):
        rows = [["AB1234567", "", "20200101", "12345678901", "1.5", "10", "OF-1"],
                ["AB1234567", "CD7654321", "bad-date", "12345678901", "1.5", "10", "OF-2"],
                ["AB1234567", "CD7654321", "20200101", "12345678901", "1.5", "10", "OF-3"]]
        summary, kept = self.run_extract(rows)
        self.assertEqual(summary["rows_read"], summary["converted"] + summary["skipped"])
        self.assertEqual(len(kept), 1)
        self.assertEqual(sum(summary["skip_reasons"].values()), summary["skipped"])


if __name__ == "__main__":
    unittest.main()
