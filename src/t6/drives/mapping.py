"""Checked sheet Project names, kept independently of reloadable Drive data."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from t6 import verbflow
from t6.drives import Ref
from t6.domain import Project
from t6.drives.sheets import SheetSource
from t6.lookup import Index, Lookup, Normalizers, Policy
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Outcome, Refused


class MappingEntry(BaseModel):
    name: str
    project: str | None
    certain: bool = False
    project_name: str | None = None


def project_key(name: str) -> str:
    return Normalizers().text(name)


def project_names(source: SheetSource, tracker: SheetSource | None = None) -> set[str]:
    """Per-project tab names and distinct PROJECT NAME values in tracker tabs."""
    names = {name for name in source.tabs() if name.strip().upper() != 'PROJECT TEMPLATE'}
    if tracker is not None:
        for rows in tracker.tabs().values():
            column = None
            for row in rows:
                headers = [str(value or '').strip().upper() for value in row]
                if 'PROJECT NAME' in headers:
                    column = headers.index('PROJECT NAME')
                elif column is not None and len(row) > column and row[column] is not None:
                    name = str(row[column]).strip()
                    if name:
                        names.add(name)
    return names


def propose_mapping(source: SheetSource, tracker: SheetSource | None, projects: tuple[Project, ...],
                    file: str) -> list[dict[str, object]]:
    lookup = Lookup(Index(projects=projects))
    policy = Policy(llm=False)
    entries: list[dict[str, object]] = []
    for name in sorted(project_names(source, tracker)):
        ranked = lookup.project(None, name=name).candidates
        exact = [candidate for candidate in ranked if candidate.score == 1.0]
        candidate = ranked[0].entity if ranked else None
        certain = False
        if exact:
            candidate, certain = exact[0].entity, len(exact) == 1
        else:
            code = lookup.project(name)
            if code.entity is not None or code.candidates:
                candidate = code.entity or code.candidates[0].entity
                certain = not code.needs_review
            elif ranked:
                runner_up = ranked[1].score if len(ranked) > 1 else 0.0
                certain = ranked[0].score >= policy.fuzzy_accept and round(ranked[0].score - runner_up, 6) >= policy.margin
        entries.append(MappingEntry(name=name, project=candidate.t6_id if candidate else None,
                                     certain=certain, project_name=candidate.name if candidate else None).model_dump())
    Path(file).write_text(json.dumps(entries, indent=2) + '\n')
    return entries


def load_mapping(db: sqlite3.Connection) -> dict[str, str]:
    return dict(db.execute('SELECT name, project_id FROM drive_project_mapping'))


def project_ref(name: str, mapping: dict[str, str]) -> Ref:
    project = mapping.get(project_key(name))
    return Ref(page_id=project) if project else Ref(text=name)


def apply_mapping(registry: SqliteProjectRegistry, tokens: PreviewTokens, file: str, *,
                  actor: str, idempotency_key: str | None, confirm: str | None) -> Outcome:
    entries: list[MappingEntry] = TypeAdapter(list[MappingEntry]).validate_json(Path(file).read_text())
    checked: dict[str, str | None] = {}
    for entry in entries:
        key = project_key(entry.name)
        if not key:
            return Refused(rule='mapping', reason='A sheet Project name must not be blank')
        if key in checked and checked[key] != entry.project:
            return Refused(rule='mapping', reason=f'Conflicting mappings for {entry.name}')
        checked[key] = entry.project
    request = verbflow.begin(tokens, 'drive mapping apply', actor,
                             {'file': str(Path(file).resolve()), 'mapping': checked}, idempotency_key, confirm)
    if isinstance(request, Refused):
        return request
    with registry._transaction() as db:
        settled = verbflow.settle(db, tokens, request)
        if settled:
            return settled
        for project in checked.values():
            if project is not None and not db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone():
                return Refused(rule='project', reason=f'Unknown registered Project: {project}')
        result = {'mapping': [entry.model_dump() for entry in entries]}
        if not request.nonce:
            return verbflow.issue(tokens, request, result)[0]
        for key, project in checked.items():
            if project is None:
                db.execute('DELETE FROM drive_project_mapping WHERE name=?', (key,))
            else:
                db.execute('INSERT INTO drive_project_mapping VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET project_id=excluded.project_id',
                           (key, project))
        return verbflow.finish(db, request, result)
