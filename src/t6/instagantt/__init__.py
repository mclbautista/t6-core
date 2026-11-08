"""Read Instagantt Standalone exports. Install with `pip install 't6[instagantt]'`.

- `parse_export` / `read_export`: CSV export to `Timeline`s of `Task`s.
- `link_timelines`: tie each Timeline to its Notion Project through External
  Links; unmapped projects are reported, not guessed.

Read only: writes to Instagantt go through the browser skill, and CSV import
is never used to sync.
"""

from t6.instagantt.linking import link_timelines
from t6.instagantt.model import (
    InstaganttError,
    LinkedTimeline,
    LinkReport,
    Task,
    Timeline,
    Unmapped,
)
from t6.instagantt.reader import parse_export, read_export

__all__ = [
    "InstaganttError",
    "LinkReport",
    "LinkedTimeline",
    "Task",
    "Timeline",
    "Unmapped",
    "link_timelines",
    "parse_export",
    "read_export",
]
