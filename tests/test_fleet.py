"""t6.fleet: Machines in Bays, matching, dry run, typed writes and the fleet rules."""

from datetime import UTC, datetime, timedelta
import unittest

from t6.domain import Machine, Ring
from t6.fleet import (
    ROLE_GROUPS,
    Fleet,
    MachineFacts,
    ManifestChainError,
    SyncResult,
    is_stale,
    manifest_chain,
    match_key,
    role_group,
)
from t6.notion.schema import load_snapshot, refresh_snapshot
from t6.notion.testing import FakeNotion, date, rich_text, select, title
from t6.notion.writes import Ambiguous, Rejected

BAYS = "ds-bays"
NOW = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)


def live_bays() -> FakeNotion:
    fake = FakeNotion()
    fake.add_snapshot_data_source(BAYS, "bays")
    return fake


def add_bay(
    fake: FakeNotion,
    name: str = "Bay 1",
    host: str | None = "Bay-1",
    serial: str | None = None,
    manifest: str | None = None,
    ring: str | None = None,
    version: str | None = None,
    checked: str | None = None,
    os: str | None = None,
) -> str:
    page = fake.add_page(
        BAYS,
        {
            "Name": title(name),
            "Hostname": rich_text(host),
            "Serial Number": rich_text(serial),
            "Munki Manifest": rich_text(manifest),
            "Ring": select(ring),
            "Munki Client Version": rich_text(version),
            "Last Check-in": date(checked),
            "OS Version": rich_text(os),
        },
    )
    return str(page["id"])


def facts(*names: str, **kw: object) -> MachineFacts:
    kw.setdefault("observed_at", NOW)
    return MachineFacts(names=names, **kw)  # type: ignore[arg-type]


def patches(fake: FakeNotion) -> int:
    return sum(1 for r in fake.requests if r.method == "PATCH")


class MatchKeyTests(unittest.TestCase):
    def test_case_spaces_and_ard_suffix_are_not_identity(self) -> None:
        for name in ("201-Bay-01-Mac-Pro-111", "201 Bay 01 Mac Pro", "201-BAY-01-MAC-PRO", " 201_bay_01_mac_pro "):
            self.assertEqual(match_key(name), "201-bay-01-mac-pro", name)

    def test_only_a_trailing_two_to_four_digit_suffix_is_dropped(self) -> None:
        self.assertEqual(match_key("Mac-mini-M4"), "mac-mini-m4")
        self.assertEqual(match_key("Sixto-5"), "sixto-5")
        self.assertEqual(match_key("Tim-M2-Max-9"), "tim-m2-max-9")
        self.assertEqual(match_key("303B iMac"), match_key("303B-iMac"))


class RuleTests(unittest.TestCase):
    def test_role_group_is_the_one_role_group_included(self) -> None:
        self.assertEqual(role_group(["toc"]), "toc")
        self.assertEqual(role_group(["all", "sound"]), "sound")
        self.assertIsNone(role_group(["all"]))
        self.assertIsNone(role_group(["toc", "sound"]))
        self.assertEqual(len(ROLE_GROUPS), 9)

    def test_manifest_chain_of_a_machine(self) -> None:
        tree = {"202-TOC": ["toc"], "toc": ["all"], "all": ["site_default"], "site_default": []}
        self.assertEqual(manifest_chain("202-TOC", tree), ("202-TOC", "toc", "all", "site_default"))

    def test_a_shared_manifest_appears_once(self) -> None:
        tree = {"m": ["a", "b"], "a": ["all"], "b": ["all"], "all": []}
        self.assertEqual(manifest_chain("m", tree), ("m", "a", "all", "b"))

    def test_a_missing_manifest_or_a_cycle_is_an_error(self) -> None:
        with self.assertRaises(ManifestChainError):
            manifest_chain("m", {"m": ["gone"]})
        with self.assertRaises(ManifestChainError):
            manifest_chain("m", {"m": ["a"], "a": ["m"]})

    def test_staleness(self) -> None:
        self.assertFalse(is_stale(NOW - timedelta(seconds=3600), NOW))
        self.assertTrue(is_stale(NOW - timedelta(seconds=3601), NOW))
        self.assertTrue(is_stale(None, NOW))
        self.assertFalse(is_stale(NOW - timedelta(seconds=90), NOW, max_age=100))

    def test_facts_are_checked(self) -> None:
        with self.assertRaises(ValueError):
            MachineFacts(names=("",))
        with self.assertRaises(ValueError):
            MachineFacts(names=("a",), ring="canary")
        with self.assertRaises(ValueError):
            MachineFacts(names=("a",), last_check_in=datetime(2026, 11, 14, 4, 0))


class ReadTests(unittest.TestCase):
    def test_bays_read_as_machines(self) -> None:
        fake = live_bays()
        add_bay(
            fake, serial="S1", manifest="Bay-1", ring="pilot", version="7.0.1", checked="2026-11-14T03:00:00+00:00", os="26.1"
        )
        with fake.client() as client:
            (machine,) = Fleet(client, BAYS).machines()
        self.assertIsInstance(machine, Machine)
        self.assertEqual(
            (machine.name, machine.hostname, machine.serial_number, machine.munki_manifest, machine.ring),
            ("Bay 1", "Bay-1", "S1", "Bay-1", Ring.PILOT),
        )
        self.assertEqual((machine.munki_client_version, machine.os_version), ("7.0.1", "26.1"))
        self.assertEqual(machine.last_check_in, datetime(2026, 11, 14, 3, 0, tzinfo=UTC))

    def test_an_unknown_ring_reads_as_its_name(self) -> None:
        fake = live_bays()
        add_bay(fake, ring="canary")
        with fake.client() as client:
            (machine,) = Fleet(client, BAYS).machines()
        self.assertEqual(machine.ring, "canary")


class SyncTests(unittest.TestCase):
    def test_dry_run_reports_changes_and_writes_nothing(self) -> None:
        fake = live_bays()
        add_bay(fake, serial="old")
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("Bay-1", serial_number="S1", ring=Ring.PILOT)], now=NOW)
        self.assertEqual(result.status, "matched")
        self.assertEqual({(c.fact, c.old, c.new) for c in result.changes}, {("serial_number", "old", "S1"), ("ring", None, Ring.PILOT)})
        self.assertIsNone(result.written)
        self.assertEqual(patches(fake), 0)

    def test_apply_writes_all_six_facts_and_returns_the_saved_machine(self) -> None:
        fake = live_bays()
        add_bay(fake)
        checked = datetime(2026, 11, 14, 3, 30, 42, tzinfo=UTC)
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync(
                [
                    facts(
                        "Bay-1",
                        serial_number="S1",
                        munki_manifest="Bay-1",
                        ring=Ring.TESTING,
                        munki_client_version="7.0.1",
                        last_check_in=checked,
                        os_version="26.1",
                    )
                ],
                apply=True,
                now=NOW,
            )
        saved = result.written
        assert isinstance(saved, Machine)
        self.assertEqual(
            (saved.serial_number, saved.munki_manifest, saved.ring, saved.munki_client_version, saved.os_version),
            ("S1", "Bay-1", Ring.TESTING, "7.0.1", "26.1"),
        )
        self.assertEqual(saved.last_check_in, checked.replace(second=0))

    def test_a_fact_that_already_holds_is_not_written(self) -> None:
        fake = live_bays()
        add_bay(fake, serial="S1", ring="pilot", checked="2026-11-14T03:30:00+00:00")
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync(
                [facts("Bay-1", serial_number="S1", ring=Ring.PILOT, last_check_in=datetime(2026, 11, 14, 3, 30, 42, tzinfo=UTC))],
                apply=True,
                now=NOW,
            )
        self.assertEqual(result.changes, ())
        self.assertEqual(patches(fake), 0)

    def test_an_unknown_fact_never_clears_a_value(self) -> None:
        fake = live_bays()
        add_bay(fake, serial="S1", manifest="keep")
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("Bay-1", serial_number=None)], apply=True, now=NOW)
            (machine,) = Fleet(client, BAYS).machines()
        self.assertEqual(result.changes, ())
        self.assertIn("serial_number", result.skipped)
        self.assertEqual((machine.serial_number, machine.munki_manifest), ("S1", "keep"))

    def test_observed_facts_from_a_stale_observation_are_not_written(self) -> None:
        fake = live_bays()
        add_bay(fake)
        old = NOW - timedelta(hours=2)
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync(
                [facts("Bay-1", serial_number="S1", munki_client_version="7.0.1", os_version="26.1", observed_at=old)],
                apply=True,
                now=NOW,
            )
            (machine,) = Fleet(client, BAYS).machines()
        self.assertEqual([c.fact for c in result.changes], ["serial_number"])
        self.assertEqual(set(result.skipped) >= {"munki_client_version", "os_version"}, True)
        self.assertEqual((machine.serial_number, machine.munki_client_version, machine.os_version), ("S1", None, None))

    def test_no_observation_time_counts_as_stale(self) -> None:
        fake = live_bays()
        add_bay(fake)
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("Bay-1", os_version="26.1", observed_at=None)], now=NOW)
        self.assertEqual(result.changes, ())

    def test_matching_ignores_case_spaces_and_ard_suffix(self) -> None:
        fake = live_bays()
        add_bay(fake, name="201 Bay 01 Mac Pro (63)", host="201-Bay-01-Mac-Pro")
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("201-bay-01-mac-pro-111", serial_number="S1")], now=NOW)
        self.assertEqual(result.status, "matched")

    def test_the_computer_name_also_matches_the_row_name(self) -> None:
        fake = live_bays()
        add_bay(fake, name="303B iMac", host=None)
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("303b-imac-77", serial_number="S1")], now=NOW)
        self.assertEqual(result.status, "matched")

    def test_unmatched_is_skipped_and_no_row_is_created(self) -> None:
        fake = live_bays()
        add_bay(fake)
        before = fake.page_count()
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("Nowhere", serial_number="S1")], apply=True, now=NOW)
        self.assertEqual(result.status, "unmatched")
        self.assertEqual(fake.page_count(), before)
        self.assertEqual(patches(fake), 0)
        self.assertEqual([r for r in fake.requests if r.method == "POST" and not r.url.path.endswith("/query")], [])

    def test_two_matching_rows_are_ambiguous(self) -> None:
        fake = live_bays()
        add_bay(fake, name="Bay 1", host="Bay-1")
        add_bay(fake, name="Bay 1 copy", host="bay-1-200")
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("Bay-1", serial_number="S1")], apply=True, now=NOW)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(patches(fake), 0)

    def test_a_row_claimed_by_two_machines_is_written_for_neither(self) -> None:
        fake = live_bays()
        add_bay(fake, name="Bay 1", host="Bay-1")
        with fake.client() as client:
            results = Fleet(client, BAYS).sync(
                [facts("Bay-1", serial_number="S1"), facts("bay-1-200", serial_number="S2")], apply=True, now=NOW
            )
        self.assertEqual([r.status for r in results], ["ambiguous", "ambiguous"])
        self.assertEqual(patches(fake), 0)

    def test_two_machines_write_their_own_rows(self) -> None:
        fake = live_bays()
        add_bay(fake, name="Bay 1", host="Bay-1")
        add_bay(fake, name="Bay 2", host="Bay-2")
        with fake.client() as client:
            results = Fleet(client, BAYS).sync(
                [facts("Bay-1", serial_number="S1"), facts("Bay-2", serial_number="S2")], apply=True, now=NOW
            )
            machines = {m.name: m.serial_number for m in Fleet(client, BAYS).machines()}
        self.assertTrue(all(isinstance(r.written, Machine) for r in results))
        self.assertEqual(machines, {"Bay 1": "S1", "Bay 2": "S2"})

    def test_a_refused_write_is_a_typed_failure(self) -> None:
        fake = live_bays()
        add_bay(fake)
        fake.fail_next(400, code="validation_error", after=1)
        with fake.client() as client:
            (result,) = Fleet(client, BAYS).sync([facts("Bay-1", serial_number="S1")], apply=True, now=NOW)
        assert isinstance(result, SyncResult)
        self.assertIsInstance(result.written, Rejected)


class RenameTests(unittest.TestCase):
    def test_a_property_and_ring_option_rename_changes_nothing(self) -> None:
        fake = live_bays()
        for ring in Ring:
            fake.add_option(BAYS, "Ring", ring.value)
        add_bay(fake, ring="testing")
        with fake.client() as client:
            snapshot = refresh_snapshot(client, load_snapshot(), {"bays": BAYS})
        fake.rename_property(BAYS, "Serial Number", "Serial")
        fake.rename_option(BAYS, "Ring", "pilot", "Pilot ring")
        with fake.client() as client:
            (result,) = Fleet(client, BAYS, snapshot).sync(
                [facts("Bay-1", serial_number="S1", ring=Ring.PILOT)], apply=True, now=NOW
            )
        saved = result.written
        assert isinstance(saved, Machine)
        self.assertEqual((saved.serial_number, saved.ring), ("S1", Ring.PILOT))
        self.assertEqual(fake.page(saved.page_id)["properties"]["Ring"]["select"]["name"], "Pilot ring")


if __name__ == "__main__":
    unittest.main()
