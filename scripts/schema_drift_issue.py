#!/usr/bin/env python3
"""Print the drift in a `t6 schema-check` JSON report as Markdown; print nothing when there is none.

    python3 scripts/schema_drift_issue.py report.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from t6.notion.schema import drift_markdown


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: schema_drift_issue.py REPORT.json", file=sys.stderr)
        return 2
    sys.stdout.write(drift_markdown(json.loads(Path(argv[0]).read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
