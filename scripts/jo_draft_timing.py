"""Time the three model calls of `jo draft` on real lines (spec #791, ticket #792).

    python scripts/jo_draft_timing.py LINES.json --config llm.json --privacy-registry privacy_registry.json

See `t6.llm.jo_draft_timing` for the file shapes. Prints numbers only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from t6.llm.config import LLMConfig, decisions_from
from t6.llm.jo_draft_timing import HAIKU_TAG, LOOKUPS_TAG, MATCH_TAG, measure, summarize
from t6.llm.transport import Provider
from t6.privacy import Registry


def _registry(path: str | None) -> Registry:
    from t6.server.verbs import _read_privacy_names

    if not path:
        raise SystemExit("give --privacy-registry or set T6_READ_PRIVACY_REGISTRY: only masked text is sent")
    return _read_privacy_names(path)


def _providers(config: Mapping[str, Any], env: Mapping[str, str]) -> tuple[Provider, Provider, Provider]:
    llm = LLMConfig.from_mapping(config["llm"])
    per_task = config.get("decisions_by_task") or {}

    def jev(tag: str) -> Provider:
        entry = per_task.get(tag, config.get("decisions"))
        if entry is None:
            raise SystemExit(f"no decisions entry for {tag}: add decisions_by_task.{tag} or decisions")
        return decisions_from(entry, env)

    return jev(MATCH_TAG), jev(LOOKUPS_TAG), llm.provider_for(HAIKU_TAG, env=env)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jo_draft_timing.py", description="Time Jev match, Jev lookups and one Haiku turn on masked lines")
    parser.add_argument("lines", help="JSON file: [{line, candidates: [{task, purpose, mode}]}]")
    parser.add_argument("--config", default=None, help="the LLM config JSON (default: $T6_LLM_CONFIG)")
    parser.add_argument("--privacy-registry", default=None, help="privacy_registry.json (default: $T6_READ_PRIVACY_REGISTRY)")
    args = parser.parse_args(argv)
    source = os.environ if env is None else env
    config_path = args.config or source.get("T6_LLM_CONFIG")
    if not config_path:
        raise SystemExit("give --config or set T6_LLM_CONFIG")
    with open(config_path, encoding="utf-8") as file:
        config = json.load(file)
    with open(args.lines, encoding="utf-8") as file:
        lines = json.load(file)
    registry = _registry(args.privacy_registry or source.get("T6_READ_PRIVACY_REGISTRY"))
    jev_match, jev_lookups, haiku = _providers(config, source)
    print(json.dumps(summarize(measure(lines, registry, jev_match=jev_match, jev_lookups=jev_lookups, haiku=haiku)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
