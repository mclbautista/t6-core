"""AST guards: every Notion read outside NotionSource/client/sync/testing goes through NotionSource,
and every `freshness="live"` call is cataloged in an explicit allowlist with a one-line reason.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import unittest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "t6"
EXEMPT_DIRECT_MODULES = frozenset(
    {
        "notion/client.py",
        "notion/source.py",
        "notion/sync.py",
        "notion/testing.py",
    }
)
NOTION_READ_METHODS = frozenset(
    {
        "query_data_source",
        "retrieve_page",
        "retrieve_data_source",
        "list_block_children",
        "retrieve_user",
    }
)
RAW_CLIENT_CALLABLES = frozenset({"open_notion", "NotionClient", "AsyncNotionClient"})

# Every non-default `freshness="live"` call outside `notion/source.py` must be listed here
# as `(relpath_under_src_t6, enclosing_function, call_name): "one-line reason"`.
LIVE_READ_ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("notion/writes.py", "_write", "retrieve_page"): "Confirm saved page properties immediately after create_page or update_page.",
    ("notion/job_orders.py", "JobOrders.find_by_telegram_key", "query_data_source"): "Retry once live when mirrored telegram_key lookup misses.",
    ("notion/job_orders.py", "JobOrders.find_all_by_telegram_key", "query_data_source"): "Retry once live when mirrored telegram_key lookup misses.",
    ("notion/job_orders.py", "JobOrders.create", "get"): "Confirm saved Job Order and body immediately after create.",
    ("notion/job_orders.py", "JobOrders.ensure_body", "list_block_children"): "Read live page block body before appending missing body.",
    ("notion/job_orders.py", "JobOrders._write_body", "list_block_children"): "Confirm appended paragraph blocks immediately after body write.",
    ("notion/job_orders.py", "JobOrders._read", "list_block_children"): "Read live page block body (block bodies are not mirrored).",
    ("notion/reads.py", "DeliverableReader.for_project", "query_data_source"): "Retry once live when mirrored project deliverables lookup misses.",
    ("job_order.py", "find_forms", "list"): "Retry once live when mirrored duplicate-key lookup misses.",
    ("notion/meeting_notes.py", "MeetingNotes.create", "retrieve_page"): "Confirm saved Meeting Note immediately after create and block append.",
    ("notion/meeting_notes.py", "MeetingNotes.mark_sent", "retrieve_page"): "Confirm Meeting Note immediately after mark_sent update.",
    ("notion/schema.py", "refresh_snapshot", "retrieve_data_source"): "Refresh committed schema snapshot from live Notion data sources.",
    ("notion/schema.py", "check_schema", "retrieve_data_source"): "Check live Notion data source properties against schema snapshot.",
    ("notion/schema.py", "_audit_projects", "query_data_source"): "Audit live Projects pages against registry during schema check.",
    ("notion/projection.py", "ProjectProjection.sync_status", "retrieve_page"): "Check current Notion status baseline before projecting status change.",
    ("notion/projection.py", "ProjectProjection.sync_status", "retrieve_data_source"): "Resolve live Status select option name before projecting status.",
    ("notion/projection.py", "ProjectProjection.sync", "retrieve_page"): "Read live Project page before projecting T6 ID, Code and Rotation.",
    ("notion/projection.py", "ProjectProjection.reconcile", "retrieve_page"): "Reconcile live Notion page state for notion.page_changed event.",
    ("notion/jo_projection.py", "JobOrderProjection.reconcile", "retrieve_page"): "Reconcile live Job Order page state for notion.page_changed event.",
    ("notion/jo_projection.py", "JobOrderProjection.sync", "query_data_source"): "Deduplicate or recover Job Order create by T6 ID against live Notion.",
    ("notion/jo_projection.py", "JobOrderProjection.sync", "retrieve_page"): "Check current Notion status baseline before projecting Job Order.",
    ("notion/person_projection.py", "PersonProjection.reconcile", "retrieve_page"): "Reconcile live Contacts page state for notion.page_changed event.",
    ("notion/person_projection.py", "PersonProjection.sync", "retrieve_page"): "Check the shown T6 ID on the Contacts page before projecting.",
    ("notion/deliverable_projection.py", "DeliverableProjection._option", "retrieve_data_source"): "Resolve live Deliverable Status option name before projecting status.",
    ("notion/deliverable_projection.py", "DeliverableProjection.reconcile", "retrieve_page"): "Reconcile live Deliverable page state for notion.page_changed event.",
    ("notion/deliverable_projection.py", "DeliverableProjection.sync", "query_data_source"): "Deduplicate Deliverable create by T6 ID against live Notion.",
    ("notion/deliverable_projection.py", "DeliverableProjection.sync", "retrieve_page"): "Check current Deliverable name and status before projecting.",
    ("notion/editor.py", "editor", "retrieve_user"): "Resolve Notion webhook author user display name live.",
    ("notion/migration.py", "read_legacy_projects", "query_data_source"): "One-off legacy Projects migration reads live Notion rows.",
    ("media_migration.py", "read_external_links_once_for_migration", "query_data_source"): "One-off External Links migration reads live Notion rows.",
    ("fleet/bays.py", "Fleet._check_schema", "retrieve_data_source"): "Check live Bays schema before fleet sync or edit.",
    ("fleet/bays.py", "Fleet._moved", "retrieve_page"): "Confirm Bays row was not edited or moved right before write.",
    ("fleet/bays.py", "Fleet._write", "retrieve_page"): "Read back saved Machine row immediately after fleet update.",
    ("cli.py", "_migrate_people", "list"): "One-off Contacts migration reads live Notion rows.",
    ("cli.py", "_migrate_job_orders", "list"): "One-off Job Orders migration reads live Notion rows.",
    ("cli.py", "_migrate_bays", "query_data_source"): "One-off Bays migration reads live Notion rows.",
    ("cli.py", "_migrate_deliverables", "query_data_source"): "One-off Deliverables migration reads live Notion rows.",
    ("cli.py", "_import_swing", "read_swing_drives"): "One-off Swing Drives migration reads live Notion rows.",
    ("cli_jo.py", "_get", "get"): "Confirm live Job Order fields on jo get.",
    ("cli_jo.py", "_get", "find_by_telegram_key"): "Confirm live Job Order by Telegram key on jo get.",
    ("verbs.py", "deliverable_define.project_type", "retrieve_page"): "Read live Project Type when defining a Deliverable.",
    ("notion/mirror.py", "MirrorSyncer.rescan_source", "query_data_source"): "Full mirror rescan reads live Notion data source rows.",
    ("notion/mirror.py", "MirrorSyncer.rescan_source", "retrieve_data_source"): "Full mirror rescan reads live Notion data source metadata.",
    ("notion/mirror.py", "MirrorSyncer.poll_source", "query_data_source"): "Incremental mirror poll queries live Notion changes since high-water mark.",
    ("notion/mirror.py", "MirrorWriter.handle_page_changed", "retrieve_page"): "Refresh latest Notion page properties on notion.page_changed webhook event.",
    ("notion/mirror.py", "MirrorWriter.refresh_page", "retrieve_page"): "Refresh latest Notion page properties after API verb write.",
    ("server/verbs.py", "VerbAPI._dispatch", "PackageSpecReader"): "Read live Package Spec during deliverable.deliver.",
    ("server/verbs.py", "VerbAPI._meeting_notes", "retrieve_page"): "Verify live Meeting Note state before mutation and read back after write.",
    ("server/verbs.py", "VerbAPI._typed_read", "list"): "Retry once against live Notion when a mirrored read finds no match.",
    ("server/verbs.py", "VerbAPI._typed_read", "find_all_by_telegram_key"): "Confirm live Job Order by Telegram key on jo get.",
    ("server/verbs.py", "VerbAPI._typed_read", "retrieve_page"): "Confirm live Job Order parent data source on jo get.",
    ("server/verbs.py", "VerbAPI._typed_read.blocks", "list_block_children"): "Read live Meeting Note block body (block bodies are not mirrored).",
}


@dataclass(frozen=True)
class LiveFreshnessCall:
    relpath: str
    enclosing_function: str
    call_name: str
    lineno: int


def _call_target_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ast.unparse(func)


def _source_returning_functions(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            if ast.unparse(node.returns).strip() == "NotionSource":
                names.add(node.name)
    return names


def _is_source_expr(
    node: ast.expr,
    source_vars: set[str],
    source_attrs: set[str],
    source_funcs: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in source_vars
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return node.attr == "notion_source" or node.attr in source_attrs
        return False
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id in ("NotionSource", "as_source") or node.func.id in source_funcs
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "NotionSource" and node.func.attr == "from_client":
                return True
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "self" and node.func.attr in source_funcs:
                return True
    return False


def _class_source_attrs(cls: ast.ClassDef, source_funcs: set[str]) -> set[str]:
    candidate_attrs: set[str] = set()
    tainted_attrs: set[str] = set()
    for stmt in ast.walk(cls):
        if isinstance(stmt, ast.Assign):
            is_src = _is_source_expr(stmt.value, set(), candidate_attrs, source_funcs)
            for target in stmt.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    if is_src:
                        candidate_attrs.add(target.attr)
                    else:
                        tainted_attrs.add(target.attr)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            is_src = _is_source_expr(stmt.value, set(), candidate_attrs, source_funcs)
            if isinstance(stmt.target, ast.Attribute) and isinstance(stmt.target.value, ast.Name) and stmt.target.value.id == "self":
                if is_src or ast.unparse(stmt.annotation).strip() == "NotionSource":
                    candidate_attrs.add(stmt.target.attr)
                else:
                    tainted_attrs.add(stmt.target.attr)
    # Multi-target tuple unpacking like `self._notion, self._source = as_source(source), data_source_id`
    for stmt in ast.walk(cls):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Tuple) and isinstance(stmt.value, ast.Tuple):
            for target, value in zip(stmt.targets[0].elts, stmt.value.elts):
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    if _is_source_expr(value, set(), candidate_attrs, source_funcs):
                        candidate_attrs.add(target.attr)
                        tainted_attrs.discard(target.attr)
                    else:
                        tainted_attrs.add(target.attr)
    return candidate_attrs - tainted_attrs


def _collect_local_source_vars(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    source_attrs: set[str],
    source_funcs: set[str],
    inherited_vars: set[str] | None = None,
) -> set[str]:
    vars_ok: set[str] = set(inherited_vars or ())
    tainted: set[str] = set()
    for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs):
        if arg.annotation is not None and ast.unparse(arg.annotation).strip() == "NotionSource":
            vars_ok.add(arg.arg)
            tainted.discard(arg.arg)
        else:
            tainted.add(arg.arg)

    def walk_body(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            # Do not descend into nested functions/classes for local variable provenance.
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.Assign):
                if (
                    len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Tuple)
                    and isinstance(stmt.value, ast.Tuple)
                ):
                    for target, val in zip(stmt.targets[0].elts, stmt.value.elts):
                        if isinstance(target, ast.Name):
                            if _is_source_expr(val, vars_ok - tainted, source_attrs, source_funcs):
                                vars_ok.add(target.id)
                            else:
                                tainted.add(target.id)
                else:
                    is_src = _is_source_expr(stmt.value, vars_ok - tainted, source_attrs, source_funcs)
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            if is_src:
                                vars_ok.add(target.id)
                            else:
                                tainted.add(target.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.value is not None:
                    if _is_source_expr(stmt.value, vars_ok - tainted, source_attrs, source_funcs):
                        vars_ok.add(stmt.target.id)
                    else:
                        tainted.add(stmt.target.id)
                elif ast.unparse(stmt.annotation).strip() != "NotionSource":
                    tainted.add(stmt.target.id)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    if isinstance(item.optional_vars, ast.Name):
                        tainted.add(item.optional_vars.id)
                walk_body(stmt.body)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                if isinstance(stmt.target, ast.Name):
                    tainted.add(stmt.target.id)
                walk_body(stmt.body)
                walk_body(stmt.orelse)
            elif isinstance(stmt, ast.If):
                walk_body(stmt.body)
                walk_body(stmt.orelse)
            elif isinstance(stmt, ast.While):
                walk_body(stmt.body)
                walk_body(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                walk_body(stmt.body)
                for h in stmt.handlers:
                    walk_body(h.body)
                walk_body(stmt.orelse)
                walk_body(stmt.finalbody)

    walk_body(fn.body)
    return vars_ok - tainted


def find_direct_notion_read_violations(source: str, relpath: str) -> list[str]:
    """Return human-readable violations for direct NotionClient/open_notion reads in `source`."""
    if relpath in EXEMPT_DIRECT_MODULES:
        return []
    tree = ast.parse(source, filename=relpath)
    source_funcs = _source_returning_functions(tree)
    violations: list[str] = []

    def is_raw_client_call(expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Call):
            name = _call_target_name(expr.func)
            if name in RAW_CLIENT_CALLABLES:
                return name
        return None

    def scan_node(
        node: ast.AST,
        scope: str,
        source_vars: set[str],
        source_attrs: set[str],
        inside_notion_source_call: bool = False,
    ) -> None:
        if isinstance(node, ast.ClassDef):
            cls_scope = f"{scope}.{node.name}" if scope else node.name
            cls_attrs = _class_source_attrs(node, source_funcs)
            for child in node.body:
                scan_node(child, cls_scope, set(), cls_attrs, False)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_scope = f"{scope}.{node.name}" if scope else node.name
            fn_vars = _collect_local_source_vars(node, source_attrs, source_funcs, source_vars)
            for child in node.body:
                scan_node(child, fn_scope, fn_vars, source_attrs, False)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                raw = is_raw_client_call(item.context_expr)
                if raw is not None:
                    violations.append(f"{relpath}:{item.context_expr.lineno} ({scope or '<module>'}): `with {raw}(...)` bypasses NotionSource")
        if isinstance(node, ast.Call):
            target = _call_target_name(node.func)
            if isinstance(node.func, ast.Attribute) and node.func.attr in NOTION_READ_METHODS:
                if not _is_source_expr(node.func.value, source_vars, source_attrs, source_funcs):
                    violations.append(
                        f"{relpath}:{node.lineno} ({scope or '<module>'}): direct `{ast.unparse(node.func)}(...)` not on a NotionSource"
                    )
            if target in RAW_CLIENT_CALLABLES and not inside_notion_source_call:
                is_open_notion_def = relpath == "cli.py" and scope == "_open_notion" and target == "NotionClient"
                if not is_open_notion_def:
                    violations.append(
                        f"{relpath}:{node.lineno} ({scope or '<module>'}): raw `{target}(...)` outside NotionSource"
                    )
            is_source_ctor = target == "NotionSource" or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "NotionSource"
            )
            for subnode in ast.iter_child_nodes(node):
                scan_node(subnode, scope, source_vars, source_attrs, inside_notion_source_call or is_source_ctor)
            return
        for subnode in ast.iter_child_nodes(node):
            scan_node(subnode, scope, source_vars, source_attrs, inside_notion_source_call)

    scan_node(tree, "", set(), set(), False)
    return violations


def find_live_freshness_calls(source: str, relpath: str) -> tuple[list[LiveFreshnessCall], list[str]]:
    """Return `(live_calls, violations)` for `freshness` parameters/keywords outside `notion/source.py`."""
    if relpath == "notion/source.py":
        return [], []
    tree = ast.parse(source, filename=relpath)
    live_calls: list[LiveFreshnessCall] = []
    violations: list[str] = []

    def scan(node: ast.AST, scope: str) -> None:
        if isinstance(node, ast.ClassDef):
            cls_scope = f"{scope}.{node.name}" if scope else node.name
            for child in node.body:
                scan(child, cls_scope)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_scope = f"{scope}.{node.name}" if scope else node.name
            all_args = [*node.args.posonlyargs, *node.args.args]
            pos_defaults = [None] * (len(all_args) - len(node.args.defaults)) + list(node.args.defaults)
            for arg, default in zip(all_args, pos_defaults):
                if arg.arg == "freshness" and isinstance(default, ast.Constant) and default.value == "live":
                    violations.append(f"{relpath}:{node.lineno} ({fn_scope}): default `freshness='live'` is disallowed outside source.py")
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if arg.arg == "freshness" and isinstance(default, ast.Constant) and default.value == "live":
                    violations.append(f"{relpath}:{node.lineno} ({fn_scope}): default `freshness='live'` is disallowed outside source.py")
            for child in node.body:
                scan(child, fn_scope)
            return
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg != "freshness":
                    continue
                if isinstance(kw.value, ast.Constant):
                    if kw.value.value == "live":
                        live_calls.append(LiveFreshnessCall(relpath, scope or "<module>", _call_target_name(node.func), node.lineno))
                    elif kw.value.value != "mirror":
                        violations.append(f"{relpath}:{node.lineno} ({scope}): invalid freshness constant {kw.value.value!r}")
                elif not isinstance(kw.value, ast.Name):
                    violations.append(f"{relpath}:{node.lineno} ({scope}): non-literal freshness expression `{ast.unparse(kw.value)}`")
        for subnode in ast.iter_child_nodes(node):
            scan(subnode, scope)

    scan(tree, "")
    return live_calls, violations


class NotionReadGuardTests(unittest.TestCase):
    def test_no_direct_notion_client_reads_outside_exempt_modules(self) -> None:
        violations: list[str] = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            relpath = path.relative_to(SRC_ROOT).as_posix()
            violations.extend(find_direct_notion_read_violations(path.read_text(encoding="utf-8"), relpath))
        self.assertEqual(violations, [], "\n".join(violations))

    def test_live_freshness_calls_match_explicit_allowlist(self) -> None:
        for key, reason in LIVE_READ_ALLOWLIST.items():
            self.assertIsInstance(reason, str, key)
            self.assertTrue(reason.strip(), f"empty reason for {key}")
            self.assertNotIn("\n", reason, f"multi-line reason for {key}")

        found_calls: list[LiveFreshnessCall] = []
        violations: list[str] = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            relpath = path.relative_to(SRC_ROOT).as_posix()
            calls, file_violations = find_live_freshness_calls(path.read_text(encoding="utf-8"), relpath)
            found_calls.extend(calls)
            violations.extend(file_violations)

        self.assertEqual(violations, [], "\n".join(violations))
        found_keys = {(call.relpath, call.enclosing_function, call.call_name) for call in found_calls}
        self.assertEqual(found_keys, set(LIVE_READ_ALLOWLIST))

    def test_guard_catches_planted_direct_reads_and_unlisted_live_reads(self) -> None:
        bad_client_param = 'def bad(client):\n    return client.retrieve_page("x")\n'
        self.assertTrue(find_direct_notion_read_violations(bad_client_param, "notion/example.py"))

        bad_typed_client = 'def bad(client: NotionClient):\n    return client.retrieve_page("x")\n'
        self.assertTrue(find_direct_notion_read_violations(bad_typed_client, "notion/example.py"))

        bad_open_notion = 'def bad(open_notion, t):\n    with open_notion(t) as c:\n        return c.query_data_source("ds")\n'
        self.assertTrue(find_direct_notion_read_violations(bad_open_notion, "notion/example.py"))

        bad_write_client_read = (
            'def bad(source: NotionSource):\n'
            '    with source.open_write_client() as c:\n'
            '        return c.retrieve_page("x")\n'
        )
        self.assertTrue(find_direct_notion_read_violations(bad_write_client_read, "notion/example.py"))

        bad_live_default = 'def sneaky(source: NotionSource, *, freshness: str = "live"):\n    return source.retrieve_page("x", freshness=freshness)\n'
        _, live_violations = find_live_freshness_calls(bad_live_default, "notion/example.py")
        self.assertTrue(live_violations)

        unlisted_live = 'def sneaky(source: NotionSource):\n    return source.retrieve_page("x", freshness="live")\n'
        calls, live_violations = find_live_freshness_calls(unlisted_live, "notion/example.py")
        self.assertEqual(live_violations, [])
        self.assertEqual({(c.relpath, c.enclosing_function, c.call_name) for c in calls} - set(LIVE_READ_ALLOWLIST), {("notion/example.py", "sneaky", "retrieve_page")})


if __name__ == "__main__":
    unittest.main()
