"""Tests for the SQLite WAL Notion mirror (#516) and batched project_resolve."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing, redirect_stderr
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from t6.cli import DataSourceConfig, _read_data_sources, read_data_source_configs
from t6.domain import Project
from t6.notion import (
    MirrorReader,
    MirrorSyncer,
    MirrorWriter,
    NotionSource,
    last_read_from_mirror,
    mirror_db_path,
)
from t6.notion.generated import ContactsRow, DeliverablesRow, JobOrdersRow, ProjectsRow
from t6.notion.reads import ContactReader, DeliverableReader, ProjectReader
from t6.notion.testing import FakeNotion, checkbox, date, multi_select, number, rich_text, select, title, relation
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import ProjectRefs, Registered, Rotation, SqliteProjectRegistry
from t6.registry.core import normalize_page_id
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import (
    CameraEnrichInput,
    DeliverableCandidatesInput,
    Done,
    JoFindInput,
    JoGetInput,
    JoListInput,
    MachineEditInput,
    MachineEditItem,
    MachineFactsInput,
    MachineSyncInput,
    MeetingNotesAppendInput,
    MeetingNotesCreateInput,
    MeetingNotesMarkSentInput,
    MeetingNotesTrashInput,
    PersonCreateInput,
    PersonFindInput,
    PersonListInput,
    PersonTelegramInput,
    Preview,
    ProjectFlagsInput,
    ProjectGetInput,
    ProjectListInput,
    ProjectNotesInput,
    ProjectResolveInput,
    Refused,
)

PROJECTS_DS = "ds-projects-0001"
CONTACTS_DS = "ds-contacts-0002"
JOB_ORDERS_DS = "ds-job-orders-0003"
DELIVERABLES_DS = "ds-deliverables-0004"
BAYS_DS = "ds-bays-0005"
CAMERAS_DS = "ds-cameras-0006"
MEETING_NOTES_DS = "ds-meeting-notes-0007"


class StepClock:
    """Deterministic fake clock supporting wall time, monotonic time, and sleep."""

    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _make_fake(clock: Callable[[], float] | None = None) -> FakeNotion:
    fake = FakeNotion(clock=clock)
    fake.add_snapshot_data_source(PROJECTS_DS, "projects")
    fake.add_snapshot_data_source(CONTACTS_DS, "contacts")
    fake.add_snapshot_data_source(JOB_ORDERS_DS, "job_orders")
    return fake


def _count_notion_http(fake: FakeNotion) -> int:
    return len(fake.requests)


class MirrorConfigTests(unittest.TestCase):
    def test_mirror_db_path_disabled_and_unset_never_returns_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "mirror.sqlite"
            self.assertIsNone(mirror_db_path({}))
            self.assertIsNone(mirror_db_path({"T6_NOTION_MIRROR": "1"}))
            self.assertIsNone(mirror_db_path({"T6_NOTION_MIRROR_DB": "   "}))
            for off in ("0", "false", "FALSE", "off", "no", " No "):
                with self.subTest(off=off):
                    self.assertIsNone(
                        mirror_db_path({"T6_NOTION_MIRROR": off, "T6_NOTION_MIRROR_DB": str(db_path)})
                    )
            for on in ("", "1", "true", "on", "yes", "YES"):
                with self.subTest(on=on):
                    env = {"T6_NOTION_MIRROR_DB": str(db_path)}
                    if on:
                        env["T6_NOTION_MIRROR"] = on
                    self.assertEqual(mirror_db_path(env), db_path)
            self.assertFalse(db_path.exists())

    def test_mirror_db_path_rejects_invalid_toggle(self) -> None:
        with self.assertRaises(ValueError):
            mirror_db_path({"T6_NOTION_MIRROR": "maybe", "T6_NOTION_MIRROR_DB": "/tmp/m.sqlite"})

    def test_read_data_source_configs_supports_strings_and_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "data-sources.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "projects": PROJECTS_DS,
                        "contacts": {"id": CONTACTS_DS, "max_age_seconds": 120},
                    }
                ),
                encoding="utf-8",
            )
            configs = read_data_source_configs(cfg_path)
            self.assertEqual(
                configs,
                {
                    "projects": DataSourceConfig(id=PROJECTS_DS, max_age_seconds=300.0),
                    "contacts": DataSourceConfig(id=CONTACTS_DS, max_age_seconds=120.0),
                },
            )
            self.assertEqual(_read_data_sources(cfg_path), {"projects": PROJECTS_DS, "contacts": CONTACTS_DS})

            for bad in (
                {"projects": 123},
                {"projects": {"max_age_seconds": 60}},
                {"projects": {"id": PROJECTS_DS, "max_age_seconds": -5}},
                {"projects": {"id": PROJECTS_DS, "max_age_seconds": True}},
                {"projects": {"id": PROJECTS_DS, "extra": 1}},
            ):
                cfg_path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_data_source_configs(cfg_path)


class FakeNotionTimestampTests(unittest.TestCase):
    def test_last_edited_time_filter_and_update_helpers(self) -> None:
        clock = StepClock(1_760_000_000.0)
        fake = _make_fake(clock=clock)
        p1 = fake.add_page(PROJECTS_DS, {ProjectsRow.notion_name("name"): title("Show Alpha")})
        clock.advance(100.0)
        p2 = fake.add_page(PROJECTS_DS, {ProjectsRow.notion_name("name"): title("Show Beta")})
        cutoff = datetime.fromtimestamp(1_760_000_050.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        with fake.client() as client:
            recent = client.query_data_source(
                PROJECTS_DS,
                filter={"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": cutoff}},
            )
        self.assertEqual([r["id"] for r in recent], [p2["id"]])
        clock.advance(100.0)
        fake.update_page(p1["id"], {ProjectsRow.notion_name("name"): title("Show Alpha v2")})
        p1_edited = str(fake.page(p1["id"])["last_edited_time"])
        with fake.client() as client:
            recent2 = client.query_data_source(
                PROJECTS_DS,
                filter={"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": cutoff}},
            )
            exact = client.query_data_source(
                PROJECTS_DS,
                filter={"timestamp": "last_edited_time", "last_edited_time": {"equals": p1_edited}},
            )
            before_only = client.query_data_source(
                PROJECTS_DS,
                filter={"timestamp": "last_edited_time", "last_edited_time": {"before": p1_edited}},
            )
            after_only = client.query_data_source(
                PROJECTS_DS,
                filter={"timestamp": "last_edited_time", "last_edited_time": {"after": cutoff}},
            )
            on_or_before = client.query_data_source(
                PROJECTS_DS,
                filter={"timestamp": "last_edited_time", "last_edited_time": {"on_or_before": p1_edited}},
            )
        self.assertEqual({r["id"] for r in recent2}, {p1["id"], p2["id"]})
        self.assertEqual([r["id"] for r in exact], [p1["id"]])
        self.assertEqual([r["id"] for r in before_only], [p2["id"]])
        self.assertEqual({r["id"] for r in after_only}, {p1["id"], p2["id"]})
        self.assertEqual({r["id"] for r in on_or_before}, {p1["id"], p2["id"]})
        fake.delete_page(p2["id"])
        with fake.client() as client:
            remaining = client.query_data_source(PROJECTS_DS)
        self.assertEqual([r["id"] for r in remaining], [p1["id"]])


class MirrorSyncAndReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "mirror.sqlite"
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        self.p1 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Project Alpha"),
                ProjectsRow.notion_name("project_code"): select("ALP"),
            },
        )
        self.c1 = self.fake.add_page(
            CONTACTS_DS,
            {
                ContactsRow.notion_name("name"): title("Ana Cruz"),
                ContactsRow.notion_name("telegram_handle"): rich_text("@ana"),
            },
        )
        self.j1 = self.fake.add_page(
            JOB_ORDERS_DS,
            {
                JobOrdersRow.notion_name("name"): title("Cut 1"),
                JobOrdersRow.notion_name("telegram_key"): rich_text("-100:1"),
            },
        )

    def _sources(self) -> dict[str, str]:
        return {"projects": PROJECTS_DS, "contacts": CONTACTS_DS, "job_orders": JOB_ORDERS_DS, "bays": BAYS_DS}

    def test_permissions_0600_and_wal_mode(self) -> None:
        writer = MirrorWriter(self.db_path)
        self.addCleanup(writer.close)
        self.assertTrue(self.db_path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)
        with closing(sqlite3.connect(self.db_path)) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual(mode, ("wal",))
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            self._sources(),
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.tick()
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)
        for suffix in ("-wal", "-shm"):
            side = Path(f"{self.db_path}{suffix}")
            self.assertTrue(side.exists())
            self.assertEqual(stat.S_IMODE(os.stat(side).st_mode), 0o600)

    def test_poll_and_rescan_sync_new_edited_archived_and_deleted_restored(self) -> None:
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        writer = MirrorWriter(self.db_path)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            self._sources(),
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        # First tick rescans all configured sources generically (#519).
        syncer.tick()
        self.assertIsNotNone(writer.state(PROJECTS_DS))
        self.assertIsNotNone(writer.state(CONTACTS_DS))
        self.assertIsNotNone(writer.state(JOB_ORDERS_DS))
        self.assertIsNotNone(writer.state(BAYS_DS))

        reader = MirrorReader(
            self.db_path,
            clock=self.clock,
            enabled_data_sources=[PROJECTS_DS, CONTACTS_DS],
        )
        source = NotionSource(self.fake.client, mirror=reader)
        before_http = _count_notion_http(self.fake)
        projects = ProjectReader(source, PROJECTS_DS).list()
        contacts = ContactReader(source, CONTACTS_DS).list()
        self.assertEqual([p.name for p in projects], ["Project Alpha"])
        self.assertEqual([c.name for c in contacts], ["Ana Cruz"])
        self.assertEqual(_count_notion_http(self.fake), before_http)

        # New and edited Project + Contact appear after incremental poll (60s).
        self.clock.advance(65.0)
        p2 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Project Beta"),
                ProjectsRow.notion_name("project_code"): select("BET"),
            },
        )
        self.fake.update_page(
            self.c1["id"],
            {ContactsRow.notion_name("name"): title("Ana Cruz-Santos")},
        )
        syncer.tick()
        before_http = _count_notion_http(self.fake)
        self.assertEqual(
            {p.name for p in ProjectReader(source, PROJECTS_DS).list()},
            {"Project Alpha", "Project Beta"},
        )
        self.assertEqual(
            {c.name for c in ContactReader(source, CONTACTS_DS).list()},
            {"Ana Cruz-Santos"},
        )
        self.assertEqual(_count_notion_http(self.fake), before_http)

        # Archived Project disappears after poll; unarchived restores after poll.
        self.clock.advance(65.0)
        self.fake.update_page(p2["id"], archived=True)
        syncer.tick()
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list()], ["Project Alpha"])
        self.assertIsNone(reader.retrieve_page(p2["id"]))

        self.clock.advance(65.0)
        self.fake.update_page(p2["id"], archived=False)
        syncer.tick()
        self.assertEqual(
            {p.name for p in ProjectReader(source, PROJECTS_DS).list()},
            {"Project Alpha", "Project Beta"},
        )

        # Hard-deleted page disappears after full rescan (staggered across 4 sources: projects is at +3150s).
        self.fake.delete_page(p2["id"])
        self.clock.advance(3200.0)
        syncer.tick()
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list()], ["Project Alpha"])
        self.assertIsNone(reader.retrieve_page(p2["id"]))

        # Re-added/restored page reappears on next poll/rescan.
        self.clock.advance(65.0)
        self.fake.add_page(
            PROJECTS_DS,
            {ProjectsRow.notion_name("name"): title("Project Beta Restored")},
            page_id=p2["id"],
        )
        syncer.tick()
        self.assertEqual(
            {p.name for p in ProjectReader(source, PROJECTS_DS).list()},
            {"Project Alpha", "Project Beta Restored"},
        )

    def test_pacing_keeps_requests_below_3_per_second_and_staggers_rescans(self) -> None:
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        writer = MirrorWriter(self.db_path)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            self._sources(),
            min_request_interval=0.4,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        req_times: list[float] = []
        orig_query = self.fake.transport

        class TimedSource(NotionSource):
            def query_data_source(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
                req_times.append(self_clock())
                return super().query_data_source(*args, **kwargs)

            def retrieve_data_source(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                req_times.append(self_clock())
                return super().retrieve_data_source(*args, **kwargs)

        self_clock = self.clock
        syncer = MirrorSyncer(
            writer,
            TimedSource(self.fake.client),
            self._sources(),
            min_request_interval=0.4,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.tick()
        self.assertGreaterEqual(len(req_times), 4)
        for a, b in zip(req_times, req_times[1:]):
            self.assertGreaterEqual(b - a, 0.4 - 1e-9)
        self.assertIsNotNone(orig_query)

        # Sorted deterministically by source_key: bays (0s), contacts (450s), job_orders (900s), projects (1350s).
        # At t=1805s, only bays rescans while the others wait for their staggered rescan windows.
        req_times.clear()
        self.clock.advance(1805.0)
        syncer.tick()
        bays_state = writer.state(BAYS_DS)
        proj_state = writer.state(PROJECTS_DS)
        cont_state = writer.state(CONTACTS_DS)
        assert bays_state is not None and proj_state is not None and cont_state is not None
        self.assertGreater(bays_state.last_full_rescan_at or 0.0, proj_state.last_full_rescan_at or 0.0)
        self.assertGreater(proj_state.last_full_rescan_at or 0.0, cont_state.last_full_rescan_at or 0.0)

    def test_fallbacks_not_ready_stale_and_live_bypass(self) -> None:
        # 1. Not ready -> live fallback
        writer = MirrorWriter(self.db_path)
        self.addCleanup(writer.close)
        reader = MirrorReader(self.db_path, max_age_seconds=300.0, clock=self.clock)
        source = NotionSource(self.fake.client, mirror=reader)
        before = _count_notion_http(self.fake)
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list()], ["Project Alpha"])
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        # Sync so mirror becomes ready
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            self._sources(),
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.tick()

        # Ready & fresh -> mirror hit
        before = _count_notion_http(self.fake)
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list()], ["Project Alpha"])
        self.assertTrue(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake), before)

        # freshness="live" bypasses mirror completely even when ready
        before = _count_notion_http(self.fake)
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list(freshness="live")], ["Project Alpha"])
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        # 2. Stale (> max_age_seconds) -> live fallback
        self.clock.advance(301.0)
        before = _count_notion_http(self.fake)
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list()], ["Project Alpha"])
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

    def test_self_healing_rebuild_on_empty_and_corrupt_files_while_reads_fall_back(self) -> None:
        writer = MirrorWriter(self.db_path)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            self._sources(),
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.tick()
        reader = MirrorReader(self.db_path, clock=self.clock)
        source = NotionSource(self.fake.client, mirror=reader)

        # Empty (0-byte) file: reads fall back to live; writer rebuilds cleanly.
        self.db_path.write_bytes(b"")
        before = _count_notion_http(self.fake)
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list()], ["Project Alpha"])
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        syncer.tick()
        before = _count_notion_http(self.fake)
        self.assertEqual([p.name for p in ProjectReader(source, PROJECTS_DS).list()], ["Project Alpha"])
        self.assertTrue(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake), before)

        # Corrupted file: reads during corruption fall back to live; writer rebuilds DB + -wal/-shm.
        self.db_path.write_bytes(b"not a valid sqlite database at all" * 20)
        before = _count_notion_http(self.fake)
        self.assertEqual([c.name for c in ContactReader(source, CONTACTS_DS).list()], ["Ana Cruz"])
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        syncer.tick()
        before = _count_notion_http(self.fake)
        self.assertEqual([c.name for c in ContactReader(source, CONTACTS_DS).list()], ["Ana Cruz"])
        self.assertTrue(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake), before)
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)

    def test_worker_and_off_switch_do_not_create_mirror_file(self) -> None:
        from t6.server.__main__ import mirror_reader_from_env
        from t6.worker.__main__ import mirror_syncer_from_env

        never_path = Path(self.tmp.name) / "never-created.sqlite"
        self.assertIsNone(mirror_syncer_from_env({"T6_NOTION_TOKEN": "t"}))
        self.assertIsNone(
            mirror_syncer_from_env(
                {
                    "T6_NOTION_TOKEN": "t",
                    "T6_NOTION_MIRROR": "0",
                    "T6_NOTION_MIRROR_DB": str(never_path),
                    "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS_DS,
                }
            )
        )
        self.assertIsNone(
            mirror_reader_from_env(
                {"T6_NOTION_MIRROR": "0", "T6_NOTION_MIRROR_DB": str(never_path)},
                {"projects": DataSourceConfig(id=PROJECTS_DS)},
            )
        )
        self.assertIsNone(
            mirror_reader_from_env({}, {"projects": DataSourceConfig(id=PROJECTS_DS)})
        )
        self.assertFalse(never_path.exists())

        # Also verify MirrorReader returns None cleanly when file does not exist and never creates it
        reader = MirrorReader(never_path, clock=self.clock)
        self.assertIsNone(reader.query_data_source(PROJECTS_DS))
        self.assertIsNone(reader.retrieve_page(self.p1["id"]))
        self.assertFalse(never_path.exists())

    def test_server_mirror_reader_respects_per_source_max_age(self) -> None:
        from t6.server.__main__ import mirror_reader_from_env

        writer = MirrorWriter(self.db_path, clock=self.clock)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            self._sources(),
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.tick()
        reader = mirror_reader_from_env(
            {"T6_NOTION_MIRROR_DB": str(self.db_path)},
            {
                "projects": DataSourceConfig(id=PROJECTS_DS, max_age_seconds=60.0),
                "contacts": DataSourceConfig(id=CONTACTS_DS, max_age_seconds=300.0),
            },
        )
        assert reader is not None
        reader._clock = self.clock
        self.clock.advance(90.0)
        self.assertIsNone(reader.query_data_source(PROJECTS_DS))
        self.assertIsNotNone(reader.query_data_source(CONTACTS_DS))

    def test_readonly_directory_mirror_reads_succeed_while_writer_active_and_fallback_after_close(self) -> None:
        mirror_dir = Path(self.tmp.name) / "ro_mirror"
        mirror_dir.mkdir(mode=0o700)
        db_path = mirror_dir / "mirror.sqlite"
        writer = MirrorWriter(db_path, clock=self.clock)
        self.addCleanup(writer.close)

        source_meta = {"object": "data_source", "id": PROJECTS_DS, "title": [{"plain_text": "Projects"}]}
        writer.replace_data_source(
            PROJECTS_DS,
            [self.p1],
            synced_at=self.clock(),
            source_key="projects",
            source_meta=source_meta,
        )
        reader = MirrorReader(db_path, clock=self.clock)
        source = NotionSource(self.fake.client, mirror=reader)

        os.chmod(mirror_dir, 0o555)
        try:
            self.assertTrue(Path(f"{db_path}-wal").exists())
            self.assertTrue(Path(f"{db_path}-shm").exists())

            rows = reader.query_data_source(PROJECTS_DS)
            self.assertIsNotNone(rows)
            assert rows is not None
            self.assertEqual([r["id"] for r in rows], [self.p1["id"]])

            page = reader.retrieve_page(self.p1["id"])
            self.assertIsNotNone(page)
            assert page is not None
            self.assertEqual(page["id"], self.p1["id"])

            meta = reader.retrieve_data_source(PROJECTS_DS)
            self.assertEqual(meta, source_meta)

            # Repeated MirrorWriter operations while active keep WAL/SHM alive and readable in 0o555 dir
            self.clock.advance(5.0)
            p2 = self.fake.add_page(
                PROJECTS_DS,
                {
                    ProjectsRow.notion_name("name"): title("Project Beta"),
                    ProjectsRow.notion_name("project_code"): select("BET"),
                },
            )
            self.assertEqual(
                writer.upsert_page(p2, active_data_source_ids=(PROJECTS_DS,), synced_at=self.clock()),
                "upserted",
            )
            rows_after_upsert = reader.query_data_source(PROJECTS_DS)
            self.assertIsNotNone(rows_after_upsert)
            assert rows_after_upsert is not None
            self.assertEqual({r["id"] for r in rows_after_upsert}, {self.p1["id"], p2["id"]})
            self.assertIsNotNone(reader.retrieve_page(p2["id"]))

            self.clock.advance(5.0)
            self.assertEqual(
                writer.replace_data_source(
                    PROJECTS_DS,
                    [self.p1, p2],
                    synced_at=self.clock(),
                    source_key="projects",
                    source_meta=source_meta,
                ),
                2,
            )
            self.assertEqual(len(reader.query_data_source(PROJECTS_DS) or []), 2)
            self.assertEqual(reader.retrieve_data_source(PROJECTS_DS), source_meta)
        finally:
            os.chmod(mirror_dir, 0o700)

        # Closing MirrorWriter unlinks -wal/-shm; on a 0o555 dir MirrorReader safely returns None (live fallback)
        writer.close()
        self.assertFalse(Path(f"{db_path}-wal").exists())
        self.assertFalse(Path(f"{db_path}-shm").exists())

        os.chmod(mirror_dir, 0o555)
        try:
            self.assertIsNone(reader.query_data_source(PROJECTS_DS))
            self.assertIsNone(reader.retrieve_page(self.p1["id"]))
            self.assertIsNone(reader.retrieve_data_source(PROJECTS_DS))

            before_http = _count_notion_http(self.fake)
            self.assertEqual(len(ProjectReader(source, PROJECTS_DS).list()), 2)
            self.assertFalse(last_read_from_mirror())
            self.assertEqual(_count_notion_http(self.fake) - before_http, 1)
        finally:
            os.chmod(mirror_dir, 0o700)

    def test_replace_data_source_deduplicates_duplicate_page_ids(self) -> None:
        with MirrorWriter(self.db_path, clock=self.clock) as writer:
            p1_v2 = {**self.p1, "last_edited_time": "2026-11-21T00:00:00Z"}
            count = writer.replace_data_source(
                PROJECTS_DS,
                [self.p1, p1_v2],
                synced_at=self.clock(),
                source_key="projects",
            )
            self.assertEqual(count, 1)
            state = writer.state(PROJECTS_DS)
            assert state is not None
            self.assertEqual(state.row_count, 1)


class VerbMirrorAndBatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        self.registry = SqliteProjectRegistry(Path(self.tmp.name) / "registry.sqlite", clock=self.clock)
        self.mirror_path = Path(self.tmp.name) / "mirror.sqlite"

        self.p1 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Project Alpha"),
                ProjectsRow.notion_name("project_code"): select("ALP"),
            },
        )
        self.c1 = self.fake.add_page(
            CONTACTS_DS,
            {
                ContactsRow.notion_name("name"): title("Ana Cruz"),
                ContactsRow.notion_name("telegram_handle"): rich_text("@ana"),
            },
        )
        self.jo1 = self.fake.add_page(
            JOB_ORDERS_DS,
            {
                JobOrdersRow.notion_name("name"): title("Cut 1"),
                JobOrdersRow.notion_name("telegram_key"): rich_text("-100:1"),
            },
        )
        reg = self.registry.register(
            actor="setup", idempotency_key="p1", notion_page_id=self.p1["id"], code="ALP", rotation=Rotation.IN
        )
        assert isinstance(reg, Registered)
        self.p1_t6_id = reg.project.id

        writer = MirrorWriter(self.mirror_path)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            {"projects": PROJECTS_DS, "contacts": CONTACTS_DS, "job_orders": JOB_ORDERS_DS},
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.tick()

        reader = MirrorReader(
            self.mirror_path,
            clock=self.clock,
            enabled_data_sources=[PROJECTS_DS, CONTACTS_DS],
        )
        self.api = VerbAPI(
            People(self.registry, PreviewTokens("key")),
            JobQueue(Path(self.tmp.name) / "queue.sqlite"),
            {"secret": "watchdog"},
            lambda _: self.fake.client(),
            data_sources={"projects": PROJECTS_DS, "contacts": CONTACTS_DS, "job_orders": JOB_ORDERS_DS},
            mirror=reader,
        )
        env = patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake"})
        env.start()
        self.addCleanup(env.stop)

    def test_retry_once_on_miss_and_unmirrored_job_orders_stay_live(self) -> None:
        # Mirror hits: 0 live Notion calls
        before = _count_notion_http(self.fake)
        res_p = self.api._typed_read("project", "resolve", ProjectResolveInput(text="ALP"))
        res_c = self.api._typed_read("person", "find", PersonFindInput(mention="Ana Cruz"))
        res_g = self.api._typed_read("project", "get", ProjectGetInput(project=self.p1_t6_id))
        self.assertIsInstance(res_p, Done)
        self.assertIsInstance(res_c, Done)
        self.assertIsInstance(res_g, Done)
        self.assertEqual(_count_notion_http(self.fake), before)

        # job_orders stays live in #516 even when mirror is active
        before = _count_notion_http(self.fake)
        res_jo = self.api._typed_read("jo", "find", JoFindInput(telegram_key="-100:1"))
        self.assertIsInstance(res_jo, Done)
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        # Newly created Project / Contact not yet in mirror -> retries live ONCE and finds it!
        self.clock.advance(10.0)
        p_new = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Project Gamma"),
                ProjectsRow.notion_name("project_code"): select("GAM"),
            },
        )
        c_new = self.fake.add_page(
            CONTACTS_DS,
            {ContactsRow.notion_name("name"): title("Ben Lim")},
        )

        before = _count_notion_http(self.fake)
        resolved_new = self.api._typed_read("project", "resolve", ProjectResolveInput(text="GAM"))
        self.assertIsInstance(resolved_new, Done)
        assert isinstance(resolved_new, Done)
        self.assertEqual(resolved_new.result["match"]["page_id"], p_new["id"])
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        before = _count_notion_http(self.fake)
        got_new = self.api._typed_read("project", "get", ProjectGetInput(project=p_new["id"]))
        self.assertIsInstance(got_new, Done)
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        before = _count_notion_http(self.fake)
        notes_new = self.api._typed_read("project", "notes", ProjectNotesInput(project=p_new["id"]))
        self.assertIsInstance(notes_new, Done)
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        before = _count_notion_http(self.fake)
        flags_new = self.api._typed_read("project", "flags", ProjectFlagsInput(project=p_new["id"]))
        self.assertIsInstance(flags_new, Done)
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        before = _count_notion_http(self.fake)
        found_new = self.api._typed_read("person", "find", PersonFindInput(mention="Ben Lim"))
        self.assertIsInstance(found_new, Done)
        assert isinstance(found_new, Done)
        self.assertEqual(found_new.result["match"]["page_id"], c_new["id"])
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        # True miss retries live ONCE (not twice!)
        before = _count_notion_http(self.fake)
        missing = self.api._typed_read("project", "resolve", ProjectResolveInput(text="NONEXISTENT"))
        self.assertIsInstance(missing, Done)
        assert isinstance(missing, Done)
        self.assertIsNone(missing.result["match"])
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        # Direct ProjectReader.get / ContactReader.get hit mirror when present, fall back live when missing
        reader = MirrorReader(self.mirror_path, clock=self.clock, enabled_data_sources=[PROJECTS_DS, CONTACTS_DS])
        source = NotionSource(self.fake.client, mirror=reader)
        before = _count_notion_http(self.fake)
        self.assertEqual(ProjectReader(source, PROJECTS_DS).get(self.p1["id"]).name, "Project Alpha")
        self.assertEqual(ContactReader(source, CONTACTS_DS).get(self.c1["id"]).name, "Ana Cruz")
        self.assertEqual(_count_notion_http(self.fake), before)

        before = _count_notion_http(self.fake)
        self.assertEqual(ProjectReader(source, PROJECTS_DS).get(p_new["id"]).name, "Project Gamma")
        self.assertEqual(ContactReader(source, CONTACTS_DS).get(c_new["id"]).name, "Ben Lim")
        self.assertEqual(_count_notion_http(self.fake) - before, 2)

    def test_batched_project_resolve_preserves_blank_page_id_error(self) -> None:
        self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Blank Rel"),
                ProjectsRow.notion_name("editors"): relation("   "),
            },
        )
        with MirrorWriter(self.mirror_path) as blank_writer:
            MirrorSyncer(
                blank_writer,
                NotionSource(self.fake.client),
                {"projects": PROJECTS_DS, "contacts": CONTACTS_DS},
                clock=self.clock,
                wall_clock=self.clock,
                sleep=self.clock.sleep,
            ).rescan_source("projects")
        res = self.api._typed_read("project", "resolve", ProjectResolveInput(text="Blank Rel"))
        self.assertIsInstance(res, Refused)
        assert isinstance(res, Refused)
        self.assertEqual(res.rule, "read")

    def test_batched_project_resolve_exact_parity_with_unbatched_semantics(self) -> None:
        # Include: bound project with code=None overriding notion code, unbound project, legacy binding,
        # bound and unbound role persons, and multiple project_person_roles.
        c_bound = self.fake.add_page(CONTACTS_DS, {ContactsRow.notion_name("name"): title("Editor One")})
        c_unbound = self.fake.add_page(CONTACTS_DS, {ContactsRow.notion_name("name"): title("Editor Two")})
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_ed1', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute(
                "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, 't6n_ed1', 1.0, 1)",
                (normalize_page_id(c_bound["id"]),),
            )
            # Also add a legacy inactive binding on another contact page
            c_legacy = self.fake.add_page(CONTACTS_DS, {ContactsRow.notion_name("name"): title("Editor Legacy")})
            db.execute(
                "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, 't6n_ed1', 0.5, 0)",
                (normalize_page_id(c_legacy["id"]),),
            )

        p_coded_none = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Override"),
                ProjectsRow.notion_name("project_code"): select("NOTION_CODE"),
                ProjectsRow.notion_name("editors"): relation(c_bound["id"], c_unbound["id"]),
                ProjectsRow.notion_name("colorist"): relation(c_legacy["id"]),
            },
        )
        reg_override = self.registry.register(
            actor="setup", idempotency_key="ov", notion_page_id=p_coded_none["id"], code=None, rotation=Rotation.IN
        )
        assert isinstance(reg_override, Registered)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_coord', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute(
                "INSERT INTO project_person_roles VALUES (?, 't6n_coord', 'coordinator'), (?, 't6n_ed1', 'editor')",
                (reg_override.project.id, reg_override.project.id),
            )

        self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Unbound"),
                ProjectsRow.notion_name("project_code"): select("UNB"),
                ProjectsRow.notion_name("director"): relation(c_bound["id"]),
            },
        )

        # Compute unbatched reference directly using ProjectRefs + bound_entity + per-project query
        raw = ProjectReader(NotionSource(self.fake.client), PROJECTS_DS).list()
        refs = ProjectRefs(self.registry)

        def unbatched(project: Project) -> Project:
            identified = refs.identified(project)

            def ids(pages: tuple[str, ...]) -> tuple[str, ...]:
                return tuple(self.registry.bound_entity("notion", p) or p for p in pages)

            with self.registry._transaction() as db:
                roles = tuple(
                    {"person_id": r[0], "role": r[1]}
                    for r in db.execute(
                        "SELECT person_id, role FROM project_person_roles WHERE project_id = ? ORDER BY person_id, role",
                        (identified.t6_id,),
                    )
                )
            return replace(
                identified,
                project_roles=roles,
                editor_ids=ids(identified.editor_ids),
                assistant_editor_ids=ids(identified.assistant_editor_ids),
                online_editor_ids=ids(identified.online_editor_ids),
                colorist_ids=ids(identified.colorist_ids),
                sound_tech_ids=ids(identified.sound_tech_ids),
                post_coordinator_ids=ids(identified.post_coordinator_ids),
                director_ids=ids(identified.director_ids),
                cinematographer_ids=ids(identified.cinematographer_ids),
                prod_coor_supe_ids=ids(identified.prod_coor_supe_ids),
            )

        expected = {p.page_id: unbatched(p) for p in raw}

        # Resync mirror and compare VerbAPI output
        with MirrorWriter(self.mirror_path) as blank_writer:
            MirrorSyncer(
                blank_writer,
                NotionSource(self.fake.client),
                {"projects": PROJECTS_DS, "contacts": CONTACTS_DS},
                clock=self.clock,
                wall_clock=self.clock,
                sleep=self.clock.sleep,
            ).rescan_source("projects")

        listed = self.api._typed_read("project", "list", ProjectListInput())
        assert isinstance(listed, Done)
        for item in listed.result["projects"]:
            exp = expected[item.page_id]
            self.assertEqual(item.t6_id, exp.t6_id)
            self.assertEqual(item.rotation, exp.rotation)
            self.assertEqual(item.project_code, exp.project_code)
            self.assertEqual(item.notion_project_code, exp.notion_project_code)
            self.assertEqual(item.editor_ids, exp.editor_ids)
            self.assertEqual(item.colorist_ids, exp.colorist_ids)
            self.assertEqual(item.director_ids, exp.director_ids)
            self.assertEqual(item.project_roles, exp.project_roles)

    def test_benchmark_400_projects_and_400_contacts(self) -> None:
        with self.registry._transaction() as db:
            for i in range(400):
                pid = f"t6n_{i:032x}"
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (pid,))
                contact_page = self.fake.add_page(
                    CONTACTS_DS,
                    {
                        ContactsRow.notion_name("name"): title(f"Person Synthetic {i:03d}"),
                        ContactsRow.notion_name("telegram_handle"): rich_text(f"@syn{i:03d}"),
                    },
                )
                db.execute(
                    "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 1.0, 1)",
                    (normalize_page_id(contact_page["id"]), pid),
                )
                proj_id = f"t6p_{i:032x}"
                code = f"S{i:03d}"
                proj_page = self.fake.add_page(
                    PROJECTS_DS,
                    {
                        ProjectsRow.notion_name("name"): title(f"Show Synthetic {i:03d}"),
                        ProjectsRow.notion_name("project_code"): select(code),
                        ProjectsRow.notion_name("editors"): relation(contact_page["id"]),
                        ProjectsRow.notion_name("t6_post_coordinator"): relation(contact_page["id"]),
                    },
                )
                db.execute(
                    "INSERT INTO projects (id, revision, code, code_form, rotation, created_at) VALUES (?, 1, ?, ?, 'in', 1.0)",
                    (proj_id, code, code.lower()),
                )
                db.execute(
                    "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 1.0, 1)",
                    (normalize_page_id(proj_page["id"]), proj_id),
                )
                db.execute(
                    "INSERT INTO project_person_roles VALUES (?, ?, 'coordinator')",
                    (proj_id, pid),
                )

        writer = MirrorWriter(self.mirror_path)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            {"projects": PROJECTS_DS, "contacts": CONTACTS_DS},
            min_request_interval=0.0,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.rescan_source("projects")
        syncer.rescan_source("contacts")

        before_http = _count_notion_http(self.fake)
        t0 = time.perf_counter()
        resolved = self.api._typed_read("project", "resolve", ProjectResolveInput(text="S399"))
        resolve_ms = (time.perf_counter() - t0) * 1000.0
        self.assertIsInstance(resolved, Done)
        assert isinstance(resolved, Done)
        self.assertEqual(resolved.result["match"]["project_code"], "S399")
        self.assertEqual(_count_notion_http(self.fake), before_http)
        self.assertLess(resolve_ms, 300.0, f"project resolve took {resolve_ms:.1f}ms")

        before_http = _count_notion_http(self.fake)
        t1 = time.perf_counter()
        found = self.api._typed_read("person", "find", PersonFindInput(mention="Person Synthetic 399"))
        find_ms = (time.perf_counter() - t1) * 1000.0
        self.assertIsInstance(found, Done)
        assert isinstance(found, Done)
        self.assertEqual(found.result["match"]["name"], "Person Synthetic 399")
        self.assertEqual(_count_notion_http(self.fake), before_http)
        self.assertLess(find_ms, 200.0, f"person find took {find_ms:.1f}ms")

        # Also verify person list uses mirror & returns all 401 contacts cleanly
        before_http = _count_notion_http(self.fake)
        listed = self.api._typed_read("person", "list", PersonListInput(registered=True))
        self.assertIsInstance(listed, Done)
        assert isinstance(listed, Done)
        self.assertEqual(len(listed.result["people"]), 400)
        self.assertEqual(_count_notion_http(self.fake), before_http)


class _InProcessTransport:
    def __init__(self, app: Any) -> None:
        self.app = app

    def send(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        from t6.server import Request

        answer = self.app.tailnet(Request(method, path, dict(headers), body))
        return answer.status, json.loads(json.dumps(dict(answer.body)))


class WebhookPageChangedMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "mirror.sqlite"
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        self.other_ds = "ds-other-9999"
        self.fake.add_data_source(self.other_ds)
        self.writer = MirrorWriter(self.db_path, clock=self.clock)
        self.addCleanup(self.writer.close)
        self.p1 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Alpha"),
                ProjectsRow.notion_name("project_code"): select("ALP"),
            },
        )
        self.c1 = self.fake.add_page(
            CONTACTS_DS,
            {
                ContactsRow.notion_name("name"): title("Ana Cruz"),
                ContactsRow.notion_name("telegram_handle"): rich_text("@ana"),
            },
        )
        self.syncer = MirrorSyncer(
            self.writer,
            NotionSource(self.fake.client),
            {"projects": PROJECTS_DS, "contacts": CONTACTS_DS},
            min_request_interval=0.0,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.syncer.tick()
        self.reader = MirrorReader(
            self.db_path,
            clock=self.clock,
            enabled_data_sources=[PROJECTS_DS, CONTACTS_DS],
        )
        self.source = NotionSource(self.fake.client, mirror=self.reader)
        self.live_source = NotionSource(self.fake.client)

    def _row(self, page_id: str) -> tuple[str, int, int, str] | None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT last_edited_time, archived, deleted, raw_json FROM mirror_pages WHERE page_id = ?",
                (normalize_page_id(page_id),),
            ).fetchone()
            if row is None:
                return None
            return (str(row[0]), int(row[1]), int(row[2]), str(row[3]))

    def test_webhook_lifecycle_created_updated_deleted_undeleted_and_variants(self) -> None:
        active = (PROJECTS_DS, CONTACTS_DS)

        # 1. page.created adds new mirrored project immediately
        self.clock.advance(5.0)
        p2 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Beta"),
                ProjectsRow.notion_name("project_code"): select("BET"),
            },
        )
        outcome = self.writer.handle_page_changed(
            self.live_source,
            {
                "page_id": p2["id"],
                "event_id": "evt-1",
                "event_type": "page.created",
                "updated_properties": [],
                "timestamp": p2["last_edited_time"],
            },
            active_data_source_ids=active,
        )
        self.assertEqual(outcome, "upserted")
        self.assertEqual(
            {p.name for p in ProjectReader(self.source, PROJECTS_DS).list()},
            {"Show Alpha", "Show Beta"},
        )
        state = self.writer.state(PROJECTS_DS)
        assert state is not None
        self.assertEqual(state.row_count, 2)

        # 2. page.properties_updated updates mirrored row
        self.clock.advance(5.0)
        self.fake.update_page(
            p2["id"],
            {ProjectsRow.notion_name("name"): title("Show Beta v2")},
        )
        outcome = self.writer.handle_page_changed(
            self.live_source,
            {
                "page_id": p2["id"],
                "event_id": "evt-2",
                "event_type": "page.properties_updated",
                "updated_properties": ["title"],
                "timestamp": self.fake.page(p2["id"])["last_edited_time"],
            },
            active_data_source_ids=active,
        )
        self.assertEqual(outcome, "upserted")
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(p2["id"]).name, "Show Beta v2")

        # 3. page.deleted marks row deleted=1 and hides it from MirrorReader
        self.clock.advance(5.0)
        delete_ts = self.fake._now_iso()
        self.fake.update_page(p2["id"], in_trash=True, last_edited_time=delete_ts)
        outcome = self.writer.handle_page_changed(
            self.live_source,
            {
                "page_id": p2["id"],
                "event_id": "evt-3",
                "event_type": "page.deleted",
                "timestamp": delete_ts,
            },
            active_data_source_ids=active,
        )
        self.assertEqual(outcome, "deleted")
        row_after_delete = self._row(p2["id"])
        assert row_after_delete is not None
        self.assertEqual(row_after_delete[2], 1)
        self.assertIsNone(self.reader.retrieve_page(p2["id"]))
        state = self.writer.state(PROJECTS_DS)
        assert state is not None
        self.assertEqual(state.row_count, 1)

        # 4. page.undeleted restores row (deleted=0)
        self.clock.advance(5.0)
        restore_ts = self.fake._now_iso()
        self.fake.update_page(
            p2["id"],
            {ProjectsRow.notion_name("name"): title("Show Beta Restored")},
            in_trash=False,
            last_edited_time=restore_ts,
        )
        outcome = self.writer.handle_page_changed(
            self.live_source,
            {
                "page_id": p2["id"],
                "event_id": "evt-4",
                "event_type": "page.undeleted",
                "timestamp": restore_ts,
            },
            active_data_source_ids=active,
        )
        self.assertEqual(outcome, "upserted")
        self.assertEqual(
            ProjectReader(self.source, PROJECTS_DS).get(p2["id"]).name,
            "Show Beta Restored",
        )
        state = self.writer.state(PROJECTS_DS)
        assert state is not None
        self.assertEqual(state.row_count, 2)

        # 5. reconcile, raw Notion envelope, and legacy typeless payloads also update mirror
        self.clock.advance(5.0)
        self.fake.update_page(p2["id"], {ProjectsRow.notion_name("name"): title("Show Beta Reconciled")})
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": p2["id"], "event_type": "reconcile"},
                active_data_source_ids=active,
            ),
            "upserted",
        )
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(p2["id"]).name, "Show Beta Reconciled")

        self.clock.advance(5.0)
        self.fake.update_page(p2["id"], {ProjectsRow.notion_name("name"): title("Show Beta Raw")})
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {
                    "id": "raw-1",
                    "type": "page.properties_updated",
                    "timestamp": self.fake.page(p2["id"])["last_edited_time"],
                    "entity": {"id": p2["id"], "type": "page"},
                },
                active_data_source_ids=active,
            ),
            "upserted",
        )
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(p2["id"]).name, "Show Beta Raw")

        self.clock.advance(5.0)
        self.fake.update_page(p2["id"], {ProjectsRow.notion_name("name"): title("Show Beta Typeless")})
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": p2["id"], "updated_properties": []},
                active_data_source_ids=active,
            ),
            "upserted",
        )
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(p2["id"]).name, "Show Beta Typeless")

        # 6. archived=True sets both archived=1 and deleted=1
        self.clock.advance(5.0)
        self.fake.update_page(p2["id"], archived=True)
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": p2["id"], "event_type": "page.properties_updated"},
                active_data_source_ids=active,
            ),
            "upserted",
        )
        archived_row = self._row(p2["id"])
        assert archived_row is not None
        self.assertEqual((archived_row[1], archived_row[2]), (1, 1))
        self.assertIsNone(self.reader.retrieve_page(p2["id"]))

        # 7. Hard-deleted 404 on live retrieve marks mirrored row deleted=1
        self.fake.delete_page(self.c1["id"])
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": self.c1["id"], "event_type": "page.properties_updated"},
                active_data_source_ids=active,
            ),
            "deleted",
        )
        deleted_c1 = self._row(self.c1["id"])
        assert deleted_c1 is not None
        self.assertEqual(deleted_c1[2], 1)

        # 8. parent.database_id fallback works in upsert_page
        db_page: dict[str, Any] = {
            "object": "page",
            "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "parent": {"type": "database_id", "database_id": PROJECTS_DS},
            "archived": False,
            "in_trash": False,
            "last_edited_time": "2026-11-20T10:00:00Z",
            "properties": {ProjectsRow.notion_name("name"): title("Show Via DB Parent")},
        }
        self.assertEqual(self.writer.upsert_page(db_page, active_data_source_ids=active), "upserted")
        self.assertIsNotNone(self.reader.retrieve_page(str(db_page["id"])))

    def test_monotonic_guard_handles_out_of_order_z_vs_ms_and_late_deleted(self) -> None:
        active = (PROJECTS_DS, CONTACTS_DS)
        page_id = self.p1["id"]

        # Seed stored row with ".999Z" (later in the same second than "Z")
        newer_page = {
            **self.fake.page(page_id),
            "last_edited_time": "2026-11-20T10:00:00.999Z",
            "properties": {
                **self.fake.page(page_id)["properties"],
                ProjectsRow.notion_name("name"): title("Show Newer 999ms"),
            },
        }
        self.assertEqual(self.writer.upsert_page(newer_page, active_data_source_ids=active), "upserted")

        # Older event ending in "Z" (000ms) is greater in ASCII ("Z" > ".") but older by datetime!
        older_page_z = {
            **self.fake.page(page_id),
            "last_edited_time": "2026-11-20T10:00:00Z",
            "properties": {
                **self.fake.page(page_id)["properties"],
                ProjectsRow.notion_name("name"): title("Show Stale 000ms"),
            },
        }
        self.assertEqual(self.writer.upsert_page(older_page_z, active_data_source_ids=active), "stale_ignored")
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(page_id).name, "Show Newer 999ms")

        # Repeated identical timestamp (>=) with offset normalization updates cleanly
        same_ts_offset = {
            **newer_page,
            "last_edited_time": "2026-11-20T10:00:00.999+00:00",
            "properties": {
                **newer_page["properties"],
                ProjectsRow.notion_name("name"): title("Show Same Instant Updated"),
            },
        }
        self.assertEqual(self.writer.upsert_page(same_ts_offset, active_data_source_ids=active), "upserted")
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(page_id).name, "Show Same Instant Updated")

        # Late page.deleted ("10:00:00Z") arriving after page.undeleted ("10:00:00.999Z") must NOT clobber!
        before_http = _count_notion_http(self.fake)
        del_outcome = self.writer.handle_page_changed(
            self.live_source,
            {
                "page_id": page_id,
                "event_type": "page.deleted",
                "timestamp": "2026-11-20T10:00:00Z",
            },
            active_data_source_ids=active,
        )
        self.assertEqual(del_outcome, "stale_ignored")
        self.assertEqual(_count_notion_http(self.fake), before_http)
        row = self._row(page_id)
        assert row is not None
        self.assertEqual(row[2], 0)
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(page_id).name, "Show Same Instant Updated")

        # Out-of-order older page.properties_updated via handle_page_changed does not overwrite newer state
        self.fake.update_page(
            page_id,
            {ProjectsRow.notion_name("name"): title("Show Old Notion State")},
            last_edited_time="2026-11-20T09:59:59Z",
        )
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": page_id, "event_type": "page.properties_updated"},
                active_data_source_ids=active,
            ),
            "stale_ignored",
        )
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(page_id).name, "Show Same Instant Updated")

    def test_page_outside_mirrored_data_sources_leaves_mirror_untouched(self) -> None:
        active = (PROJECTS_DS, CONTACTS_DS)
        unmirrored_jo = self.fake.add_page(
            JOB_ORDERS_DS,
            {JobOrdersRow.notion_name("name"): title("JO Unmirrored")},
        )
        other_page = self.fake.add_page(self.other_ds, {})

        with closing(sqlite3.connect(self.db_path)) as conn:
            before_count = int(conn.execute("SELECT COUNT(*) FROM mirror_pages").fetchone()[0])

        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": unmirrored_jo["id"], "event_type": "page.created"},
                active_data_source_ids=active,
            ),
            "not_mirrored",
        )
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": other_page["id"], "event_type": "page.properties_updated"},
                active_data_source_ids=active,
            ),
            "not_mirrored",
        )
        self.assertEqual(
            self.writer.handle_page_changed(
                self.live_source,
                {"page_id": other_page["id"], "event_type": "page.deleted"},
                active_data_source_ids=active,
            ),
            "not_mirrored",
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            after_count = int(conn.execute("SELECT COUNT(*) FROM mirror_pages").fetchone()[0])
        self.assertEqual(after_count, before_count)
        self.assertIsNone(self.writer.state(JOB_ORDERS_DS))
        self.assertIsNone(self.writer.state(self.other_ds))

    def test_worker_configured_handlers_updates_mirror_and_isolates_failures(self) -> None:
        from t6.notion.projection import ROTATION, T6_ID
        from t6.server import Grant, ServerConfig, WebhookApp
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        from t6.worker.client import WorkerJob

        self.fake.add_property(PROJECTS_DS, T6_ID, "rich_text")
        self.fake.add_property(PROJECTS_DS, ROTATION, "select")
        reg = SqliteProjectRegistry(Path(self.tmp.name) / "worker-reg.sqlite", clock=self.clock)
        worker_secret = "worker-secret"
        app = WebhookApp(
            JobQueue(Path(self.tmp.name) / "worker-q.sqlite", self.clock),
            ServerConfig(
                grants=(
                    Grant(
                        worker_secret,
                        claim=frozenset({"main"}),
                        submit=frozenset({"main"}),
                        registry_read=True,
                        registry_sync=True,
                        actor="main-worker",
                    ),
                )
            ),
            self.clock,
            registry=reg,
        )
        created = reg.register(
            actor="setup",
            idempotency_key="reg-p1",
            notion_page_id=self.p1["id"],
            code="ALP",
            rotation=Rotation.IN,
        )
        assert isinstance(created, Registered)
        transport = _InProcessTransport(app)
        settings = WorkerSettings("http://server.invalid:8081", worker_secret, "main")

        def open_fake_client(_token: str) -> Any:
            client = self.fake.client()
            orig_close = client.close
            closed = False

            def close_once() -> None:
                nonlocal closed
                if not closed:
                    closed = True
                    orig_close()

            client.close = close_once  # type: ignore[method-assign]
            self.addCleanup(close_once)
            return client

        # Off-switch / unset mirror DB creates no mirror file and wraps nothing
        off_db = Path(self.tmp.name) / "off-mirror.sqlite"
        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            off_handlers = configured_handlers(
                {
                    "T6_NOTION_TOKEN": "fake",
                    "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS_DS,
                    "T6_NOTION_MIRROR": "0",
                    "T6_NOTION_MIRROR_DB": str(off_db),
                },
                settings,
                transport,
            )
        self.assertEqual(set(off_handlers["main"]), {"registry.sync", "notion.page_changed", "project.status_sync"})
        self.assertFalse(off_db.exists())

        # Enabled mirror wraps notion.page_changed, updates mirror AND still runs ProjectProjection
        env = {
            "T6_NOTION_TOKEN": "fake",
            "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS_DS,
            "T6_NOTION_CONTACTS_DATA_SOURCE": CONTACTS_DS,
            "T6_NOTION_MIRROR_DB": str(self.db_path),
        }
        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            handlers = configured_handlers(env, settings, transport)
        handlers["main"]["registry.sync"](
            WorkerJob(
                job_id=0,
                queue="main",
                kind="registry.sync",
                dedupe_key="sync-p1",
                payload={"project_id": created.project.id, "revision": 1},
                token="t",
                attempt=1,
            )
        )
        page_changed = handlers["main"]["notion.page_changed"]

        self.clock.advance(5.0)
        self.fake.update_page(
            self.p1["id"],
            {
                ProjectsRow.notion_name("name"): title("Show Alpha Webhook"),
                ProjectsRow.notion_name("project_code"): select("HACK"),
            },
        )
        job = WorkerJob(
            job_id=1,
            queue="main",
            kind="notion.page_changed",
            dedupe_key="evt-w1",
            payload={
                "page_id": self.p1["id"],
                "event_id": "evt-w1",
                "event_type": "page.properties_updated",
                "updated_properties": [],
                "timestamp": self.fake.page(self.p1["id"])["last_edited_time"],
            },
            token="t",
            attempt=1,
        )
        result = page_changed(job)
        self.assertIsInstance(result, dict)
        # Mirror was updated with latest page title
        self.assertEqual(ProjectReader(self.source, PROJECTS_DS).get(self.p1["id"]).name, "Show Alpha Webhook")
        # ProjectProjection still ran and recorded projection conflict + restored Project Code
        self.assertEqual(len(reg.review_items()), 1)

        # Mirror error is isolated: ProjectProjection still runs, and logs never leak page titles/messages
        self.clock.advance(5.0)
        self.fake.update_page(
            self.p1["id"],
            {
                ProjectsRow.notion_name("name"): title("Sensitive Synthetic Title"),
                ProjectsRow.notion_name("project_code"): select("ALP"),
            },
        )
        stderr_buf = io.StringIO()
        with patch.object(
            MirrorWriter,
            "handle_page_changed",
            side_effect=RuntimeError("Sensitive Synthetic Title secret body"),
        ):
            with redirect_stderr(stderr_buf):
                isolated_result = page_changed(
                    WorkerJob(
                        job_id=2,
                        queue="main",
                        kind="notion.page_changed",
                        dedupe_key="evt-w2",
                        payload={
                            "page_id": self.p1["id"],
                            "event_id": "evt-w2",
                            "event_type": "page.properties_updated",
                            "updated_properties": [],
                        },
                        token="t",
                        attempt=1,
                    )
                )
        self.assertIsInstance(isolated_result, dict)
        logged = stderr_buf.getvalue()
        self.assertIn("RuntimeError", logged)
        self.assertIn("page.properties_updated", logged)
        self.assertNotIn("Sensitive Synthetic Title", logged)
        self.assertNotIn("secret body", logged)
        # ProjectProjection still resolved the conflict on the second webhook
        self.assertEqual(reg.review_items(), ())

        # Also works when mirror data sources come from T6_READ_DATA_SOURCES without T6_NOTION_PROJECTS_DATA_SOURCE,
        # and when JobOrderProjection / DeliverableProjection are chained in configured_handlers.
        ds_file = Path(self.tmp.name) / "data-sources.json"
        ds_file.write_text(json.dumps({"contacts": CONTACTS_DS}), encoding="utf-8")
        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            ds_only_handlers = configured_handlers(
                {
                    "T6_NOTION_TOKEN": "fake",
                    "T6_READ_DATA_SOURCES": str(ds_file),
                    "T6_NOTION_MIRROR_DB": str(self.db_path),
                },
                settings,
                transport,
            )
        self.assertIn("notion.page_changed", ds_only_handlers["main"])
        self.clock.advance(5.0)
        c2 = self.fake.add_page(
            CONTACTS_DS,
            {ContactsRow.notion_name("name"): title("Ben Lim")},
        )
        ds_outcome = ds_only_handlers["main"]["notion.page_changed"](
            WorkerJob(
                job_id=3,
                queue="main",
                kind="notion.page_changed",
                dedupe_key="evt-w3",
                payload={"page_id": c2["id"], "event_type": "page.created"},
                token="t",
                attempt=1,
            )
        )
        self.assertEqual(ds_outcome, {"outcome": "upserted"})
        self.assertEqual(ContactReader(self.source, CONTACTS_DS).get(c2["id"]).name, "Ben Lim")

        # Chained ProjectProjection -> JobOrderProjection -> DeliverableProjection stays intact even if mirror errors
        self.fake.add_snapshot_data_source(JOB_ORDERS_DS, "job_orders")
        deliverables_ds = "ds-deliverables-0004"
        self.fake.add_snapshot_data_source(deliverables_ds, "deliverables")
        deliv_page = self.fake.add_page(deliverables_ds, {})
        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            chained_handlers = configured_handlers(
                {
                    "T6_NOTION_TOKEN": "fake",
                    "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS_DS,
                    "T6_NOTION_JOB_ORDERS_DATA_SOURCE": JOB_ORDERS_DS,
                    "T6_NOTION_DELIVERABLES_DATA_SOURCE": deliverables_ds,
                    "T6_NOTION_MIRROR_DB": str(self.db_path),
                },
                settings,
                transport,
            )
        with (
            patch.object(MirrorWriter, "handle_page_changed", side_effect=OSError("disk error")),
            patch("t6.notion.deliverable_projection.projection_id", return_value="T6 ID"),
            redirect_stderr(io.StringIO()),
        ):
            chained_result = chained_handlers["main"]["notion.page_changed"](
                WorkerJob(
                    job_id=4,
                    queue="main",
                    kind="notion.page_changed",
                    dedupe_key="evt-w4",
                    payload={"page_id": deliv_page["id"], "event_type": "page.properties_updated"},
                    token="t",
                    attempt=1,
                )
            )
        self.assertIsInstance(chained_result, dict)


class JobOrdersAndDeliverablesMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
        self.fake.add_snapshot_data_source(DELIVERABLES_DS, "deliverables")
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        self.registry = SqliteProjectRegistry(Path(self.tmp.name) / "registry.sqlite", clock=self.clock)
        self.mirror_path = Path(self.tmp.name) / "mirror.sqlite"

        self.p1 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Alpha"),
                ProjectsRow.notion_name("project_code"): select("ALP"),
            },
        )
        self.p2 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Beta"),
                ProjectsRow.notion_name("project_code"): select("BET"),
            },
        )
        reg1 = self.registry.register(
            actor="setup", idempotency_key="p1", notion_page_id=self.p1["id"], code="ALP", rotation=Rotation.IN
        )
        reg2 = self.registry.register(
            actor="setup", idempotency_key="p2", notion_page_id=self.p2["id"], code="BET", rotation=Rotation.IN
        )
        assert isinstance(reg1, Registered) and isinstance(reg2, Registered)
        self.p1_t6_id = reg1.project.id
        self.p2_t6_id = reg2.project.id

        self.jo1 = self.fake.add_page(
            JOB_ORDERS_DS,
            {
                JobOrdersRow.notion_name("name"): title("Cut 1"),
                JobOrdersRow.notion_name("jo_number"): rich_text("#101"),
                JobOrdersRow.notion_name("projects"): relation(self.p1["id"]),
                JobOrdersRow.notion_name("telegram_key"): rich_text("-100:1"),
                JobOrdersRow.notion_name("status"): select("In progress"),
            },
        )
        self.clock.advance(5.0)
        self.jo1_dup = self.fake.add_page(
            JOB_ORDERS_DS,
            {
                JobOrdersRow.notion_name("name"): title("Cut 1 Dup"),
                JobOrdersRow.notion_name("jo_number"): rich_text("#102"),
                JobOrdersRow.notion_name("projects"): relation(self.p1["id"]),
                JobOrdersRow.notion_name("telegram_key"): rich_text("-100:1"),
                JobOrdersRow.notion_name("status"): select("Pending"),
            },
        )
        self.jo_archived = self.fake.add_page(
            JOB_ORDERS_DS,
            {
                JobOrdersRow.notion_name("name"): title("Cut Archived"),
                JobOrdersRow.notion_name("telegram_key"): rich_text("-100:99"),
            },
            archived=True,
        )
        self.d1 = self.fake.add_page(
            DELIVERABLES_DS,
            {
                DeliverablesRow.notion_name("name"): title("EP01 Master"),
                DeliverablesRow.notion_name("projects"): relation(self.p1["id"]),
            },
        )
        self.d2 = self.fake.add_page(
            DELIVERABLES_DS,
            {
                DeliverablesRow.notion_name("name"): title("EP02 Master"),
                DeliverablesRow.notion_name("projects"): relation(self.p2["id"]),
            },
        )
        self.d_trashed = self.fake.add_page(
            DELIVERABLES_DS,
            {
                DeliverablesRow.notion_name("name"): title("EP99 Trashed"),
                DeliverablesRow.notion_name("projects"): relation(self.p1["id"]),
            },
            in_trash=True,
        )

        self.writer = MirrorWriter(self.mirror_path, clock=self.clock)
        self.addCleanup(self.writer.close)
        self.sources = {
            "projects": PROJECTS_DS,
            "contacts": CONTACTS_DS,
            "job_orders": JOB_ORDERS_DS,
            "deliverables": DELIVERABLES_DS,
            "bays": BAYS_DS,
        }
        self.syncer = MirrorSyncer(
            self.writer,
            NotionSource(self.fake.client),
            self.sources,
            min_request_interval=0.0,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.env = patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _api(self, mirror: MirrorReader | None) -> VerbAPI:
        return VerbAPI(
            People(self.registry, PreviewTokens("key")),
            JobQueue(Path(self.tmp.name) / "queue.sqlite"),
            {"secret": "watchdog"},
            lambda _: self.fake.client(),
            data_sources=self.sources,
            mirror=mirror,
        )

    def test_mirror_reader_filters_and_sorts_match_notion_semantics(self) -> None:
        self.syncer.tick()
        reader = MirrorReader(self.mirror_path, clock=self.clock)

        # Property ID (unquote) filter matching for Deliverables & Job Orders
        deliv_proj_id = DeliverablesRow.notion_id("projects")
        assert deliv_proj_id is not None
        matched_deliv = reader.query_data_source(
            DELIVERABLES_DS,
            filter={"property": deliv_proj_id, "relation": {"contains": self.p1["id"].replace("-", "")}},
        )
        assert matched_deliv is not None
        self.assertEqual([r["id"] for r in matched_deliv], [self.d1["id"]])

        jo_key_id = JobOrdersRow.notion_id("telegram_key")
        assert jo_key_id is not None
        matched_jo = reader.query_data_source(
            JOB_ORDERS_DS,
            filter={"property": jo_key_id, "rich_text": {"equals": "-100:1"}},
            sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        )
        assert matched_jo is not None
        self.assertEqual([r["id"] for r in matched_jo], [self.jo1["id"], self.jo1_dup["id"]])

        # Property-name filters: title, select, status, checkbox, number, and, or
        self.fake.add_snapshot_data_source("ds-custom-filter", "projects")
        custom_ds = "ds-custom-filter"
        p_a = {
            "object": "page",
            "id": "11111111-1111-1111-1111-111111111111",
            "parent": {"type": "data_source_id", "data_source_id": custom_ds},
            "archived": False,
            "in_trash": False,
            "created_time": "2026-11-20T10:00:00Z",
            "last_edited_time": "2026-11-20T12:00:00Z",
            "properties": {
                "Name": title("Item A"),
                "Project Code": select("AAA"),
                "Status": {"type": "status", "status": {"name": "Active"}},
                "Flag": checkbox(True),
                "Eps": number(4),
            },
        }
        p_b = {
            "object": "page",
            "id": "22222222-2222-2222-2222-222222222222",
            "parent": {"type": "data_source_id", "data_source_id": custom_ds},
            "archived": False,
            "in_trash": False,
            "created_time": "2026-11-20T09:00:00Z",
            "last_edited_time": "2026-11-20T12:00:00Z",
            "properties": {
                "Name": title("Item B"),
                "Project Code": select("BBB"),
                "Status": {"type": "status", "status": {"name": "Hold"}},
                "Flag": checkbox(False),
                "Eps": number(8),
            },
        }
        p_c = {
            "object": "page",
            "id": "33333333-3333-3333-3333-333333333333",
            "parent": {"type": "data_source_id", "data_source_id": custom_ds},
            "archived": False,
            "in_trash": False,
            "created_time": "2026-11-20T09:00:00Z",
            "last_edited_time": "2026-11-20T11:00:00Z",
            "properties": {
                "Name": title("Item C"),
                "Project Code": select("CCC"),
                "Status": {"type": "status", "status": {"name": "Active"}},
                "Flag": checkbox(True),
                "Eps": number(8),
            },
        }
        self.writer.replace_data_source(custom_ds, [p_a, p_b, p_c], synced_at=self.clock())

        and_rows = reader.query_data_source(
            custom_ds,
            filter={
                "and": [
                    {"property": "Status", "status": {"equals": "Active"}},
                    {"property": "Flag", "checkbox": {"equals": True}},
                    {"property": "Eps", "number": {"equals": 8}},
                ]
            },
        )
        assert and_rows is not None
        self.assertEqual([r["id"] for r in and_rows], [p_c["id"]])

        or_rows = reader.query_data_source(
            custom_ds,
            filter={
                "or": [
                    {"property": "Name", "title": {"equals": "Item A"}},
                    {"property": "Project Code", "select": {"equals": "BBB"}},
                ]
            },
        )
        assert or_rows is not None
        self.assertEqual([r["id"] for r in or_rows], [p_a["id"], p_b["id"]])

        # Sorts on created_time / last_edited_time preserve insertion order on ties
        created_asc = reader.query_data_source(
            custom_ds,
            sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        )
        assert created_asc is not None
        self.assertEqual([r["id"] for r in created_asc], [p_b["id"], p_c["id"], p_a["id"]])

        created_desc = reader.query_data_source(
            custom_ds,
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
        )
        assert created_desc is not None
        self.assertEqual([r["id"] for r in created_desc], [p_a["id"], p_b["id"], p_c["id"]])

        edited_desc = reader.query_data_source(
            custom_ds,
            sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
        )
        assert edited_desc is not None
        self.assertEqual([r["id"] for r in edited_desc], [p_a["id"], p_b["id"], p_c["id"]])

    def test_jo_find_and_deliverable_candidates_parity_fallbacks_and_live_jo_get(self) -> None:
        from t6.notion.job_orders import JobOrders

        live_api = self._api(mirror=None)
        reader = MirrorReader(self.mirror_path, max_age_seconds=300.0, clock=self.clock)
        mirror_api = self._api(mirror=reader)

        # 1. Not-ready mirror -> falls back to live (1 HTTP call, no extra live call on miss)
        before = _count_notion_http(self.fake)
        miss_not_ready = mirror_api._typed_read("jo", "find", JoFindInput(telegram_key="-999:0"))
        self.assertIsInstance(miss_not_ready, Done)
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        # Sync all configured sources generically
        self.syncer.tick()
        self.assertTrue(self.writer.state(JOB_ORDERS_DS) and self.writer.state(JOB_ORDERS_DS).ready)  # type: ignore[union-attr]
        self.assertTrue(self.writer.state(DELIVERABLES_DS) and self.writer.state(DELIVERABLES_DS).ready)  # type: ignore[union-attr]
        self.assertTrue(self.writer.state(BAYS_DS) and self.writer.state(BAYS_DS).ready)  # type: ignore[union-attr]

        # 2. Mirror hits: identical answers to live Notion with 0 live Notion HTTP calls
        queries: tuple[Callable[[VerbAPI], Any], ...] = (
            lambda api: api._typed_read("jo", "find", JoFindInput(telegram_key="-100:1")),
            lambda api: api._typed_read("jo", "find", JoFindInput(chat_id="-100", message_id="1")),
            lambda api: api._typed_read("jo", "find", JoFindInput(project=self.p1_t6_id, number="101")),
            lambda api: api._typed_read("deliverable", "candidates", DeliverableCandidatesInput(project=self.p1_t6_id)),
        )
        for query_call in queries:
            live_res = query_call(live_api)
            before = _count_notion_http(self.fake)
            mirror_res = query_call(mirror_api)
            self.assertEqual(_count_notion_http(self.fake), before)
            self.assertTrue(last_read_from_mirror())
            assert isinstance(live_res, Done) and isinstance(mirror_res, Done)
            self.assertEqual(mirror_res.result, live_res.result)

        # 3. Retry-once-on-miss when new JO / Deliverable appears in live Notion after sync
        self.clock.advance(5.0)
        jo_new = self.fake.add_page(
            JOB_ORDERS_DS,
            {
                JobOrdersRow.notion_name("name"): title("Cut New"),
                JobOrdersRow.notion_name("jo_number"): rich_text("#205"),
                JobOrdersRow.notion_name("projects"): relation(self.p1["id"]),
                JobOrdersRow.notion_name("telegram_key"): rich_text("-100:205"),
            },
        )
        d_new = self.fake.add_page(
            DELIVERABLES_DS,
            {
                DeliverablesRow.notion_name("name"): title("EP03 Master"),
                DeliverablesRow.notion_name("projects"): relation(self.p2["id"]),
            },
        )
        # Clear p2's deliverables in mirror only? No, p2 already has d2; test on a third project with 0 in mirror
        p3 = self.fake.add_page(PROJECTS_DS, {ProjectsRow.notion_name("name"): title("Show Gamma")})
        d3 = self.fake.add_page(
            DELIVERABLES_DS,
            {
                DeliverablesRow.notion_name("name"): title("EP04 Master"),
                DeliverablesRow.notion_name("projects"): relation(p3["id"]),
            },
        )
        self.assertIsNotNone(d_new)

        before = _count_notion_http(self.fake)
        found_new_jo = mirror_api._typed_read("jo", "find", JoFindInput(telegram_key="-100:205"))
        self.assertEqual(_count_notion_http(self.fake) - before, 1)
        assert isinstance(found_new_jo, Done)
        self.assertEqual([o["page_id"] for o in found_new_jo.result["job_orders"]], [jo_new["id"]])

        before = _count_notion_http(self.fake)
        found_dup_jo = mirror_api._typed_read("jo", "find", JoFindInput(project=self.p1_t6_id, number="205"))
        self.assertEqual(_count_notion_http(self.fake) - before, 1)
        assert isinstance(found_dup_jo, Done)
        self.assertEqual([o["page_id"] for o in found_dup_jo.result["job_orders"]], [jo_new["id"]])

        before = _count_notion_http(self.fake)
        found_new_deliv = mirror_api._typed_read("deliverable", "candidates", DeliverableCandidatesInput(project=p3["id"]))
        self.assertEqual(_count_notion_http(self.fake) - before, 1)
        assert isinstance(found_new_deliv, Done)
        self.assertEqual([d["page_id"] for d in found_new_deliv.result["candidates"]], [d3["id"]])

        # JobOrders.find_by_telegram_key also retries once on miss when mirror is active
        source = NotionSource(self.fake.client, mirror=reader)
        jo_reader = JobOrders(source, JOB_ORDERS_DS)
        before = _count_notion_http(self.fake)
        hit = jo_reader.find_by_telegram_key("-100:1")
        self.assertIsNotNone(hit)
        # 1 live call for list_block_children (body stays live), 0 for query_data_source
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        before = _count_notion_http(self.fake)
        miss_retry_hit = jo_reader.find_by_telegram_key("-100:205")
        self.assertIsNotNone(miss_retry_hit)
        # 1 live query_data_source + 1 live list_block_children
        self.assertEqual(_count_notion_http(self.fake) - before, 2)

        # True miss retries live ONCE (1 HTTP call), and when mirror is OFF does 1 HTTP call (not 2)
        before = _count_notion_http(self.fake)
        self.assertIsNone(jo_reader.find_by_telegram_key("-100:does-not-exist"))
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        live_jo_reader = JobOrders(NotionSource(self.fake.client), JOB_ORDERS_DS)
        before = _count_notion_http(self.fake)
        self.assertIsNone(live_jo_reader.find_by_telegram_key("-100:does-not-exist"))
        self.assertEqual(_count_notion_http(self.fake) - before, 1)

        # 4. jo get ALWAYS stays live even when job_orders mirror is ready and fresh
        self.fake.update_page(self.jo1["id"], {JobOrdersRow.notion_name("name"): title("Cut 1 Live Edit")})
        before = _count_notion_http(self.fake)
        got = mirror_api._typed_read("jo", "get", JoGetInput(ref=self.jo1["id"]))
        self.assertGreaterEqual(_count_notion_http(self.fake) - before, 2)  # retrieve_page + list_block_children
        assert isinstance(got, Done)
        self.assertEqual(got.result["job_order"]["name"], "Cut 1 Live Edit")

        # 5. Stale mirror (> max_age_seconds) falls back to live (1 HTTP call on miss)
        self.clock.advance(301.0)
        before = _count_notion_http(self.fake)
        stale_miss = mirror_api._typed_read("jo", "find", JoFindInput(telegram_key="-999:stale"))
        self.assertIsInstance(stale_miss, Done)
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before, 1)


class WorkerWriteThroughMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        self.fake.add_snapshot_data_source(DELIVERABLES_DS, "deliverables")
        self.db_path = Path(self.tmp.name) / "mirror.sqlite"
        self.registry = SqliteProjectRegistry(Path(self.tmp.name) / "registry.sqlite", clock=self.clock)

        self.p1 = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Alpha"),
                ProjectsRow.notion_name("project_code"): select("ALP"),
            },
        )
        reg = self.registry.register(
            actor="setup", idempotency_key="p1", notion_page_id=self.p1["id"], code="ALP", rotation=Rotation.IN
        )
        assert isinstance(reg, Registered)
        self.p1_t6_id = reg.project.id

    def test_write_through_hook_swallows_errors_and_updates_mirror_after_jo_raise_without_poll(self) -> None:
        from t6.job_order import JobOrderStore
        from t6.server import Grant, ServerConfig, WebhookApp
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        from t6.worker.client import WorkerJob

        # 1. Verify record_written_page safely logs error class and never raises on write-through failure
        def boom(_page: dict[str, Any]) -> None:
            raise sqlite3.OperationalError("Sensitive Synthetic Title should not leak")

        failing_source = NotionSource(self.fake.client, on_page_written=boom)
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            created = failing_source.create_page(
                {"parent": {"data_source_id": JOB_ORDERS_DS}, "properties": {"Name": title("Cut Fail Hook")}}
            )
        self.assertTrue(created["id"])
        self.assertIn("OperationalError", stderr_buf.getvalue())
        self.assertNotIn("Sensitive Synthetic Title", stderr_buf.getvalue())

        # 2. Seed initial mirror state (ready=1) via MirrorSyncer
        writer = MirrorWriter(self.db_path, clock=self.clock)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            {
                "projects": PROJECTS_DS,
                "contacts": CONTACTS_DS,
                "job_orders": JOB_ORDERS_DS,
                "deliverables": DELIVERABLES_DS,
            },
            min_request_interval=0.0,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        syncer.tick()

        # 3. Raise a JO via JobOrderStore (outbox intent)
        store = JobOrderStore(self.registry, PreviewTokens("secret"))
        values = {
            "fields": {
                "project_id": self.p1["id"],
                "name": "ALP - AE - JO #55",
                "number": "#55",
                "telegram_key": "-100:555",
            }
        }
        preview = store.write("raise", values, actor="ana", idempotency_key="raise-55")
        assert isinstance(preview, Preview)
        raised = store.write("raise", values, actor="ana", idempotency_key="raise-55", confirm=preview.token)
        assert isinstance(raised, Done)
        intent_id = raised.result["sync_intent_id"]

        # 4. Run main worker jo.sync handler WITH mirror write-through enabled (NO MirrorSyncer poll!)
        worker_secret = "worker-secret"
        app = WebhookApp(
            JobQueue(Path(self.tmp.name) / "q.sqlite", self.clock),
            ServerConfig(
                grants=(
                    Grant(
                        worker_secret,
                        claim=frozenset({"main"}),
                        submit=frozenset({"main"}),
                        registry_read=True,
                        registry_sync=True,
                        actor="main-worker",
                    ),
                )
            ),
            self.clock,
            registry=self.registry,
        )
        transport = _InProcessTransport(app)
        settings = WorkerSettings("http://server.invalid:8081", worker_secret, "main")

        def open_fake_client(_token: str) -> Any:
            client = self.fake.client()
            orig_close = client.close
            closed = False

            def close_once() -> None:
                nonlocal closed
                if not closed:
                    closed = True
                    orig_close()

            client.close = close_once  # type: ignore[method-assign]
            self.addCleanup(close_once)
            return client

        env = {
            "T6_NOTION_TOKEN": "fake",
            "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS_DS,
            "T6_NOTION_JOB_ORDERS_DATA_SOURCE": JOB_ORDERS_DS,
            "T6_NOTION_DELIVERABLES_DATA_SOURCE": DELIVERABLES_DS,
            "T6_NOTION_MIRROR_DB": str(self.db_path),
        }
        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            handlers = configured_handlers(env, settings, transport)
        self.assertIn("notion.mirror_refresh", handlers["main"])

        self.clock.advance(5.0)
        sync_result = handlers["main"]["jo.sync"](
            WorkerJob(
                job_id=1,
                queue="main",
                kind="jo.sync",
                dedupe_key="jo-sync-55",
                payload={"intent_id": intent_id},
                token="t",
                attempt=1,
            )
        )
        self.assertIsInstance(sync_result, dict)

        # 5. Query jo find from MirrorReader immediately with ZERO live Notion HTTP calls!
        reader = MirrorReader(self.db_path, clock=self.clock)
        api = VerbAPI(
            People(self.registry, PreviewTokens("secret")),
            JobQueue(Path(self.tmp.name) / "api-q.sqlite"),
            {"secret": "watchdog"},
            lambda _: self.fake.client(),
            data_sources={
                "projects": PROJECTS_DS,
                "contacts": CONTACTS_DS,
                "job_orders": JOB_ORDERS_DS,
                "deliverables": DELIVERABLES_DS,
            },
            mirror=reader,
        )
        before_http = _count_notion_http(self.fake)
        with patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake"}):
            found = api._typed_read("jo", "find", JoFindInput(telegram_key="-100:555"))
        self.assertEqual(_count_notion_http(self.fake), before_http)
        self.assertTrue(last_read_from_mirror())
        assert isinstance(found, Done)
        self.assertEqual(len(found.result["job_orders"]), 1)
        self.assertEqual(found.result["job_orders"][0]["t6_id"], raised.result["jo_id"])


class ApiVerbRefreshMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
        self.fake.add_snapshot_data_source(DELIVERABLES_DS, "deliverables")
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        self.fake.add_snapshot_data_source(CAMERAS_DS, "cameras")
        self.fake.add_snapshot_data_source(MEETING_NOTES_DS, "meeting_notes")
        self.db_path = Path(self.tmp.name) / "mirror.sqlite"
        self.registry = SqliteProjectRegistry(Path(self.tmp.name) / "registry.sqlite", clock=self.clock)

        self.p1 = self.fake.add_page(PROJECTS_DS, {ProjectsRow.notion_name("name"): title("Show Alpha")})
        reg_p = self.registry.register(actor="setup", idempotency_key="p1", notion_page_id=self.p1["id"], code="ALP", rotation=Rotation.IN)
        assert isinstance(reg_p, Registered)
        self.p1_t6_id = reg_p.project.id

        self.c1 = self.fake.add_page(
            CONTACTS_DS,
            {ContactsRow.notion_name("name"): title("Ana Cruz"), ContactsRow.notion_name("telegram_handle"): rich_text("@ana")},
        )
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_ana', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute(
                "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, 't6n_ana', 1.0, 1)",
                (normalize_page_id(self.c1["id"]),),
            )
        self.bay1 = self.fake.add_page(BAYS_DS, {"Name": title("Bay Alpha"), "Hostname": rich_text("Bay-Alpha"), "Username": rich_text("old")})
        self.cam1 = self.fake.add_page(CAMERAS_DS, {"Name": title("Camera Alpha")})

        self.writer = MirrorWriter(self.db_path, clock=self.clock)
        self.addCleanup(self.writer.close)
        self.sources = {
            "projects": PROJECTS_DS,
            "contacts": CONTACTS_DS,
            "job_orders": JOB_ORDERS_DS,
            "deliverables": DELIVERABLES_DS,
            "bays": BAYS_DS,
            "cameras": CAMERAS_DS,
            "meeting_notes": MEETING_NOTES_DS,
        }
        MirrorSyncer(
            self.writer,
            NotionSource(self.fake.client),
            self.sources,
            min_request_interval=0.0,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        ).tick()

    def _build_api(self, *, use_mirror: bool, with_tap_app: bool = True) -> tuple[VerbAPI, Any, Any]:
        from t6.server import Grant, ServerConfig, WebhookApp
        from t6.worker.internal_queue import InternalJobQueue

        queue = JobQueue(Path(self.tmp.name) / "q.sqlite", self.clock)
        inside = InternalJobQueue(self.registry) if with_tap_app else None
        tap_app = (
            WebhookApp(
                queue,
                ServerConfig(
                    grants=(
                        Grant(
                            "worker-secret",
                            claim=frozenset({"main"}),
                            submit=frozenset({"main"}),
                            registry_read=True,
                            registry_sync=True,
                            actor="main-worker",
                        ),
                    )
                ),
                self.clock,
                registry=self.registry,
                inside=inside,
            )
            if with_tap_app
            else None
        )
        reader = MirrorReader(self.db_path, clock=self.clock) if use_mirror else None
        api = VerbAPI(
            People(
                self.registry,
                PreviewTokens("key"),
                {"watchdog": frozenset({"operator", "fleet", "bays_edit", "coordinator", "minutes", "unmasked"})},
            ),
            queue,
            {"api-secret": "watchdog"},
            lambda _: self.fake.client(),
            tap_app=tap_app,
            data_sources=self.sources,
            mirror=reader,
        )
        return api, inside, queue

    def test_api_writes_queue_mirror_refresh_with_unique_keys_and_worker_updates_mirror(self) -> None:
        from t6.domain.events import JobKind
        from t6.server.http import make_server
        from t6.client import Client
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        from t6.worker.client import WorkerJob

        env = patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake", "T6_PREVIEW_SIGNING_KEY": "key"})
        env.start()
        self.addCleanup(env.stop)

        # 1. With mirror=None, API writes must NOT queue notion.mirror_refresh
        api_no_mirror, inside_off, queue_off = self._build_api(use_mirror=False, with_tap_app=True)
        prev = api_no_mirror._person_set_telegram(
            PersonTelegramInput(person_id="t6n_ana", telegram_id=1001, idempotency_key="tg-off"), "watchdog", self.c1["id"]
        )
        assert isinstance(prev, Preview)
        done = api_no_mirror._person_set_telegram(
            PersonTelegramInput(person_id="t6n_ana", telegram_id=1001, idempotency_key="tg-off", confirm=prev.token),
            "watchdog",
            self.c1["id"],
        )
        assert isinstance(done, Done)
        self.assertEqual(inside_off.find(JobKind.NOTION_MIRROR_REFRESH.value), [])
        self.assertEqual(queue_off.waiting("main"), [])

        # 2. With mirror enabled + tap_app._inside, all 6 write sites queue notion.mirror_refresh
        api, inside, _queue = self._build_api(use_mirror=True, with_tap_app=True)
        import threading
        server = make_server(("127.0.0.1", 0), api)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        client = Client(f"http://127.0.0.1:{server.server_port}", "api-secret")

        # Site 1: _person_set_telegram (first edit)
        self.clock.advance(5.0)
        p_tg1 = client.person_set_telegram(PersonTelegramInput(person_id="t6n_ana", telegram_id=2001, idempotency_key="tg-1"))
        assert isinstance(p_tg1, Preview)
        self.assertIsInstance(
            client.person_set_telegram(PersonTelegramInput(person_id="t6n_ana", telegram_id=2001, idempotency_key="tg-1", confirm=p_tg1.token)),
            Done,
        )
        jobs = inside.find(JobKind.NOTION_MIRROR_REFRESH.value)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(normalize_page_id(str(jobs[0].payload["page_id"])), normalize_page_id(self.c1["id"]))

        # Repeat edit to the same page must queue a SECOND job (unique per-write dedupe key!)
        self.clock.advance(5.0)
        p_tg2 = client.person_set_telegram(PersonTelegramInput(person_id="t6n_ana", telegram_id=2002, idempotency_key="tg-2"))
        assert isinstance(p_tg2, Preview)
        self.assertIsInstance(
            client.person_set_telegram(PersonTelegramInput(person_id="t6n_ana", telegram_id=2002, idempotency_key="tg-2", confirm=p_tg2.token)),
            Done,
        )
        jobs = inside.find(JobKind.NOTION_MIRROR_REFRESH.value)
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0].dedupe_key, jobs[1].dedupe_key)

        # Worker processes notion.mirror_refresh -> Contact in mirror_pages has telegram_user_id=2002
        settings = WorkerSettings("http://server.invalid:8081", "worker-secret", "main")
        transport = _InProcessTransport(api.tap_app)

        def open_fake_client(_token: str) -> Any:
            c = self.fake.client()
            orig_close = c.close
            closed = False

            def close_once() -> None:
                nonlocal closed
                if not closed:
                    closed = True
                    orig_close()

            c.close = close_once  # type: ignore[method-assign]
            self.addCleanup(close_once)
            return c

        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            handlers = configured_handlers(
                {
                    "T6_NOTION_TOKEN": "fake",
                    "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS_DS,
                    "T6_NOTION_CONTACTS_DATA_SOURCE": CONTACTS_DS,
                    "T6_NOTION_MIRROR_DB": str(self.db_path),
                },
                settings,
                transport,
            )
        refresh_handler = handlers["main"][JobKind.NOTION_MIRROR_REFRESH.value]
        refresh_handler(
            WorkerJob(
                job_id=jobs[1].job_id,
                queue="main",
                kind=JobKind.NOTION_MIRROR_REFRESH.value,
                dedupe_key=jobs[1].dedupe_key,
                payload=dict(jobs[1].payload),
                token="t",
                attempt=1,
            )
        )
        before_http = _count_notion_http(self.fake)
        found_ana = api._typed_read("person", "find", PersonFindInput(mention="Ana Cruz"))
        self.assertEqual(_count_notion_http(self.fake), before_http)
        assert isinstance(found_ana, Done)
        self.assertEqual(found_ana.result["match"]["telegram_user_id"], 2002)

        # Site 2: _person_create
        self.clock.advance(5.0)
        p_new = client.person_create(PersonCreateInput(name="Ben Lim", kind="staff", telegram_id=3003, idempotency_key="pc-1"))
        assert isinstance(p_new, Preview)
        c_new_done = client.person_create(PersonCreateInput(name="Ben Lim", kind="staff", telegram_id=3003, idempotency_key="pc-1", confirm=p_new.token))
        assert isinstance(c_new_done, Done)
        new_contact_page = c_new_done.result["person"]["page_id"]
        jobs = inside.find(JobKind.NOTION_MIRROR_REFRESH.value)
        self.assertEqual(len(jobs), 3)
        refresh_handler(
            WorkerJob(
                job_id=jobs[2].job_id,
                queue="main",
                kind=JobKind.NOTION_MIRROR_REFRESH.value,
                dedupe_key=jobs[2].dedupe_key,
                payload=dict(jobs[2].payload),
                token="t",
                attempt=1,
            )
        )
        before_http = _count_notion_http(self.fake)
        found_ben = api._typed_read("person", "find", PersonFindInput(mention="Ben Lim"))
        self.assertEqual(_count_notion_http(self.fake), before_http)
        assert isinstance(found_ben, Done)
        self.assertEqual(found_ben.result["match"]["page_id"], new_contact_page)

        # Site 3: _meeting_notes (create, append, mark-sent, trash)
        mn_create = client.meeting_notes_create(MeetingNotesCreateInput(title="Weekly Sync", meeting_date="2026-11-20", project_ids=[self.p1_t6_id]))
        assert isinstance(mn_create, Done)
        mn_id = mn_create.result["meeting_note"]["page_id"]
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 4)

        self.assertIsInstance(client.meeting_notes_append(MeetingNotesAppendInput(note=mn_id, blocks=[])), Done)
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 5)

        self.assertIsInstance(client.meeting_notes_mark_sent(MeetingNotesMarkSentInput(note=mn_id)), Done)
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 6)

        mn_create2 = client.meeting_notes_create(MeetingNotesCreateInput(title="Weekly Sync 2", meeting_date="2026-11-20", project_ids=[self.p1_t6_id]))
        assert isinstance(mn_create2, Done)
        mn2_id = mn_create2.result["meeting_note"]["page_id"]
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 7)
        self.assertIsInstance(client.meeting_notes_trash(MeetingNotesTrashInput(note=mn2_id)), Done)
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 8)

        # Site 4: _machine (preview queues nothing; confirm queues refresh)
        ms_in = MachineSyncInput(machines=[MachineFactsInput(names=["Bay-Alpha"], serial_number="SN-1")])
        ms_prev = client.machine_sync(ms_in)
        assert isinstance(ms_prev, Preview)
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 8)
        self.assertIsInstance(client.machine_sync(ms_in.model_copy(update={"confirm": ms_prev.token})), Done)
        jobs = inside.find(JobKind.NOTION_MIRROR_REFRESH.value)
        self.assertEqual(len(jobs), 9)
        # Process the bays refresh job before Site 5 edits the same bay row so mirror reflects SN-1
        ds_cfg_file = Path(self.tmp.name) / "refresh-sources.json"
        ds_cfg_file.write_text(json.dumps(self.sources), encoding="utf-8")
        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            all_handlers = configured_handlers(
                {
                    "T6_NOTION_TOKEN": "fake",
                    "T6_READ_DATA_SOURCES": str(ds_cfg_file),
                    "T6_NOTION_MIRROR_DB": str(self.db_path),
                },
                settings,
                transport,
            )
        all_handlers["main"][JobKind.NOTION_MIRROR_REFRESH.value](
            WorkerJob(
                job_id=jobs[8].job_id,
                queue="main",
                kind=JobKind.NOTION_MIRROR_REFRESH.value,
                dedupe_key=jobs[8].dedupe_key,
                payload=dict(jobs[8].payload),
                token="t",
                attempt=1,
            )
        )

        # Site 5: _machine_edit (preview queues nothing; confirm saved queues refresh)
        me_in = MachineEditInput(machines=[MachineEditItem(names=["Bay-Alpha"], values={"username": "editor1"})])
        me_prev = client.machine_edit(me_in)
        assert isinstance(me_prev, Preview)
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 9)
        self.assertIsInstance(client.machine_edit(me_in.model_copy(update={"confirm": me_prev.token})), Done)
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 10)

        # Site 6: _camera_enrich
        ce_in = CameraEnrichInput(page_id=self.cam1["id"], fields={"name": "Camera Alpha v2"}, idempotency_key="cam-1")
        ce_prev = client.camera_enrich(ce_in)
        assert isinstance(ce_prev, Preview)
        self.assertIsInstance(client.camera_enrich(ce_in.model_copy(update={"confirm": ce_prev.token})), Done)
        self.assertEqual(len(inside.find(JobKind.NOTION_MIRROR_REFRESH.value)), 11)

        # Worker refresh respects monotonic guard & marks deleted on 404
        reader = MirrorReader(self.db_path, clock=self.clock)
        self.fake.update_page(
            self.c1["id"],
            {ContactsRow.notion_name("name"): title("Ana Stale")},
            last_edited_time="2020-01-01T00:00:00Z",
        )
        stale_res = refresh_handler(
            WorkerJob(
                job_id=99,
                queue="main",
                kind=JobKind.NOTION_MIRROR_REFRESH.value,
                dedupe_key="refresh-stale",
                payload={"page_id": self.c1["id"]},
                token="t",
                attempt=1,
            )
        )
        self.assertEqual(stale_res, {"outcome": "stale_ignored"})
        self.assertEqual(ContactReader(NotionSource(self.fake.client, mirror=reader), CONTACTS_DS).get(self.c1["id"]).name, "Ana Cruz")

        self.fake.delete_page(self.c1["id"])
        del_res = refresh_handler(
            WorkerJob(
                job_id=100,
                queue="main",
                kind=JobKind.NOTION_MIRROR_REFRESH.value,
                dedupe_key="refresh-404",
                payload={"page_id": self.c1["id"]},
                token="t",
                attempt=1,
            )
        )
        self.assertEqual(del_res, {"outcome": "deleted"})
        self.assertIsNone(reader.retrieve_page(self.c1["id"]))

        # Fallback queue when tap_app is None
        api_no_tap, _inside_none, fallback_queue = self._build_api(use_mirror=True, with_tap_app=False)
        ce2 = api_no_tap._camera_enrich(CameraEnrichInput(page_id=self.cam1["id"], fields={"name": "Camera Fallback"}, idempotency_key="cam-fb"), "watchdog")
        assert isinstance(ce2, Preview)
        self.assertIsInstance(
            api_no_tap._camera_enrich(CameraEnrichInput(page_id=self.cam1["id"], fields={"name": "Camera Fallback"}, idempotency_key="cam-fb", confirm=ce2.token), "watchdog"),
            Done,
        )
        self.assertEqual([j.kind for j in fallback_queue.waiting("main")], [JobKind.NOTION_MIRROR_REFRESH.value])


class AllSourcesGenericMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
        self.fake.add_snapshot_data_source(BAYS_DS, "bays")
        self.mirror_path = Path(self.tmp.name) / "mirror.sqlite"
        self.registry = SqliteProjectRegistry(Path(self.tmp.name) / "registry.sqlite", clock=self.clock)

        self.ds_ids: dict[str, str] = {
            "projects": PROJECTS_DS,
            "contacts": CONTACTS_DS,
            "job_orders": JOB_ORDERS_DS,
            "deliverables": DELIVERABLES_DS,
            "meeting_notes": MEETING_NOTES_DS,
            "swing_drives": "ds-swing-0008",
            "bays": BAYS_DS,
            "cameras": CAMERAS_DS,
            "studios": "ds-studios-0009",
            "package_specs": "ds-package-specs-0010",
            "synthetic_extra": "ds-synthetic-0011",
        }
        for key, ds_id in self.ds_ids.items():
            if ds_id not in (PROJECTS_DS, CONTACTS_DS, JOB_ORDERS_DS):
                if key == "synthetic_extra":
                    self.fake.add_data_source(ds_id)
                else:
                    self.fake.add_snapshot_data_source(ds_id, key)

        self.fake.add_option(JOB_ORDERS_DS, "Department", "AE")
        self.fake.add_option(JOB_ORDERS_DS, "Department", "VFX")
        self.fake.add_option(JOB_ORDERS_DS, "Department", "MISC")
        self.fake._data_sources[PROJECTS_DS]["description"] = [
            {"plain_text": "Studio ", "annotations": {"bold": False}},
            {"plain_text": "Notice", "annotations": {"bold": True}},
        ]

        # Seed rows in all 11 data sources (synthetic names only)
        self.studio_page = self.fake.add_page(
            self.ds_ids["studios"],
            {"Name": title("Client Corp"), "Aliases": multi_select("CC")},
        )
        self.proj_page = self.fake.add_page(
            PROJECTS_DS,
            {
                ProjectsRow.notion_name("name"): title("Show Alpha"),
                ProjectsRow.notion_name("project_code"): select("ALP"),
                ProjectsRow.notion_name("studio"): relation(self.studio_page["id"]),
            },
        )
        reg = self.registry.register(
            actor="setup", idempotency_key="reg-alp", notion_page_id=self.proj_page["id"], code="ALP", rotation=Rotation.IN
        )
        assert isinstance(reg, Registered)
        self.proj_t6_id = reg.project.id

        self.contact_page = self.fake.add_page(
            CONTACTS_DS,
            {ContactsRow.notion_name("name"): title("Ana Cruz"), ContactsRow.notion_name("telegram_handle"): rich_text("@ana")},
        )
        self.jo_page = self.fake.add_page(
            JOB_ORDERS_DS,
            {
                JobOrdersRow.notion_name("name"): title("Cut 1"),
                JobOrdersRow.notion_name("projects"): relation(self.proj_page["id"]),
                JobOrdersRow.notion_name("telegram_key"): rich_text("-100:1"),
            },
        )
        self.fake._children[self.jo_page["id"]] = [
            {"object": "block", "id": "blk-jo-1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "JO body line"}]}}
        ]
        self.deliv_page = self.fake.add_page(
            DELIVERABLES_DS,
            {
                DeliverablesRow.notion_name("name"): title("EP01 Master"),
                DeliverablesRow.notion_name("projects"): relation(self.proj_page["id"]),
            },
        )
        self.mn_ready = self.fake.add_page(
            MEETING_NOTES_DS,
            {
                "Name": title("Weekly Sync - Show Alpha"),
                "Meeting Date": date("2026-11-20"),
                "Project": relation(self.proj_page["id"]),
                "Ready to send": checkbox(True),
                "Sent at": date(None),
            },
        )
        self.fake._children[self.mn_ready["id"]] = [
            {"object": "block", "id": "blk-mn-1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Live note body"}]}}
        ]
        self.mn_sent = self.fake.add_page(
            MEETING_NOTES_DS,
            {
                "Name": title("Weekly Sync - Sent"),
                "Meeting Date": date("2026-11-19"),
                "Project": relation(self.proj_page["id"]),
                "Ready to send": checkbox(True),
                "Sent at": date("2026-11-19T12:00:00Z"),
            },
        )
        self.swing_page = self.fake.add_page(
            self.ds_ids["swing_drives"],
            {
                "Name": title("Swing Drive 01"),
                "In Use": checkbox(True),
                "Last Moved": date("2026-11-18"),
                "Collaborator": relation(self.contact_page["id"]),
                "Format ": select("APFS"),
                "HD Type": {"type": "multi_select", "multi_select": [{"name": "SSD"}]},
                "Short Notes": rich_text("Case A"),
            },
        )
        self.bay_page = self.fake.add_page(
            BAYS_DS,
            {"Name": title("Bay Alpha"), "Hostname": rich_text("Bay-Alpha"), "Ring": select("production")},
        )
        self.cam_page = self.fake.add_page(
            CAMERAS_DS,
            {"Name": title("Camera Alpha"), "Tags": {"type": "multi_select", "multi_select": [{"name": "4K"}]}},
        )
        self.spec_page = self.fake.add_page(
            self.ds_ids["package_specs"],
            {
                "Name": title("Delivery Spec"),
                "Package kind": rich_text("delivery"),
                "Version": rich_text("1.0"),
                "Advisory items": rich_text("Check audio"),
                "Blocking items": rich_text("Check frame rate"),
            },
        )
        self.extra_page = self.fake.add_page(
            self.ds_ids["synthetic_extra"],
            {"Name": title("Synthetic Row 1")},
        )

    def test_read_data_source_configs_from_env_and_worker_merge(self) -> None:
        from t6.server.verbs import read_config_from_env, read_data_source_configs_from_env
        from t6.worker.__main__ import mirror_data_sources_from_env

        cfg_path = Path(self.tmp.name) / "data-sources.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "projects": {"id": PROJECTS_DS, "max_age_seconds": 600},
                    "contacts": CONTACTS_DS,
                    "synthetic_extra": {"id": self.ds_ids["synthetic_extra"], "max_age_seconds": 120},
                }
            ),
            encoding="utf-8",
        )
        env = {"T6_READ_DATA_SOURCES": str(cfg_path)}
        configs = read_data_source_configs_from_env(env)
        self.assertEqual(
            configs,
            {
                "projects": DataSourceConfig(id=PROJECTS_DS, max_age_seconds=600.0),
                "contacts": DataSourceConfig(id=CONTACTS_DS, max_age_seconds=300.0),
                "synthetic_extra": DataSourceConfig(id=self.ds_ids["synthetic_extra"], max_age_seconds=120.0),
            },
        )
        legacy_sources, privacy = read_config_from_env(env)
        self.assertEqual(
            legacy_sources,
            {
                "projects": PROJECTS_DS,
                "contacts": CONTACTS_DS,
                "synthetic_extra": self.ds_ids["synthetic_extra"],
            },
        )
        self.assertIsNone(privacy)
        self.assertEqual(read_data_source_configs_from_env({}), {})

        # Worker merges legacy env vars only if NOT already present in T6_READ_DATA_SOURCES
        merged = mirror_data_sources_from_env(
            {
                "T6_READ_DATA_SOURCES": str(cfg_path),
                "T6_NOTION_PROJECTS_DATA_SOURCE": "ds-legacy-override-ignored",
                "T6_NOTION_JOB_ORDERS_DATA_SOURCE": JOB_ORDERS_DS,
                "T6_NOTION_DELIVERABLES_DATA_SOURCE": DELIVERABLES_DS,
            }
        )
        self.assertEqual(merged["projects"], DataSourceConfig(id=PROJECTS_DS, max_age_seconds=600.0))
        self.assertEqual(merged["job_orders"], DataSourceConfig(id=JOB_ORDERS_DS, max_age_seconds=300.0))
        self.assertEqual(merged["deliverables"], DataSourceConfig(id=DELIVERABLES_DS, max_age_seconds=300.0))
        self.assertEqual(merged["synthetic_extra"], DataSourceConfig(id=self.ds_ids["synthetic_extra"], max_age_seconds=120.0))

        # Validation rejects invalid max_age_seconds, unknown keys, and blank IDs
        for bad in (
            {"projects": ""},
            {"projects": "   "},
            {"projects": {"id": ""}},
            {"projects": {"id": PROJECTS_DS, "max_age_seconds": 0}},
            {"projects": {"id": PROJECTS_DS, "max_age_seconds": -1}},
            {"projects": {"id": PROJECTS_DS, "max_age_seconds": None}},
            {"projects": {"id": PROJECTS_DS, "max_age_seconds": float("nan")}},
            {"projects": {"id": PROJECTS_DS, "unknown_key": 1}},
        ):
            cfg_path.write_text(json.dumps(bad, allow_nan=True), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_data_source_configs(cfg_path)

    def test_all_10_sources_plus_extra_sync_read_from_mirror_and_bodies_stay_live(self) -> None:
        from t6.fleet.bays import Fleet
        from t6.notion.cameras import get_cameras
        from t6.notion.departments import get_departments
        from t6.notion.job_orders import JobOrders
        from t6.notion.meeting_notes import MeetingNotes
        from t6.notion.package_specs import PackageSpecReader
        from t6.notion.reads import StudioReader
        from t6.notion.swing_drives import read_swing_drives
        from t6.server.__main__ import mirror_reader_from_env
        from t6.server.verbs import read_data_source_configs_from_env
        from t6.verbs import DepartmentsInput, MeetingNotesListInput, StudioNoticesInput
        from t6.worker.__main__ import mirror_syncer_from_env

        cfg_path = Path(self.tmp.name) / "data-sources.json"
        raw_cfg: dict[str, Any] = {k: v for k, v in self.ds_ids.items()}
        raw_cfg["cameras"] = {"id": CAMERAS_DS, "max_age_seconds": 60}
        raw_cfg["bays"] = {"id": BAYS_DS, "max_age_seconds": 600}
        cfg_path.write_text(json.dumps(raw_cfg), encoding="utf-8")

        env = {
            "T6_NOTION_TOKEN": "fake",
            "T6_READ_DATA_SOURCES": str(cfg_path),
            "T6_NOTION_MIRROR_DB": str(self.mirror_path),
        }

        def open_fake_client(_token: str) -> Any:
            client = self.fake.client()
            orig_close = client.close
            closed = False

            def close_once() -> None:
                nonlocal closed
                if not closed:
                    closed = True
                    orig_close()

            client.close = close_once  # type: ignore[method-assign]
            self.addCleanup(close_once)
            return client

        with patch("t6.notion.NotionClient", side_effect=open_fake_client):
            syncer = mirror_syncer_from_env(env)
        assert syncer is not None
        self.addCleanup(syncer._writer.close)
        syncer._clock = self.clock
        syncer._wall_clock = self.clock
        syncer._sleep = self.clock.sleep
        syncer.tick()

        # All 11 configured data sources (10 studio + 1 synthetic extra) are ready with source_json & max_age_seconds
        for key, ds_id in self.ds_ids.items():
            state = syncer._writer.state(ds_id)
            assert state is not None, f"missing mirror state for {key}"
            self.assertTrue(state.ready, f"not ready: {key}")
            self.assertIsNotNone(state.source_json, f"missing source_json for {key}")
        self.assertEqual(syncer._writer.state(CAMERAS_DS).max_age_seconds, 60.0)  # type: ignore[union-attr]
        self.assertEqual(syncer._writer.state(BAYS_DS).max_age_seconds, 600.0)  # type: ignore[union-attr]

        source_configs = read_data_source_configs_from_env(env)
        reader = mirror_reader_from_env(env, source_configs)
        assert reader is not None
        reader._clock = self.clock
        live_source = NotionSource(self.fake.client)
        mirror_source = NotionSource(self.fake.client, mirror=reader)

        api = VerbAPI(
            People(self.registry, PreviewTokens("key")),
            JobQueue(Path(self.tmp.name) / "queue.sqlite"),
            {"secret": "watchdog"},
            lambda _: self.fake.client(),
            data_sources=self.ds_ids,
            mirror=reader,
        )

        # Every reader answers identically from mirror with 0 HTTP calls
        with patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake"}):
            before = _count_notion_http(self.fake)

            self.assertEqual([p.name for p in ProjectReader(mirror_source, PROJECTS_DS).list()], ["Show Alpha"])
            self.assertTrue(last_read_from_mirror())
            self.assertEqual([c.name for c in ContactReader(mirror_source, CONTACTS_DS).list()], ["Ana Cruz"])
            self.assertTrue(last_read_from_mirror())
            self.assertEqual(len(JobOrders(mirror_source, JOB_ORDERS_DS).list()), 1)
            self.assertTrue(last_read_from_mirror())
            self.assertEqual(
                [d.name for d in DeliverableReader(mirror_source, DELIVERABLES_DS).for_project(self.proj_page["id"])],
                ["EP01 Master"],
            )
            self.assertTrue(last_read_from_mirror())

            mn_reader = MeetingNotes(mirror_source, MEETING_NOTES_DS)
            self.assertEqual(len(mn_reader.with_title("Weekly Sync - Show Alpha")), 1)
            self.assertTrue(last_read_from_mirror())
            self.assertEqual(len(mn_reader.with_title_prefix("Weekly Sync")), 2)
            self.assertTrue(last_read_from_mirror())
            self.assertEqual([n.name for n in mn_reader.ready_to_send()], ["Weekly Sync - Show Alpha"])
            self.assertTrue(last_read_from_mirror())

            self.assertEqual(
                read_swing_drives(mirror_source, self.ds_ids["swing_drives"]),
                read_swing_drives(live_source, self.ds_ids["swing_drives"]),
            )
            # Only live_source made 1 HTTP call; mirror_source made 0
            self.assertEqual(_count_notion_http(self.fake) - before, 1)

            before = _count_notion_http(self.fake)
            self.assertEqual([m.name for m in Fleet(mirror_source, BAYS_DS).machines()], ["Bay Alpha"])
            self.assertTrue(last_read_from_mirror())
            self.assertEqual([c["name"] for c in get_cameras(mirror_source, CAMERAS_DS)], ["Camera Alpha"])
            self.assertTrue(last_read_from_mirror())
            self.assertEqual(StudioReader(mirror_source, self.ds_ids["studios"]).names(), ("Client Corp", "CC"))
            self.assertTrue(last_read_from_mirror())
            self.assertEqual(len(PackageSpecReader(mirror_source, self.ds_ids["package_specs"]).find("delivery")), 1)
            self.assertTrue(last_read_from_mirror())
            self.assertEqual(
                [d.name for d in get_departments(mirror_source, JOB_ORDERS_DS)],
                ["TOC", "AE", "OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN"],
            )
            self.assertTrue(last_read_from_mirror())

            # Synthetic 11th source queries and metadata work via config only
            extra_rows = mirror_source.query_data_source(self.ds_ids["synthetic_extra"])
            self.assertEqual([r["id"] for r in extra_rows], [self.extra_page["id"]])
            self.assertTrue(last_read_from_mirror())
            extra_meta = mirror_source.retrieve_data_source(self.ds_ids["synthetic_extra"])
            self.assertEqual(extra_meta["id"], self.ds_ids["synthetic_extra"])
            self.assertTrue(last_read_from_mirror())

            # VerbAPI studio notices & departments use mirror retrieve_data_source (0 HTTP calls)
            notices = api._typed_read("studio", "notices", StudioNoticesInput())
            self.assertIsInstance(notices, Done)
            depts = api._typed_read("departments", "", DepartmentsInput())
            self.assertIsInstance(depts, Done)
            assert isinstance(depts, Done)
            self.assertEqual(
                [d["name"] for d in depts.result["departments"]],
                ["TOC", "AE", "OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN"],
            )

            # Meeting Notes list without body uses 0 HTTP calls
            mn_no_body = api._typed_read("meeting-notes", "list", MeetingNotesListInput(include_body=False))
            self.assertIsInstance(mn_no_body, Done)
            self.assertEqual(_count_notion_http(self.fake), before)

            # Page bodies (list_block_children) ALWAYS stay live!
            before_body = _count_notion_http(self.fake)
            mn_with_body = api._typed_read(
                "meeting-notes", "list", MeetingNotesListInput(title="Weekly Sync - Show Alpha", include_body=True)
            )
            self.assertIsInstance(mn_with_body, Done)
            assert isinstance(mn_with_body, Done)
            self.assertEqual(mn_with_body.result["meeting_notes"][0]["body"][0]["text"], "Live note body")
            self.assertEqual(_count_notion_http(self.fake) - before_body, 1)
            self.assertEqual(self.fake.requests[-1].url.path, f"/v1/blocks/{self.mn_ready['id']}/children")

            before_jo_body = _count_notion_http(self.fake)
            jo_got = JobOrders(mirror_source, JOB_ORDERS_DS).get(self.jo_page["id"])
            self.assertEqual(jo_got.source_text, "JO body line")
            self.assertEqual(_count_notion_http(self.fake) - before_jo_body, 1)
            self.assertEqual(self.fake.requests[-1].url.path, f"/v1/blocks/{self.jo_page['id']}/children")

        # Per-source max_age_seconds: advance 90s -> cameras (max_age=60) falls back to live, bays (max_age=600) stays mirror!
        self.clock.advance(90.0)
        before_cam = _count_notion_http(self.fake)
        self.assertEqual([c["name"] for c in get_cameras(mirror_source, CAMERAS_DS)], ["Camera Alpha"])
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before_cam, 1)

        before_bay = _count_notion_http(self.fake)
        self.assertEqual([m.name for m in Fleet(mirror_source, BAYS_DS).machines()], ["Bay Alpha"])
        self.assertTrue(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake), before_bay)

        # retrieve_data_source freshness="live" bypasses mirror, and stale/missing source_json falls back to live
        before_ds = _count_notion_http(self.fake)
        self.assertEqual(mirror_source.retrieve_data_source(BAYS_DS, freshness="live")["id"], BAYS_DS)
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before_ds, 1)

        with closing(sqlite3.connect(self.mirror_path)) as conn:
            conn.execute(
                "UPDATE mirror_state SET source_json = NULL WHERE data_source_id = ?",
                (normalize_page_id(BAYS_DS),),
            )
            conn.commit()
        before_null_meta = _count_notion_http(self.fake)
        self.assertEqual(mirror_source.retrieve_data_source(BAYS_DS)["id"], BAYS_DS)
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before_null_meta, 1)

    def test_mirror_reader_full_filter_operators(self) -> None:
        ds = self.ds_ids["synthetic_extra"]
        self.fake.add_data_source(ds)
        rows = [
            {
                "object": "page",
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "parent": {"type": "data_source_id", "data_source_id": ds},
                "archived": False,
                "in_trash": False,
                "properties": {
                    "Name": title("Alpha Project One"),
                    "Notes": rich_text("prefix-middle-suffix"),
                    "EmptyText": rich_text(""),
                    "Status": select("Active"),
                    "Tags": {"type": "multi_select", "multi_select": [{"name": "4K"}, {"name": "RAW"}]},
                    "Due": date("2026-11-20"),
                    "Count": number(10),
                },
            },
            {
                "object": "page",
                "id": "aaaaaaaa-0000-0000-0000-000000000002",
                "parent": {"type": "data_source_id", "data_source_id": ds},
                "archived": False,
                "in_trash": False,
                "properties": {
                    "Name": title("Beta Project Two"),
                    "Notes": rich_text("other"),
                    "EmptyText": rich_text("filled"),
                    "Status": select("Paused"),
                    "Tags": {"type": "multi_select", "multi_select": []},
                    "Due": date("2026-11-25"),
                    "Count": number(3),
                },
            },
        ]
        writer = MirrorWriter(self.mirror_path, clock=self.clock)
        self.addCleanup(writer.close)
        writer.replace_data_source(ds, rows, synced_at=self.clock())
        reader = MirrorReader(self.mirror_path, clock=self.clock)

        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Name", "title": {"starts_with": "Alpha"}}) or [])],
            [rows[0]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Notes", "rich_text": {"ends_with": "suffix"}}) or [])],
            [rows[0]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Notes", "rich_text": {"contains": "middle"}}) or [])],
            [rows[0]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "EmptyText", "rich_text": {"is_empty": True}}) or [])],
            [rows[0]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "EmptyText", "rich_text": {"is_not_empty": True}}) or [])],
            [rows[1]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Status", "select": {"does_not_equal": "Active"}}) or [])],
            [rows[1]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Tags", "multi_select": {"contains": "RAW"}}) or [])],
            [rows[0]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Tags", "multi_select": {"is_empty": True}}) or [])],
            [rows[1]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Tags", "multi_select": {"is_not_empty": True}}) or [])],
            [rows[0]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Due", "date": {"on_or_after": "2026-11-22"}}) or [])],
            [rows[1]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Due", "date": {"before": "2026-11-22"}}) or [])],
            [rows[0]["id"]],
        )
        self.assertEqual(
            [r["id"] for r in (reader.query_data_source(ds, filter={"property": "Due", "date": {"equals": "2026-11-20"}}) or [])],
            [rows[0]["id"]],
        )

    def test_unsupported_filter_falls_back_to_live_notion(self) -> None:
        ds = self.ds_ids["synthetic_extra"]
        with MirrorWriter(self.mirror_path, clock=self.clock) as writer:
            writer.replace_data_source(ds, [self.extra_page], synced_at=self.clock(), source_key="synthetic_extra")
        reader = MirrorReader(self.mirror_path, clock=self.clock)
        source = NotionSource(self.fake.client, mirror=reader)

        unsupported_people = {"property": "Owner", "people": {"contains": "user-1"}}
        self.assertIsNone(reader.query_data_source(ds, filter=unsupported_people))
        self.assertIsNone(
            reader.query_data_source(
                ds,
                filter={
                    "and": [
                        {"property": "Name", "title": {"equals": "Synthetic Row 1"}},
                        unsupported_people,
                    ]
                },
            )
        )
        self.assertIsNone(
            reader.query_data_source(
                ds,
                filter={"timestamp": "last_edited_time", "last_edited_time": {"past_week": {}}},
            )
        )

        # NotionSource falls back to live Notion instead of returning unfiltered mirror rows
        before_http = _count_notion_http(self.fake)
        live_rows = source.query_data_source(ds, filter=unsupported_people)
        self.assertFalse(last_read_from_mirror())
        self.assertEqual(_count_notion_http(self.fake) - before_http, 1)
        self.assertEqual([r["id"] for r in live_rows], [self.extra_page["id"]])

    def test_staggered_1800s_rescan_pacing_with_multipage_pagination_across_10_sources(self) -> None:
        from t6.notion import RequestPacer

        # Seed >100 rows (250 rows -> 3 paginated POST requests) in projects to exercise iter_data_source pacing
        for i in range(250):
            self.fake.add_page(PROJECTS_DS, {ProjectsRow.notion_name("name"): title(f"Show Paginated {i:03d}")})

        studio_10 = {k: v for k, v in self.ds_ids.items() if k != "synthetic_extra"}
        request_timestamps: list[float] = []
        orig_handle = self.fake._handle

        def recording_handle(request: Any) -> Any:
            request_timestamps.append(self.clock())
            return orig_handle(request)

        self.fake._handle = recording_handle  # type: ignore[method-assign]
        import httpx

        self.fake.transport = httpx.MockTransport(self.fake._handle)

        pacer = RequestPacer(0.4, clock=self.clock, sleep=self.clock.sleep)
        writer = MirrorWriter(self.mirror_path, clock=self.clock)
        self.addCleanup(writer.close)
        syncer = MirrorSyncer(
            writer,
            NotionSource(lambda: self.fake.client(pacer=pacer), pacer=pacer),
            studio_10,
            min_request_interval=0.4,
            pacer=pacer,
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )

        # Cold start rescans all 10 sources (including 3 paginated requests for projects + retrieve_data_source each)
        syncer.tick()
        self.assertGreaterEqual(len(request_timestamps), 22)
        for t1, t2 in zip(request_timestamps, request_timestamps[1:]):
            self.assertGreaterEqual(t2 - t1, 0.4 - 1e-9)
        # Verify consecutive requests are >= 0.4s apart and sustained rate stays strictly < 3 req/s
        for idx, ts in enumerate(request_timestamps):
            in_window = sum(1 for other in request_timestamps[idx:] if other - ts < 1.0 - 1e-9)
            self.assertLessEqual(in_window, 3)
        for t0, t3 in zip(request_timestamps, request_timestamps[3:]):
            self.assertGreater(t3 - t0, 1.0)

        # Verify rescans are staggered across 1800s (10 sources -> 180s spacing) in sorted source_key order
        sorted_keys = sorted(studio_10)
        initial_rescans = {
            k: writer.state(studio_10[k]).last_full_rescan_at for k in sorted_keys  # type: ignore[union-attr]
        }
        # Advance to 1850s: only index 0 (bays) has rescanned again; index 1 (cameras, +180s) through 9 have not
        self.clock.advance(1850.0)
        syncer.tick()
        self.assertGreater(
            writer.state(studio_10[sorted_keys[0]]).last_full_rescan_at or 0.0,  # type: ignore[union-attr]
            initial_rescans[sorted_keys[0]] or 0.0,
        )
        for later_key in sorted_keys[1:]:
            self.assertEqual(
                writer.state(studio_10[later_key]).last_full_rescan_at,  # type: ignore[union-attr]
                initial_rescans[later_key],
                f"{later_key} rescanned too early",
            )

        # Advance another 200s (total +2050s): index 1 (cameras, due at +1980s) rescans, index 2 (contacts, +2160s) has not
        self.clock.advance(200.0)
        syncer.tick()
        self.assertGreater(
            writer.state(studio_10[sorted_keys[1]]).last_full_rescan_at or 0.0,  # type: ignore[union-attr]
            initial_rescans[sorted_keys[1]] or 0.0,
        )
        self.assertEqual(
            writer.state(studio_10[sorted_keys[2]]).last_full_rescan_at,  # type: ignore[union-attr]
            initial_rescans[sorted_keys[2]],
        )

    def test_worker_main_runs_mirror_tick_with_and_without_linked_tick(self) -> None:
        from t6.worker import __main__ as worker_main

        cfg_path = Path(self.tmp.name) / "data-sources.json"
        cfg_path.write_text(json.dumps({"bays": BAYS_DS}), encoding="utf-8")

        def open_fake_client(_token: str) -> Any:
            c = self.fake.client()
            orig_close = c.close
            closed = False

            def close_once() -> None:
                nonlocal closed
                if not closed:
                    closed = True
                    orig_close()

            c.close = close_once  # type: ignore[method-assign]
            self.addCleanup(close_once)
            return c

        # 1. Without linked_tick (no T6_GATEWAY_URL / T6_DATABASE): mirror_tick still runs!
        seen_ticks: list[Any] = []

        def fake_serve(worker: Any, stop: Any, poll_seconds: float, tick: Any = None, **kwargs: Any) -> None:
            seen_ticks.append(tick)
            if tick is not None:
                tick()
            stop.set()

        env_no_gateway = {
            "T6_SERVER_URL": "http://127.0.0.1:8081",
            "T6_WORKER_SECRET": "sec",
            "T6_WORKER_QUEUE": "main",
            "T6_NOTION_TOKEN": "fake",
            "T6_READ_DATA_SOURCES": str(cfg_path),
            "T6_NOTION_MIRROR_DB": str(self.mirror_path),
        }
        with (
            patch("t6.notion.NotionClient", side_effect=open_fake_client),
            patch.object(worker_main, "serve", side_effect=fake_serve),
        ):
            rc = worker_main.main(environ=env_no_gateway, transport=_InProcessTransport(None))
        self.assertEqual(rc, 0)
        self.assertIsNotNone(seen_ticks[0])
        reader = MirrorReader(self.mirror_path, clock=self.clock)
        self.assertIsNotNone(reader.query_data_source(BAYS_DS))

        # 2. With linked_tick configured: both linked.tick and mirror_syncer.tick run
        seen_ticks.clear()
        reg_path = Path(self.tmp.name) / "worker-linked.sqlite"
        SqliteProjectRegistry(reg_path, clock=self.clock)
        mirror_path_2 = Path(self.tmp.name) / "mirror2.sqlite"
        env_with_gateway = {
            **env_no_gateway,
            "T6_NOTION_MIRROR_DB": str(mirror_path_2),
            "T6_DATABASE": str(reg_path),
            "T6_API_URL": "http://127.0.0.1:8081",
            "T6_GATEWAY_URL": "http://127.0.0.1:8082",
            "T6_GATEWAY_SECRET": "gw-sec",
            "T6_REVIEW_CHAT": "12345",
        }
        with (
            patch("t6.notion.NotionClient", side_effect=open_fake_client),
            patch.object(worker_main, "serve", side_effect=fake_serve),
            patch("t6.worker.linked_chats.LinkedChats.tick") as mock_linked_tick,
        ):
            rc2 = worker_main.main(environ=env_with_gateway, transport=_InProcessTransport(None))
        self.assertEqual(rc2, 0)
        mock_linked_tick.assert_called_once()
        self.assertIsNotNone(MirrorReader(mirror_path_2, clock=self.clock).query_data_source(BAYS_DS))

    def test_worker_main_shares_single_mirror_writer_and_closes_on_shutdown(self) -> None:
        from t6.worker import __main__ as worker_main

        cfg_path = Path(self.tmp.name) / "single-writer-sources.json"
        cfg_path.write_text(json.dumps({"bays": BAYS_DS}), encoding="utf-8")

        def open_fake_client(_token: str) -> Any:
            c = self.fake.client()
            orig_close = c.close
            closed = False

            def close_once() -> None:
                nonlocal closed
                if not closed:
                    closed = True
                    orig_close()

            c.close = close_once  # type: ignore[method-assign]
            self.addCleanup(close_once)
            return c

        created_writers: list[MirrorWriter] = []
        closed_flags: list[bool] = []
        orig_init = MirrorWriter.__init__
        orig_close = MirrorWriter.close

        def spy_init(self_w: MirrorWriter, path: str | Path, *, clock: Any = time.time) -> None:
            orig_init(self_w, path, clock=clock)
            created_writers.append(self_w)

        def spy_close(self_w: MirrorWriter) -> None:
            closed_flags.append(True)
            orig_close(self_w)

        def fake_serve(worker: Any, stop: Any, poll_seconds: float, tick: Any = None, **kwargs: Any) -> None:
            if tick is not None:
                tick()
            # While main-worker serve() is running, WAL & SHM stay open via single shared writer
            self.assertEqual(len(created_writers), 1)
            self.assertEqual(len(closed_flags), 0)
            self.assertTrue(Path(f"{self.mirror_path}-wal").exists())
            self.assertTrue(Path(f"{self.mirror_path}-shm").exists())
            stop.set()

        env = {
            "T6_SERVER_URL": "http://127.0.0.1:8081",
            "T6_WORKER_SECRET": "sec",
            "T6_WORKER_QUEUE": "main",
            "T6_NOTION_TOKEN": "fake",
            "T6_READ_DATA_SOURCES": str(cfg_path),
            "T6_NOTION_MIRROR_DB": str(self.mirror_path),
        }
        with (
            patch("t6.notion.NotionClient", side_effect=open_fake_client),
            patch.object(MirrorWriter, "__init__", spy_init),
            patch.object(MirrorWriter, "close", spy_close),
            patch.object(worker_main, "serve", side_effect=fake_serve),
        ):
            rc = worker_main.main(environ=env, transport=_InProcessTransport(None))
        self.assertEqual(rc, 0)
        self.assertEqual(len(created_writers), 1)
        self.assertEqual(len(closed_flags), 1)
        self.assertFalse(Path(f"{self.mirror_path}-wal").exists())
        self.assertFalse(Path(f"{self.mirror_path}-shm").exists())


if __name__ == "__main__":
    unittest.main()
