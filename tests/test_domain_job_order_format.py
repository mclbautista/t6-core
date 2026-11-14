"""The Job Order formatter rules: header, date line, field order, defaults and findings."""

from datetime import date
import unittest

from t6.domain.job_order_format import format_job_order

DAY = date(2026, 6, 22)

DRAFT = """ORC - toc - JO No. 062
June 1 / Someday
Title: Orchid FTR
Task: Download VFX Renders from Frameio
Purpose: For post grade
Source: @url:`https://f.io/x`
Mode of Transfer: Frameio transfer
Link Settings: view only
Deadline: June 22 / Monday 5PM
Note: Should be the following shots
ORC_048_0156
ORC_048_0160
Assigned: @asha Mika
cc: @lena

Login: user / pass
"""


def codes(text: str, day: date = DAY) -> list[str]:
    return [finding.code for finding in format_job_order(text, day).findings]


class FormatTests(unittest.TestCase):
    def test_a_complete_draft_gets_the_studio_layout(self) -> None:
        result = format_job_order(DRAFT, DAY)
        self.assertEqual(result.findings, ())
        self.assertEqual(
            result.text,
            """ORC - TOC - JO #062
June 22 / Monday

Title: Orchid FTR
Task: Download VFX Renders from Frameio
Purpose: For post grade
Source: @url:`https://f.io/x`
Watermark: none
Mode of Transfer: Frameio transfer
Link Settings: view only
Deadline: June 22 / Monday 5PM
Status:
Pending:
Note: Should be the following shots
ORC_048_0156
ORC_048_0160

Assigned: @asha Mika
cc: @lena

Login: user / pass
""",
        )

    def test_header_number_forms_are_rewritten(self) -> None:
        for header in ("ORC - TOC - JO #62", "ORC - TO - JO 62", "ORC - toc - JOB ORDER - 62", "JO CODE: ORC - TOC - #62"):
            with self.subTest(header=header):
                self.assertEqual(format_job_order(header, DAY).text.splitlines()[0], "ORC - TOC - JO #62")

    def test_the_date_line_is_computed_never_typed(self) -> None:
        self.assertEqual(format_job_order("A - TOC - JO #1\nJuly 23 / Monday", date(2026, 7, 23)).text.splitlines()[1], "July 23 / Thursday")

    def test_labels_keep_the_writers_wording(self) -> None:
        text = format_job_order("A - OE - JO #1\nSource Drive: MG OCF 03A\nTransfer Mode(s): Fromsmash\nInstructions: do it\nTasks: x", DAY).text
        for line in ("Source Drive: MG OCF 03A", "Transfer Mode(s): Fromsmash", "Instructions: do it", "Tasks: x"):
            self.assertIn(line, text)

    def test_watermark_defaults_to_none_and_status_and_pending_are_blank(self) -> None:
        text = format_job_order("A - OE - JO #1\nStatus: Done\nWatermark:", DAY).text
        self.assertIn("Watermark: none\n", text)
        self.assertIn("Status: Done", text)  # a given Status is kept; only an absent one is blank
        self.assertIn("Pending:\n", text)

    def test_nothing_missing_is_invented(self) -> None:
        text = format_job_order("A - OE - JO #1", DAY).text
        for label in ("Title:", "Task:", "Purpose:", "Assigned:", "Deadline:", "Subtitle:"):
            self.assertNotIn(label, text)

    def test_findings(self) -> None:
        cases = [
            ("not a header\nTitle: X FTR\nTask: t\nPurpose: p\nAssigned: a", ["header_unparsed"]),
            ("A - OE - JO #1\nTask: t\nPurpose: p\nAssigned: a", ["missing_title"]),
            ("A - OE - JO #1\nTitle: Some Show\nTask: t\nPurpose: p\nAssigned: a", ["no_deliverable_code"]),
            ("A - OE - JO #1\nTitle: Show Ep07-08\nTask: t\nPurpose: p\nAssigned: a", []),
            ("A - OE - JO #1\nTitle: S TLR\nTask: t\nPurpose: p\nMode of Transfer: Fromsmash\nAssigned: a", ["link_settings_missing"]),
            ("A - OE - JO #1\nTitle: S FTR\nTask: t\nPurpose: p\nSource Drive: MFG Shuttle 05\nAssigned: a", ["mfg_shuttle_out_of_range"]),
            ("A - OE - JO #1\nTitle: S FTR\nTask: t\nPurpose: p\nSource Drive: MFG Shuttle 03\nAssigned: a", []),
            ("", ["empty_draft"]),
        ]
        for draft, expected in cases:
            with self.subTest(draft=draft):
                self.assertEqual(codes(draft), expected)


if __name__ == "__main__":
    unittest.main()
