"""The public camera-specs CSV for the OCF Drive Planner Sheet (t6-core#24)."""
import csv
import io
import re
import tempfile
import unittest
from pathlib import Path

from t6.planner import camera_specs
from t6.planner.camera_specs import CATALOGUE, CATALOGUE_COLUMNS, PUBLIC_COLUMNS

HEADER = ",".join(CATALOGUE_COLUMNS)


def catalogue(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def sheet_tb(mbps: float, cameras: int, days: int, hours: float) -> float:
    """The Sheet's formula: Mbps x cameras x days x hours a day / 1000."""
    return mbps * cameras * days * hours / 1000


class RenderTests(unittest.TestCase):
    def test_header_is_the_allowlist_and_nothing_else(self) -> None:
        out = camera_specs.camera_specs_csv(catalogue('Cam,1920 x 1080,Codec,23.976,50,https://x'))
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(tuple(rows[0]), PUBLIC_COLUMNS)
        self.assertEqual(rows[1], ['Cam|1920 x 1080|Codec|23.976', 'Cam', '1920 x 1080', 'Codec', '23.976', '50'])
        self.assertNotIn('https://x', out)

    def test_a_catalogue_with_any_other_column_is_refused(self) -> None:
        for extra in ('client', 'show', 'contact_email'):
            with self.subTest(extra=extra):
                text = f'{HEADER},{extra}\nCam,1920 x 1080,Codec,23.976,50,src,secret\n'
                with self.assertRaises(ValueError):
                    camera_specs.camera_specs_csv(text)

    def test_commas_are_quoted_and_the_file_has_no_bom_or_cr(self) -> None:
        out = camera_specs.camera_specs_csv(catalogue('Cam,1920 x 1080,"XAVC S-I (All-Intra, H.264)",23.976,90,s'))
        self.assertFalse(out.startswith('﻿'))
        self.assertNotIn('\r', out)
        self.assertTrue(out.endswith('\n'))
        parsed = list(csv.reader(io.StringIO(out)))
        self.assertEqual({len(row) for row in parsed}, {len(PUBLIC_COLUMNS)})
        self.assertEqual(parsed[1][3], 'XAVC S-I (All-Intra, H.264)')

    def test_modes_without_a_bitrate_are_left_out_and_reported(self) -> None:
        text = catalogue('A,1 x 1,C,23.976,10,s', 'B,1 x 1,C,23.976,,')
        self.assertNotIn('B|', camera_specs.camera_specs_csv(text))
        self.assertEqual(camera_specs.unpriced_modes(text), ['B|1 x 1|C|23.976'])

    def test_bad_rows_are_refused(self) -> None:
        for bad in (
            'A,1 x 1,C,23.976,10,s\nA,1 x 1,C,23.976,12,s',  # duplicate mode
            'A,,C,23.976,10,s',  # empty part of the key
            'A|B,1 x 1,C,23.976,10,s',  # the key separator
            'A,1 x 1,C,23.976,-5,s',  # not above zero
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    camera_specs.camera_specs_csv(f'{HEADER}\n{bad}\n')

    def test_the_output_file_name_is_one_the_vps_edge_serves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            camera_specs.write_camera_specs(Path(tmp))
            files = [p.name for p in Path(tmp).iterdir()]
        self.assertEqual(files, ['camera-specs.csv'])
        self.assertRegex(files[0], r'^[A-Za-z0-9][A-Za-z0-9._-]*\.csv$')


class RealCatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        text = CATALOGUE.read_text(encoding='utf-8')
        self.public = {
            row['key']: row for row in csv.DictReader(io.StringIO(camera_specs.camera_specs_csv(text)))
        }

    def test_the_sheets_reference_ocf_rows_give_the_same_totals(self) -> None:
        mode = 'ARRI Alexa 35|4608 x 3164 (Open Gate)|ARRIRAW (Open Gate)|23.976'
        mbps = float(self.public[mode]['bitrate_mbps'])
        first = sheet_tb(mbps, 3, 11, 2)
        second = sheet_tb(mbps, 2, 4, 2)
        self.assertEqual(f'{first:.2f}', '302.48')
        self.assertEqual(f'{second:.2f}', '73.33')
        self.assertEqual(f'{first + second:.2f}', '375.81')

    def test_every_published_row_has_a_positive_bitrate_and_a_unique_key(self) -> None:
        text = CATALOGUE.read_text(encoding='utf-8')
        rows = list(csv.DictReader(io.StringIO(camera_specs.camera_specs_csv(text))))
        self.assertEqual(len(rows), len({row['key'] for row in rows}))
        self.assertTrue(all(float(row['bitrate_mbps']) > 0 for row in rows))
        self.assertTrue(all(row['frame_rate'] == '23.976' for row in rows))

    def test_no_row_reads_like_a_show_client_or_contact(self) -> None:
        out = camera_specs.camera_specs_csv(CATALOGUE.read_text(encoding='utf-8'))
        self.assertIsNone(re.search(r'@|https?://|\+\d{6,}', out))


if __name__ == '__main__':
    unittest.main()
