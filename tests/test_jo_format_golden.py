"""Golden tests: Job Orders shaped like the `job-order-formatter` skill's examples pass the formatter with no findings (spec #791, ticket #799).

The examples use invented Projects and people. When `T6_SKILLS_DIR` points at the skills folder, the real
skill's own examples are checked too.
"""
import os
import re
import unittest
from datetime import date, datetime
from pathlib import Path

from t6.domain.job_order_format import format_job_order

DAY = date(2026, 7, 23)

SIMPLE = """HBR - TOC - JO #0095
July 23 / Thursday

Title: Harbor Lights FTR
Task: Download the following mats & copy to the grading drive
Purpose: For grading
Source Material: Sample reel (attached ss)
Watermark: none
Mode of Transfer: Reel to raid and grading drive
Link Settings: n/a, direct copy
Deadline: July 23, 6:30PM
Status:
Pending:
Assigned: @mayar
cc: @leaong
"""

EPISODIC = """HBR - AE - JO #0096
July 23 / Thursday

Title: Harbor Lights Ep07
Task: Export and upload Ep07 audio mix to the share
Purpose: For client review
Source: PIX share
Watermark: client
Mode of Transfer: Frame.io link
Link Settings: view only
Deadline: July 24, 10AM
Status:
Pending:
Notes: Paki-send na lang po ang link sa thread pag tapos na.
Assigned: @mayar
cc: @leaong
"""

SUBTITLED = """HBR - COOR - JO #0097
July 23 / Thursday

Title: Harbor Lights TLR
Task: Burn in subtitles and deliver the trailer
Purpose: For festival submission
Source: Archive drive
Watermark: none
Subtitle: English, burned in
Mode of Transfer: Transfer service
Link Settings: n/a
Deadline: July 25, 5PM
Status:
Pending:
Assigned: @mayar
cc: @leaong
"""


def posted_on(example: str) -> date:
    """The day an example's own date line (`July 23 / Thursday`) names, in the nearest year where the weekday agrees."""
    month_day, weekday = (part.strip() for part in example.splitlines()[1].split('/'))
    for year in range(2026, 2036):
        day = datetime.strptime(f'{month_day} {year}', '%B %d %Y').date()
        if day.strftime('%A') == weekday:
            return day
    raise AssertionError(f'no year matches {example.splitlines()[1]!r}')


class GoldenTests(unittest.TestCase):
    def check(self, text: str, day: date = DAY) -> None:
        result = format_job_order(text, day)
        self.assertEqual([f.code for f in result.findings], [], result.text)

    def test_a_simple_file_transfer_has_no_findings(self) -> None:
        self.check(SIMPLE)

    def test_an_episodic_upload_has_no_findings(self) -> None:
        self.check(EPISODIC)

    def test_a_subtitled_trailer_has_no_findings(self) -> None:
        self.check(SUBTITLED)

    @unittest.skipUnless(os.environ.get('T6_SKILLS_DIR'), 'T6_SKILLS_DIR is not set')
    def test_the_real_skill_examples_have_no_findings(self) -> None:
        skill = Path(os.environ['T6_SKILLS_DIR']) / 'job-order-formatter' / 'SKILL.md'
        examples = re.findall(r'### Example \d+[^\n]*\n\n```\n(.*?)\n```', skill.read_text(encoding='utf-8'), re.DOTALL)
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example=example.splitlines()[0]):
                self.check(example, posted_on(example))
