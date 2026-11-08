"""Exercise Event and Job types through the public t6.domain interface."""

from datetime import datetime, timezone
import unittest

from t6.domain import (
    Event,
    EventSource,
    FrameioCommentCreated,
    Job,
    JobKind,
    ProjectStatusChanged,
)


WHEN = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 8, 16, 30, tzinfo=timezone.utc)


class FrameioCommentEventTests(unittest.TestCase):
    def test_comment_event_is_a_typed_event(self) -> None:
        event = FrameioCommentCreated(
            comment_id="c-1",
            frameio_project_id="fp-1",
            file_id="f-1",
            source=EventSource.WEBHOOK,
            received_at=WHEN,
        )
        self.assertIsInstance(event, Event)
        self.assertEqual(event.comment_id, "c-1")
        self.assertEqual(event.frameio_project_id, "fp-1")
        self.assertEqual(event.file_id, "f-1")

    def test_same_comment_has_same_key_from_webhook_and_poll(self) -> None:
        from_webhook = FrameioCommentCreated(
            comment_id="c-1",
            frameio_project_id="fp-1",
            source=EventSource.WEBHOOK,
            received_at=WHEN,
        )
        from_poll = FrameioCommentCreated(
            comment_id="c-1",
            frameio_project_id="fp-1",
            file_id="f-1",
            source=EventSource.POLL,
            received_at=LATER,
        )
        self.assertEqual(from_webhook.dedupe_key, from_poll.dedupe_key)
        self.assertEqual(from_webhook, from_poll)
        self.assertEqual(hash(from_webhook), hash(from_poll))

    def test_different_comments_have_different_keys(self) -> None:
        first = FrameioCommentCreated(comment_id="c-1", frameio_project_id="fp-1")
        second = FrameioCommentCreated(comment_id="c-2", frameio_project_id="fp-1")
        self.assertNotEqual(first.dedupe_key, second.dedupe_key)

    def test_blank_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FrameioCommentCreated(comment_id=" ", frameio_project_id="fp-1")
        with self.assertRaises(ValueError):
            FrameioCommentCreated(comment_id="c-1", frameio_project_id="")


class ProjectStatusChangedTests(unittest.TestCase):
    def test_status_change_is_a_typed_event(self) -> None:
        event = ProjectStatusChanged(page_id="p-1", status="Project Start")
        self.assertIsInstance(event, Event)
        self.assertEqual(event.page_id, "p-1")
        self.assertEqual(event.status, "Project Start")

    def test_same_change_has_same_key_from_webhook_and_poll(self) -> None:
        from_webhook = ProjectStatusChanged(
            page_id="p-1", status="Project Start", source=EventSource.WEBHOOK, received_at=WHEN
        )
        from_poll = ProjectStatusChanged(
            page_id="p-1", status="Project Start", source=EventSource.POLL, received_at=LATER
        )
        self.assertEqual(from_webhook.dedupe_key, from_poll.dedupe_key)
        self.assertEqual(from_webhook, from_poll)

    def test_key_depends_on_page_and_status(self) -> None:
        base = ProjectStatusChanged(page_id="p-1", status="Project Start")
        other_page = ProjectStatusChanged(page_id="p-2", status="Project Start")
        other_status = ProjectStatusChanged(page_id="p-1", status="Delivered")
        self.assertNotEqual(base.dedupe_key, other_page.dedupe_key)
        self.assertNotEqual(base.dedupe_key, other_status.dedupe_key)

    def test_events_of_different_tools_never_share_a_key(self) -> None:
        comment = FrameioCommentCreated(comment_id="x", frameio_project_id="x")
        status = ProjectStatusChanged(page_id="x", status="x")
        self.assertNotEqual(comment.dedupe_key, status.dedupe_key)
        self.assertNotEqual(comment, status)

    def test_blank_fields_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ProjectStatusChanged(page_id="", status="Project Start")
        with self.assertRaises(ValueError):
            ProjectStatusChanged(page_id="p-1", status="  ")

    def test_source_defaults_to_webhook(self) -> None:
        event = ProjectStatusChanged(page_id="p-1", status="Project Start")
        self.assertIs(event.source, EventSource.WEBHOOK)
        self.assertIsNone(event.received_at)


class JobTests(unittest.TestCase):
    def test_job_carries_kind_payload_and_dedupe_key(self) -> None:
        job = Job(kind=JobKind.FRAMEIO_REVIEW_DONE, payload={"text": "hi"}, dedupe_key="tap:42")
        self.assertIs(job.kind, JobKind.FRAMEIO_REVIEW_DONE)
        self.assertEqual(job.payload, {"text": "hi"})
        self.assertEqual(job.dedupe_key, "tap:42")

    def test_job_for_event_derives_key_from_event_and_kind(self) -> None:
        webhook = ProjectStatusChanged(page_id="p-1", status="Project Start")
        poll = ProjectStatusChanged(page_id="p-1", status="Project Start", source=EventSource.POLL)
        a = Job.for_event(JobKind.PROJECT_START_APPROVAL, webhook, {"page_id": "p-1"})
        b = Job.for_event(JobKind.PROJECT_START_APPROVAL, poll, {"page_id": "p-1"})
        self.assertEqual(a.dedupe_key, b.dedupe_key)
        self.assertEqual(a, b)

    def test_jobs_of_different_kinds_from_one_event_have_different_keys(self) -> None:
        event = ProjectStatusChanged(page_id="p-1", status="Project Start")
        keys = {Job.for_event(kind, event).dedupe_key for kind in JobKind}
        self.assertEqual(len(keys), len(JobKind))

    def test_payload_is_read_only_and_copied(self) -> None:
        source = {"page_id": "p-1"}
        job = Job(kind=JobKind.FRAMEIO_REVIEW_DONE, payload=source, dedupe_key="k")
        source["page_id"] = "changed"
        self.assertEqual(job.payload["page_id"], "p-1")
        with self.assertRaises(TypeError):
            job.payload["page_id"] = "x"  # type: ignore[index]

    def test_payload_defaults_to_empty(self) -> None:
        job = Job(kind=JobKind.FRAMEIO_REVIEW_DONE, dedupe_key="k")
        self.assertEqual(dict(job.payload), {})

    def test_jobs_with_same_key_are_equal_and_hashable(self) -> None:
        a = Job(kind=JobKind.FRAMEIO_REVIEW_DONE, payload={"n": 1}, dedupe_key="k")
        b = Job(kind=JobKind.FRAMEIO_REVIEW_DONE, payload={"n": 2}, dedupe_key="k")
        self.assertEqual(a, b)
        self.assertEqual(len({a, b}), 1)

    def test_blank_dedupe_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Job(kind=JobKind.FRAMEIO_REVIEW_DONE, dedupe_key=" ")

    def test_job_kinds_have_stable_string_values(self) -> None:
        self.assertEqual(JobKind("project_start.approval"), JobKind.PROJECT_START_APPROVAL)
        for kind in JobKind:
            with self.subTest(kind=kind):
                self.assertEqual(JobKind(kind.value), kind)


if __name__ == "__main__":
    unittest.main()
