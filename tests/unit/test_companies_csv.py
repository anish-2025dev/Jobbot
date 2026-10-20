"""data/companies.csv: the verified seed list must be loadable by `seed-companies`."""
import csv
from pathlib import Path

from app.ingest.detect import detect
from app.ingest.runner import SOURCE_OF_ATS

ROWS = list(csv.DictReader((Path(__file__).parents[2] / "data/companies.csv").open(encoding="utf-8")))


def test_size_and_uniqueness():
    assert len(ROWS) >= 500
    assert len({r["name"].casefold() for r in ROWS}) == len(ROWS)
    assert len({(r["ats"], r["slug"].lower()) for r in ROWS}) == len(ROWS)
    assert sum(r["segment"].startswith("india") for r in ROWS) >= 150


def test_every_row_points_at_a_pollable_board():
    for r in ROWS:
        assert r["ats"] in SOURCE_OF_ATS, r
        d = detect(r["careers_url"])
        assert d is not None and d.ats == r["ats"] and d.slug == r["slug"], (r, d)
        assert int(r["open_jobs"]) > 0


def test_india_and_fresher_boards_come_first():
    order = {"india-fresher": 0, "india": 1, "global-fresher": 2, "global": 3}
    segs = [order[r["segment"]] for r in ROWS]
    assert segs == sorted(segs)
