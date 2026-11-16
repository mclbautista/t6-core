"""The Media location kinds and roles, the Drive roles and the Person kinds, each listed once.

Everything that names them reads them from here: the verb input models (so the catalogue schema lists them), the CLI
choices, and the stores that check them.
"""
from __future__ import annotations

from typing import Literal, get_args

MediaKind = Literal['drive', 'nas-path', 'frameio-project', 'frameio-folder', 'resolve-project', 'timeline', 'instagantt-project']
MediaRole = Literal['master', 'backup', 'review', 'working', 'frameio-project']
DriveRole = Literal['master', 'backup', 'offline', 'swing', 'archive']
PersonKind = Literal['staff', 'freelancer', 'client', 'vendor']

MEDIA_KINDS: tuple[str, ...] = get_args(MediaKind)
MEDIA_ROLES: tuple[str, ...] = get_args(MediaRole)
DRIVE_ROLES: tuple[str, ...] = get_args(DriveRole)
PERSON_KINDS: tuple[str, ...] = get_args(PersonKind)
