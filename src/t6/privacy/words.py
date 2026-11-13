"""Ordinary English words, so masking can leave them readable.

A name word, project code or project name that is also an ordinary word ("Will", "Joy", a code such as
"TWO", a project called "Home") would blank the notes if it were hidden everywhere. The `strict` masking
preset hides such a word only when it is written capitalised or in capitals, and leaves ordinary project
names visible. These lists are ported from the notion-reports redactor.
"""

from __future__ import annotations

PLAIN_WORDS = frozenset("""
a an the and or but nor so yet of on in at by for to from with without into onto over under after before
during until till since while about around within up down out off as than then that this these those
there here is are was were be been being am do does did has have had having will would shall should can
could may might must not no nor now still also already just only still again yet soon later next last
first new old it its it's they them their he she his her we our you your i my me us who whom whose which
what when where why how if else both each either every any all some more most less least many much few
other another same such very too per via vs per own again further once back away ahead among between
against along across
""".split())
"""Grammar words: articles, prepositions, pronouns, auxiliaries."""

COMMON_WORDS = frozenset("""
add added approve approved ask asked awaiting back blocked book booked bring call cancel change check
checked clear client clients close closed colour color come comment complete completed confirm confirmed
continue cut date dates day days deadline deadlines deliver delivered delivery delay delayed done draft
due early edit edits editor end ended episode episodes expect expected extend extended feedback fine
finish finished fix fixed follow get give go going good grade grading hand handed happen happening
health hold held issue issues item items keep late launch lead leads lock locked look make mark meeting
meetings miss missed missing move moved moves need needed needs note notes ok okay open order pass
pending plan planned plans post pass problem problems proceed progress project projects push pushed put
question questions ready receive received release released remain remains remaining request requested
resolve resolved review reviewed risk risks run running schedule scheduled send sent set share shared
shift shifted show shown slip slipped stage start started status step steps stop stopped submit
submitted submission team tell testing time timeline today tomorrow track tracking turn update updated
upcoming wait waiting want week weeks work working yesterday
""".split())
"""Everyday status vocabulary."""

EXTRA_ORDINARY = frozenset("""
one two three four five six seven eight nine ten zero joy grace hope faith mark rose art bill dawn june
april may will wade dean earl frank grant hunter lane max miles pat peace rich sky summer victor
""".split())
"""Ordinary words that are also given names or codes."""

PROJECT_PLAIN_WORDS = frozenset("""
home live show news work play life world time game night light spring winter autumn fall water fire
earth stone river ocean story tale road city town farm park club team crew talk radio kids family
""".split())
"""Ordinary words a project may be named after. A Project named only so is not hidden by `strict`."""

MONTH_WORDS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)
"""Month names and abbreviations. A name word that is a month is not hidden as a name word."""


def is_ordinary(word: str) -> bool:
    """True if ``word`` is an ordinary English word, a month name, or a common given name that is one."""
    low = word.casefold()
    return low in PLAIN_WORDS or low in COMMON_WORDS or low in EXTRA_ORDINARY or low in MONTH_WORDS
