"""Privacy tools for text that leaves T6.

``withhold`` strips credentials one way. Nothing it returns can restore them.
``pseudonymize`` swaps registry names for placeholders, and its ``Session``
puts them back.
"""

from t6.privacy.masking import Leak, Masked, Person, Project, Registry, Session, Unresolved, pseudonymize
from t6.privacy.withhold import MASKER_VERSION, WITHHELD, Withheld, withhold

__all__ = [
    "MASKER_VERSION",
    "WITHHELD",
    "Leak",
    "Masked",
    "Person",
    "Project",
    "Registry",
    "Session",
    "Unresolved",
    "Withheld",
    "pseudonymize",
    "withhold",
]
