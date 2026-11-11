"""Frame.io through one token holder (the `t6[frameio]` extra). Standard library only.

Callers use `ask` with a typed request. The Frame.io worker (`FrameioWorker`)
is the only process with Frame.io credentials.
"""

from t6.frameio.requests import (
    Approvals,
    BriefingFeed,
    Comments,
    CreateProjectFolders,
    Failed,
    FrameioRequest,
    FrameioResult,
    Ok,
    FrameioProjects,
    ShareLinks,
    Versions,
    ask,
)
from t6.frameio.worker import FrameioWorker, SignInRequired, Tokens

__all__ = [
    "Approvals",
    "BriefingFeed",
    "Comments",
    "CreateProjectFolders",
    "Failed",
    "FrameioRequest",
    "FrameioResult",
    "FrameioWorker",
    "Ok",
    "FrameioProjects",
    "ShareLinks",
    "SignInRequired",
    "Tokens",
    "Versions",
    "ask",
]
