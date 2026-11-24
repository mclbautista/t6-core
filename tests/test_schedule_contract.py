from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from t6.schedule import (
    ScheduleSnapshot,
    UnsupportedLayout,
    extract_schedule,
    parse_request,
    parse_response,
    round_trip,
)
from t6.schedule.contract import dump_response, request_json_schema, response_json_schema

FIXTURES = Path(__file__).parent / "fixtures" / "schedule"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class FixtureTests(unittest.TestCase):
    def test_request_fixture_produces_the_response_fixture(self) -> None:
        result = extract_schedule(parse_request(load("request.v1.json")))
        self.assertEqual(dump_response(result), load("response.v1.json"))

    def test_unsupported_request_fixture_produces_the_unsupported_fixture(self) -> None:
        result = extract_schedule(parse_request(load("request.unsupported.v1.json")))
        self.assertIsInstance(result, UnsupportedLayout)
        self.assertEqual(dump_response(result), load("response.unsupported.v1.json"))

    def test_response_fixtures_round_trip(self) -> None:
        for name in ("response.v1.json", "response.unsupported.v1.json"):
            parsed = parse_response(load(name))
            self.assertEqual(round_trip(parsed), parsed)
            self.assertEqual(dump_response(parsed), load(name))

    def test_response_fixture_parses_from_text(self) -> None:
        parsed = parse_response((FIXTURES / "response.v1.json").read_text())
        assert isinstance(parsed, ScheduleSnapshot)
        self.assertEqual(parsed.schema_version, "ScheduleSnapshot/v1")
        self.assertEqual(parsed.layout_version, "monthly-calendar/v1")

    def test_committed_schemas_match_the_models(self) -> None:
        self.assertEqual(load("request.schema.v1.json"), request_json_schema())
        self.assertEqual(load("response.schema.v1.json"), response_json_schema())


class ValidationTests(unittest.TestCase):
    def response(self) -> dict[str, Any]:
        data = load("response.v1.json")
        assert isinstance(data, dict)
        return data

    def rejects(self, data: dict[str, Any]) -> None:
        with self.assertRaises(ValidationError):
            parse_response(data)

    def test_other_schema_version_is_rejected(self) -> None:
        data = self.response()
        data["schema_version"] = "ScheduleSnapshot/v2"
        self.rejects(data)

    def test_unknown_field_is_rejected(self) -> None:
        data = self.response()
        data["candidates"][0]["instagantt_id"] = "x"
        self.rejects(data)

    def test_dates_must_be_real_iso_dates_in_order(self) -> None:
        for field, value in (("start", "2026-02-30"), ("due", "03/11/2026"), ("due", "2026-11-02")):
            data = self.response()
            data["candidates"][0][field] = value
            self.rejects(data)

    def test_candidate_must_name_a_known_group(self) -> None:
        data = self.response()
        data["candidates"][0]["group_key"] = "m09-xxx"
        self.rejects(data)

    def test_held_group_cannot_carry_candidates(self) -> None:
        data = self.response()
        data["groups"][0]["status"] = "held"
        data["groups"][0]["exceptions"] = [{"code": "ambiguous_year", "detail": "x"}]
        self.rejects(data)

    def test_group_status_must_match_its_exceptions(self) -> None:
        data = self.response()
        data["groups"][0]["exceptions"] = [{"code": "unread_region", "detail": "x"}]
        self.rejects(data)

    def test_request_rejects_an_unknown_schema_version(self) -> None:
        data = load("request.v1.json")
        data["schema_version"] = "ScheduleSnapshotRequest/v9"
        with self.assertRaises(ValidationError):
            parse_request(data)


if __name__ == "__main__":
    unittest.main()
