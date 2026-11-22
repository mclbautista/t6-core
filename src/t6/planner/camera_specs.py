"""Camera-specs CSV for the OCF Drive Planner Sheet (t6-core#24).

The catalogue (`camera_modes.csv`, next to this file) holds one row for each
camera mode. `camera_specs_csv` turns it into the public CSV that the Sheet
reads with IMPORTDATA. The Sheet does the storage arithmetic; this file only
carries the rates.

Public data only. The public columns are a fixed allowlist, and a catalogue
with any other column is refused, so a show, client or contact field cannot
reach the output by accident.

    python -m t6.planner.camera_specs <out-dir>

writes `camera-specs.csv` into <out-dir>. Push it with
`tools/csv_push.sh` in t6-netops.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
import sys
from collections.abc import Sequence

CATALOGUE = Path(__file__).with_name("camera_modes.csv")
OUTPUT_NAME = "camera-specs.csv"

# Catalogue columns. `bitrate_source` is for the people who keep the file; it
# is not published.
CATALOGUE_COLUMNS = (
    "camera_model",
    "resolution",
    "codec",
    "frame_rate",
    "bitrate_mbps",
    "bitrate_source",
)
KEY_PARTS = ("camera_model", "resolution", "codec", "frame_rate")
PUBLIC_COLUMNS = ("key", *KEY_PARTS, "bitrate_mbps")


def _read(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != CATALOGUE_COLUMNS:
        raise ValueError(f"catalogue columns must be exactly {list(CATALOGUE_COLUMNS)}")
    rows = []
    for number, row in enumerate(reader, start=2):
        for column in KEY_PARTS:
            if not row[column].strip():
                raise ValueError(f"line {number}: {column} is empty")
            if "|" in row[column]:
                raise ValueError(f"line {number}: {column} must not contain '|'")
        for column in ("frame_rate", "bitrate_mbps"):
            value = row[column].strip()
            if value and float(value) <= 0:
                raise ValueError(f"line {number}: {column} must be above zero")
        rows.append({column: row[column].strip() for column in CATALOGUE_COLUMNS})
    keys = [_key(row) for row in rows]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate mode: {duplicates[0]}")
    return rows


def _key(row: dict[str, str]) -> str:
    return "|".join(row[column] for column in KEY_PARTS)


def unpriced_modes(text: str) -> list[str]:
    """Keys of the modes that have no bitrate. They are left out of the CSV."""
    return [_key(row) for row in _read(text) if not row["bitrate_mbps"]]


def camera_specs_csv(text: str) -> str:
    """Render the public CSV from catalogue text. UTF-8, comma, LF, no BOM."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(PUBLIC_COLUMNS)
    for row in _read(text):
        if row["bitrate_mbps"]:
            public = {"key": _key(row), **row}
            writer.writerow([public[column] for column in PUBLIC_COLUMNS])
    return out.getvalue()


def write_camera_specs(out_dir: Path, catalogue: Path = CATALOGUE) -> list[str]:
    """Write `camera-specs.csv` into `out_dir`. Return the keys left out."""
    text = catalogue.read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / OUTPUT_NAME).write_text(camera_specs_csv(text), encoding="utf-8", newline="")
    return unpriced_modes(text)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    left_out = write_camera_specs(Path(args[0]))
    print(f"wrote {Path(args[0]) / OUTPUT_NAME}; {len(left_out)} mode(s) left out (no bitrate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
