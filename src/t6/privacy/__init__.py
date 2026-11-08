"""Privacy tools for text that leaves T6.

``withhold`` strips credentials one way. Nothing it returns can restore them.
"""

from t6.privacy.withhold import MASKER_VERSION, WITHHELD, Withheld, withhold

__all__ = ["MASKER_VERSION", "WITHHELD", "Withheld", "withhold"]
