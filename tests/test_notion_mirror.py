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
from t6.notion.generated import ContactsRow, JobOrdersRow, ProjectsRow
from t6.notion.reads import ContactReader, ProjectReader
from t6.notion.testing import FakeNotion, rich_text, select, title, relation
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import ProjectRefs, Registered, Rotation, SqliteProjectRegistry
from t6.registry.core import normalize_page_id
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import (
    Done,
    JoFindInput,
    PersonFindInput,
    PersonListInput,
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
        return {"projects": PROJECTS_DS, "contacts": CONTACTS_DS, "job_orders": JOB_ORDERS_DS}

    def test_permissions_0600_and_wal_mode(self) -> None:
        writer = MirrorWriter(self.db_path)
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
            if side.exists():
                self.assertEqual(stat.S_IMODE(os.stat(side).st_mode), 0o600)

    def test_poll_and_rescan_sync_new_edited_archived_and_deleted_restored(self) -> None:
        writer = MirrorWriter(self.db_path)
        syncer = MirrorSyncer(
            writer,
            NotionSource(self.fake.client),
            self._sources(),
            clock=self.clock,
            wall_clock=self.clock,
            sleep=self.clock.sleep,
        )
        # First tick rescans projects and contacts only (job_orders stays unmirrored in #516).
        syncer.tick()
        self.assertIsNotNone(writer.state(PROJECTS_DS))
        self.assertIsNotNone(writer.state(CONTACTS_DS))
        self.assertIsNone(writer.state(JOB_ORDERS_DS))

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

        # Hard-deleted page disappears after full rescan (staggered 30m+ across 2 sources).
        self.fake.delete_page(p2["id"])
        self.clock.advance(1850.0)
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
        writer = MirrorWriter(self.db_path)
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

        # At t=1805s, only projects rescans (contacts is staggered to 2700s).
        req_times.clear()
        self.clock.advance(1805.0)
        syncer.tick()
        proj_state = writer.state(PROJECTS_DS)
        cont_state = writer.state(CONTACTS_DS)
        assert proj_state is not None and cont_state is not None
        self.assertGreater(proj_state.last_full_rescan_at or 0.0, cont_state.last_full_rescan_at or 0.0)

    def test_fallbacks_not_ready_stale_and_live_bypass(self) -> None:
        # 1. Not ready -> live fallback
        writer = MirrorWriter(self.db_path)
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


class VerbMirrorAndBatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = StepClock(1_760_000_000.0)
        self.fake = _make_fake(clock=self.clock)
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
        MirrorSyncer(
            MirrorWriter(self.mirror_path),
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
        MirrorSyncer(
            MirrorWriter(self.mirror_path),
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
        self.other_ds = "ds-other-9999"
        self.fake.add_data_source(self.other_ds)
        self.writer = MirrorWriter(self.db_path, clock=self.clock)
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


if __name__ == "__main__":
    unittest.main()
