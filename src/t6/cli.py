"""JSON command-line interface for the shared library."""

import argparse
import json
from importlib.metadata import version


def main() -> None:
    """Run a T6 command."""
    parser = argparse.ArgumentParser(prog="t6")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Print the installed version as JSON")
    parser.parse_args()
    print(json.dumps({"version": version("t6")}))
