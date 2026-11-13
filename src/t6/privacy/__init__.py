"""Privacy tools for text that leaves T6.

``withhold`` strips credentials one way. Nothing it returns can restore them.
``pseudonymize`` swaps registry names for placeholders, and its ``Session``
puts them back. ``MaskOptions`` sets what each task hides, with the presets
``NAMES`` and ``STRICT``. ``Scrubber`` removes exact, configured secret values
from text and log lines.
"""

from t6.privacy.masking import (
    NAMES,
    PRESETS,
    STRICT,
    Leak,
    Masked,
    MaskOptions,
    Person,
    Project,
    Registry,
    Session,
    Unresolved,
    pseudonymize,
)
from t6.privacy.scrub import REDACTED, Scrubber, ScrubbingFormatter
from t6.privacy.withhold import MASKER_VERSION, WITHHELD, Withheld, withhold

__all__ = [
    "REDACTED",
    "MASKER_VERSION",
    "NAMES",
    "PRESETS",
    "STRICT",
    "WITHHELD",
    "Leak",
    "MaskOptions",
    "Masked",
    "Person",
    "Project",
    "Registry",
    "Scrubber",
    "ScrubbingFormatter",
    "Session",
    "Unresolved",
    "Withheld",
    "pseudonymize",
    "withhold",
]
