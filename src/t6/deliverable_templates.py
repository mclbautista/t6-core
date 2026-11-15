"""Milestone templates and their derived Notion Deliverable Status."""
from typing import Any

FILM_PIPELINE = ('ingested', 'synced', 'piclock', 'conform done', 'master approved', 'QC passed', 'delivered', 'archiving', 'archived')
FILM_TRACKS = {'color': 'color delivered', 'VFX': 'VFX approved', 'GFX/titles': None,
               'sound': 'printmaster in', 'music': None, 'captions': None}
TEMPLATES = {
    'film': FILM_PIPELINE,
    'color_only': ('Received', 'Graded Main', 'Patches', 'Pushed', 'Aired', 'Archival Fixes', 'archiving', 'archived'),
    'sound_only': ('ADR Assessment', 'Locked', 'ADR Done', 'Dx Complete', 'Mix Passes', 'printmaster in', 'archiving', 'archived'),
    'tvc': ('ingested', 'piclock', 'master approved', 'delivered', 'archiving', 'archived'),
    'daily': ('Precut', 'All Seq In', 'Locked', 'Online', 'Mastering', 'Pushed', 'QC Fixes', 'archiving', 'archived'),
}
PROJECT_TYPES = {
    **dict.fromkeys(('Feature Film', 'Theatrical Feature', 'Documentary Feature', 'Festival Feature', 'MMFF Feature', "Int'l Co-Prod Feature", 'Streamer Originals Film', 'Short Film', 'Limited Series', 'Limited Documentary Series', 'Streamer Originals Series'), 'film'),
    **dict.fromkeys(('Serial TV', 'Serial Reality'), 'daily'),
    'Serial TV - Color': 'color_only', 'TVC': 'tvc', 'Docu Ad': 'tvc',
}
STATUSES = {'ingested': 'Waiting for Lock', 'synced': 'Waiting for Lock',
            'piclock': 'In Progress', 'conform done': 'In Progress',
            'master approved': 'Mastering', 'QC passed': 'QC Passed',
            'delivered': 'Delivered', 'archiving': 'Archiving', 'archived': 'Archived'}


def stage(template: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    pipeline = TEMPLATES[template]
    recorded = {fact['milestone'] for fact in facts}
    position = next((name for name in reversed(pipeline) if name in recorded), None)
    tracks = ({track: 'complete' if milestone and milestone in recorded else 'pending'
               for track, milestone in FILM_TRACKS.items()} if template == 'film' else {})
    return {'template': template, 'position': position, 'tracks': tracks}


def status(template: str, facts: list[dict[str, Any]]) -> str:
    position = stage(template, facts)['position']
    if position is None:
        return 'Staged'
    if template != 'film' and position == TEMPLATES[template][0]:
        return 'In Progress'
    return STATUSES.get(position, 'In Progress')
