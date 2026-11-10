"""Project Codes and rotation through the private service: assign, release, reuse."""

from pathlib import Path
import threading
from typing import Any

from test_registry_service import PAGE, PAGE_DASHED, READER_SECRET, WORKER_SECRET, RegistryTestCase

from t6.server import Request, Response

OTHER_PAGE = "fedcba9876543210fedcba9876543210"


class CodesTestCase(RegistryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.keys = 0

    def key(self) -> str:
        self.keys += 1
        return f"auto-{self.keys}"

    def new(self, code: str | None = None, rotation: str | None = None, page: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if code is not None:
            body["code"] = code
        if rotation is not None:
            body["rotation"] = rotation
        if page is not None:
            body["notion_page_id"] = page
        answer = self.send("POST", "/registry/projects", body, key=self.key())
        self.assertEqual(answer.status, 201, answer.body)
        return self.project(answer)

    def read(self, project: dict[str, Any]) -> dict[str, Any]:
        return self.project(self.send("GET", f"/registry/projects/{project['id']}"))

    def change(self, project: dict[str, Any], what: str, body: dict[str, Any], **options: Any) -> Response:
        """POST one change at the Project's current revision, unless the body names one."""
        body = {"expected_revision": self.read(project)["revision"], **body}
        options.setdefault("key", self.key())
        return self.send("POST", f"/registry/projects/{project['id']}/{what}", body, **options)

    def set_code(self, project: dict[str, Any], code: str | None, **options: Any) -> Response:
        return self.change(project, "code", {"code": code}, **options)

    def rotate(self, project: dict[str, Any], rotation: str, **options: Any) -> Response:
        return self.change(project, "rotation", {"rotation": rotation}, **options)

    def bind(self, project: dict[str, Any], page: str, **options: Any) -> Response:
        return self.change(project, "bindings", {"notion_page_id": page}, **options)

    def footprint(self, project: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
        """Everything a refused change must leave alone."""
        return self.read(project), len(self.registry.audit(project["id"])), len(self.registry.pending_sync())


class ReuseTests(CodesTestCase):
    def test_a_code_moves_from_a_to_b_and_a_cannot_return_until_resolved(self) -> None:
        a = self.new("ABC", "in")
        self.assertEqual(self.rotate(a, "out").status, 200)
        left = self.read(a)
        self.assertEqual((left["code"], left["rotation"]), ("ABC", "out"))  # the code stays on record

        b = self.new()
        self.assertEqual(self.set_code(b, "abc").status, 200)
        self.assertEqual(self.rotate(b, "in").status, 200)

        before = self.footprint(a)
        refused = self.rotate(a, "in")
        self.assertEqual(refused.status, 409)
        self.assertEqual(
            refused.body, {"error": "code_held", "code": "ABC", "held_by": [b["id"]], "reason": "in_rotation"}
        )
        self.assertEqual(self.footprint(a), before)
        self.assertEqual(self.read(a)["id"], a["id"])

        # Resolution one: A takes another code.
        self.assertEqual(self.set_code(a, "ABD").status, 200)
        self.assertEqual(self.rotate(a, "in").status, 200)
        self.assertEqual(self.read(b)["rotation"], "in")

    def test_a_returns_with_its_code_once_b_is_released(self) -> None:
        a = self.new("ABC", "out")
        b = self.new("ABC", "in")
        self.assertEqual(self.rotate(a, "in").status, 409)
        self.assertEqual(self.rotate(b, "out").status, 200)
        self.assertEqual(self.rotate(a, "in").status, 200)
        self.assertEqual(self.read(b)["code"], "ABC")

    def test_clearing_a_code_frees_it(self) -> None:
        a = self.new("ABC", "in")
        b = self.new(rotation="in")
        self.assertEqual(self.set_code(b, "ABC").status, 409)
        cleared = self.set_code(a, None)
        self.assertEqual(cleared.status, 200)
        self.assertIsNone(self.project(cleared)["code"])
        self.assertEqual(self.set_code(b, "ABC").status, 200)

    def test_changing_a_code_in_rotation_is_checked(self) -> None:
        self.new("ABC", "in")
        b = self.new("XYZ", "in")
        before = self.footprint(b)
        self.assertEqual(self.set_code(b, "a b c").status, 409)
        self.assertEqual(self.footprint(b), before)

    def test_registration_with_a_held_code_creates_nothing(self) -> None:
        holder = self.new("ABC", "in")
        refused = self.send("POST", "/registry/projects", {"code": "abc", "rotation": "in"}, key="clash")
        self.assertEqual(refused.status, 409)
        self.assertEqual(refused.body["held_by"], [holder["id"]])
        self.assertEqual(len(self.registry.pending_sync()), 1)

    def test_reservations_survive_a_restart(self) -> None:
        a = self.new("ABC", "in")
        b = self.new("ABC")
        self.start()
        self.assertEqual(self.rotate(b, "in").status, 409)
        self.assertEqual(self.read(a)["rotation"], "in")


class CodeSpellingTests(CodesTestCase):
    def test_equivalent_spellings_are_one_code_and_the_spelling_is_kept(self) -> None:
        a = self.new("BH-101", "in")
        self.assertEqual(self.read(a)["code"], "BH-101")
        for spelling in ("bh 101", "BH101", " b.h-101 "):
            other = self.new(spelling)
            self.assertEqual(self.rotate(other, "in").status, 409, spelling)

    def test_projects_without_a_code_share_rotation_freely(self) -> None:
        a, b = self.new(rotation="in"), self.new(rotation="in")
        self.assertEqual((self.read(a)["rotation"], self.read(b)["rotation"]), ("in", "in"))
        blanked = self.set_code(self.new("ABC", "in"), "   ")
        self.assertEqual(blanked.status, 200)
        self.assertIsNone(self.project(blanked)["code"])

    def test_a_code_with_no_letters_or_digits_is_refused(self) -> None:
        a = self.new()
        self.assertEqual(self.set_code(a, "--").status, 400)
        self.assertEqual(self.send("POST", "/registry/projects", {"code": "--"}, key="k").status, 400)


class ConcurrencyTests(CodesTestCase):
    def test_concurrent_claims_have_one_winner(self) -> None:
        projects = [self.new("ABC") for _ in range(8)]
        statuses: list[int] = []
        gate = threading.Barrier(len(projects))

        def claim(project: dict[str, Any], key: str) -> None:
            body = {"rotation": "in", "expected_revision": project["revision"]}
            gate.wait()
            statuses.append(self.send("POST", f"/registry/projects/{project['id']}/rotation", body, key=key).status)

        threads = [threading.Thread(target=claim, args=(p, f"race-{i}")) for i, p in enumerate(projects)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(statuses), [200] + [409] * 7)
        self.assertEqual([self.read(p)["rotation"] for p in projects].count("in"), 1)


class UnclassifiedTests(CodesTestCase):
    def test_unclassified_projects_block_their_codes_until_reviewed(self) -> None:
        x = self.new("ABC", "unclassified")
        y = self.new("abc", "unclassified")  # imported rows may share a code; they stay separate
        self.assertNotEqual(x["id"], y["id"])
        z = self.new("ABC")
        refused = self.rotate(z, "in")
        self.assertEqual(refused.status, 409)
        self.assertEqual(refused.body["reason"], "unclassified")
        self.assertEqual(refused.body["held_by"], [x["id"], y["id"]])

        self.assertEqual(self.rotate(x, "in").status, 409)  # y is still unresolved
        self.assertEqual(self.rotate(y, "out").status, 200)
        classified = self.rotate(x, "in")
        self.assertEqual(classified.status, 200)
        self.assertEqual(self.registry.audit(x["id"])[-1].action, "classified")
        self.assertEqual(self.rotate(z, "in").status, 409)  # now held by x, in rotation

    def test_a_project_cannot_be_made_unclassified_again(self) -> None:
        a = self.new("ABC", "in")
        self.assertEqual(self.rotate(a, "unclassified").status, 400)
        self.assertEqual(self.rotate(a, "sideways").status, 400)


class RevisionAndReplayTests(CodesTestCase):
    def test_each_change_raises_the_revision_and_is_audited_with_a_sync_intent(self) -> None:
        a = self.new()
        changed = self.set_code(a, "ABC")
        self.assertEqual(changed.body["changed"], True)
        self.assertEqual(self.project(changed)["revision"], 2)
        self.clock.advance(5)
        self.rotate(a, "in")
        self.assertEqual(self.read(a)["revision"], 3)
        entries = self.registry.audit(a["id"])
        self.assertEqual([entry.action for entry in entries], ["registered", "code_changed", "rotation_changed"])
        self.assertEqual((entries[1].actor, entries[1].before, entries[1].after), ("writer", {"code": None}, {"code": "ABC"}))
        self.assertEqual((entries[2].at, entries[2].before, entries[2].after), (self.clock(), {"rotation": "out"}, {"rotation": "in"}))
        self.assertEqual([intent.revision for intent in self.registry.pending_sync()], [1, 2, 3])

    def test_a_stale_revision_is_refused_and_changes_nothing(self) -> None:
        a = self.new()
        self.set_code(a, "ABC")
        before = self.footprint(a)
        stale = self.change(a, "code", {"code": "XYZ", "expected_revision": 1})
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.body, {"error": "revision_conflict", "revision": 2})
        self.assertEqual(self.footprint(a), before)

    def test_a_change_needs_a_revision_and_a_key(self) -> None:
        a = self.new()
        path = f"/registry/projects/{a['id']}/code"
        self.assertEqual(self.send("POST", path, {"code": "ABC"}, key="k").status, 400)
        self.assertEqual(self.send("POST", path, {"code": "ABC", "expected_revision": True}, key="k").status, 400)
        self.assertEqual(self.send("POST", path, {"code": "ABC", "expected_revision": 1}).status, 400)
        self.assertEqual(self.send("POST", path, {"expected_revision": 1}, key="k").status, 400)
        self.assertEqual(self.send("POST", path, {"code": 7, "expected_revision": 1}, key="k").status, 400)
        self.assertEqual(self.read(a)["revision"], 1)

    def test_a_replayed_change_returns_the_project_and_changes_nothing_more(self) -> None:
        a = self.new()
        body = {"code": "ABC", "expected_revision": 1}
        first = self.change(a, "code", body, key="same")
        self.rotate(a, "in")
        before = self.footprint(a)
        again = self.change(a, "code", body, key="same")  # its revision is stale by now; a replay still answers
        self.assertEqual((first.status, again.status), (200, 200))
        self.assertEqual((again.body["changed"], again.body["replayed"]), (False, True))
        self.assertEqual(self.footprint(a), before)

    def test_a_key_reused_for_a_different_change_is_refused(self) -> None:
        a = self.new()
        self.set_code(a, "ABC", key="same")
        clash = self.set_code(a, "XYZ", key="same")
        self.assertEqual(clash.status, 409)
        self.assertEqual(clash.body["error"], "idempotency_key_reused")
        self.assertEqual(self.read(a)["code"], "ABC")

    def test_a_change_to_the_same_value_changes_nothing(self) -> None:
        a = self.new("ABC", "in")
        before = self.footprint(a)
        same = self.rotate(a, "in")
        self.assertEqual((same.status, same.body["changed"]), (200, False))
        self.assertEqual(self.set_code(a, "ABC").body["changed"], False)
        self.assertEqual(self.footprint(a), before)

    def test_an_unknown_project_is_not_found(self) -> None:
        answer = self.send("POST", "/registry/projects/t6p_nope/code", {"code": "A", "expected_revision": 1}, key="k")
        self.assertEqual(answer.status, 404)


class ReplacementBindingTests(CodesTestCase):
    def test_a_replacement_page_keeps_the_old_page_resolving_to_the_same_project(self) -> None:
        a = self.new("ABC", "in", page=PAGE)
        self.clock.advance(10)
        bound = self.bind(a, OTHER_PAGE)
        self.assertEqual(bound.status, 200)
        self.assertEqual(
            self.project(bound)["bindings"],
            [
                {"system": "notion", "page_id": PAGE, "bound_at": self.clock() - 10, "status": "legacy", "available": True},
                {"system": "notion", "page_id": OTHER_PAGE, "bound_at": self.clock(), "status": "active", "available": True},
            ],
        )
        for page in (PAGE_DASHED, OTHER_PAGE):
            found = self.send("GET", f"/registry/pages/{page}")
            self.assertEqual(found.status, 200)
            self.assertEqual(self.project(found)["id"], a["id"])
        entry = self.registry.audit(a["id"])[-1]
        self.assertEqual((entry.action, entry.before, entry.after), ("page_bound", {"page_id": PAGE}, {"page_id": OTHER_PAGE}))

    def test_an_old_page_never_moves_to_another_project(self) -> None:
        a = self.new(page=PAGE)
        self.bind(a, OTHER_PAGE)
        b = self.new()
        before = self.footprint(b)
        refused = self.bind(b, PAGE)
        self.assertEqual(refused.status, 409)
        self.assertEqual(refused.body, {"error": "page_already_bound", "project_id": a["id"]})
        self.assertEqual(self.footprint(b), before)
        self.assertEqual(self.send("POST", "/registry/projects", {"notion_page_id": PAGE}, key="again").status, 409)

    def test_binding_the_old_page_again_makes_it_the_active_one(self) -> None:
        a = self.new(page=PAGE)
        self.bind(a, OTHER_PAGE)
        self.bind(a, PAGE)
        statuses = {binding["page_id"]: binding["status"] for binding in self.read(a)["bindings"]}
        self.assertEqual(statuses, {PAGE: "active", OTHER_PAGE: "legacy"})
        self.assertEqual(self.bind(a, PAGE).body["changed"], False)

    def test_an_unbound_page_is_not_found(self) -> None:
        self.assertEqual(self.send("GET", f"/registry/pages/{PAGE}").status, 404)


class AccessAndOutageTests(CodesTestCase):
    def test_changes_need_the_write_grant(self) -> None:
        a = self.new("ABC")
        for secret in (WORKER_SECRET, READER_SECRET):
            self.assertEqual(self.rotate(a, "in", secret=secret).status, 403)
            self.assertEqual(self.set_code(a, "XYZ", secret=secret).status, 403)
            self.assertEqual(self.bind(a, PAGE, secret=secret).status, 403)
        self.assertEqual(self.send("GET", f"/registry/pages/{PAGE}", secret=WORKER_SECRET).status, 403)
        self.assertEqual(self.rotate(a, "in", secret="wrong").status, 401)
        self.assertEqual(self.read(a)["revision"], 1)

    def test_the_public_listener_serves_no_changes(self) -> None:
        a = self.new("ABC")
        headers = {"Authorization": "Bearer registry-writer-secret", "Idempotency-Key": "k"}
        body = b'{"rotation": "in", "expected_revision": 1}'
        answer = self.app.public(Request("POST", f"/registry/projects/{a['id']}/rotation", headers, body))
        self.assertEqual(answer.status, 404)
        self.assertEqual(self.read(a)["rotation"], "out")

    def test_an_unreachable_registry_blocks_changes_and_reads(self) -> None:
        a = self.new("ABC")
        for leftover in Path(self.directory.name).glob("registry.sqlite3*"):
            leftover.unlink()
        self.path.mkdir()  # the database can no longer be opened
        for answer in (
            self.send("POST", f"/registry/projects/{a['id']}/rotation", {"rotation": "in", "expected_revision": 1}, key="k"),
            self.send("POST", "/registry/projects", {}, key="k2"),
            self.send("GET", f"/registry/projects/{a['id']}"),
            self.send("GET", f"/registry/pages/{PAGE}"),
        ):
            self.assertEqual(answer.status, 503)
            self.assertEqual(answer.body, {"error": "registry_unavailable"})
