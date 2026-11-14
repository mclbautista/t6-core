"""Opaque T6 entity identifiers. IDs are permanent and carry their kind."""

from dataclasses import dataclass
from enum import Enum
import re
import secrets
from uuid import uuid4


class EntityKind(str, Enum):
    PROJECT = "p"
    DELIVERABLE = "v"
    PACKAGE = "k"
    VERSION = "r"
    TIMELINE = "t"
    PERSON = "n"
    BAY = "b"
    BOOKING = "g"
    MEDIA_LOCATION = "m"
    CONCERN = "c"
    APPROVAL = "a"
    JOB_ORDER = "j"
    DRIVE = "d"
    MILESTONE = "s"
    CHAT_LINK = "h"
    TRANSCRIPT = "x"


@dataclass(frozen=True)
class T6Id:
    kind: EntityKind
    value: str

    def __post_init__(self) -> None:
        length = 24 if self.kind is EntityKind.PROJECT else 32
        if not isinstance(self.kind, EntityKind) or not re.fullmatch(rf"t6{self.kind.value}_[0-9a-f]{{{length}}}", self.value):
            raise ValueError("invalid ID for entity kind")

    @classmethod
    def mint(cls, kind: EntityKind) -> "T6Id":
        suffix = secrets.token_hex(12) if kind is EntityKind.PROJECT else uuid4().hex
        return cls(kind, f"t6{kind.value}_{suffix}")

    @classmethod
    def parse(cls, value: str, kind: EntityKind) -> "T6Id":
        return cls(kind, value)

    def __str__(self) -> str:
        return self.value
