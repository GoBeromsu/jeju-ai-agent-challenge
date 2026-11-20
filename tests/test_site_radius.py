import math
import struct
import tempfile
import unittest
from pathlib import Path

from app.engine import Engine, Profile
from app.place import Place


class SiteRadiusTest(unittest.TestCase):
    def test_counts_across_dong_boundaries_and_respects_each_circle(self) -> None:
        # North/south offsets from the projection origin avoid longitude scale effects.
        z_per_m = 111_320 * 180 / (math.pi * 6_371_000)
        records = [
            (0, 0, 0, 2, 0, 0),                           # same business at the center
            (0, 299.9 * z_per_m, 0, 2, 0, 1),             # same business, adjacent dong
            (0, 300.1 * z_per_m, 0, 1, 0, 1),             # same major, just outside 300m
            (250 * z_per_m, 250 * z_per_m, 0, 2, 0, 1),   # in the square, outside the circle
            (0, 500.1 * z_per_m, 0, 2, 0, 1),
            (0, 999.9 * z_per_m, 0, 1, 0, 1),
            (0, 1000.1 * z_per_m, 0, 2, 0, 1),
            (0, 0, 0, 99, 7, 1),                          # different major at the center
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "stores.bin").write_bytes(b"".join(struct.pack("<ffHHBB", *row) for row in records))
            site = Place(Engine(), data_dir=path).site_radius(Profile(business=2), (0, 0))

        assert site is not None
        self.assertEqual(
            [(row["radiusM"], row["numerator"], row["denominator"], row["share"]) for row in site["radii"]],
            [(300, 2, 2, 100.0), (500, 3, 4, 75.0), (1000, 4, 6, 400 / 6)],
        )
