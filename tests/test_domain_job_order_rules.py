"""Job Order studio rules, ported from the watchdog and notion-reports tests."""

import unittest

from t6.domain import JobOrderStatus
from t6.domain.job_order_rules import (
    MFG_SHUTTLES,
    WATERMARK_DEFAULT,
    JobOrderStatusGroup,
    canonical_name,
    health_flags,
    is_active,
    is_deliverable_code,
    is_mfg_shuttle,
    jo_id,
    jo_key,
    needs_link_settings,
    parse_header,
    parse_number,
    parse_telegram_key,
    status_group,
    telegram_key,
)


class HeaderTests(unittest.TestCase):
    def test_accepted_number_forms_keep_the_digits_as_posted(self) -> None:
        for text, digits in {
            "JO #74": "74",
            "JO No. 15": "15",
            "#0155": "0155",
            "JOB ORDER 27": "27",
            "JO01": "01",
            "JOB ORDER - 033": "033",
        }.items():
            with self.subTest(text=text):
                self.assertEqual(parse_number(text), digits)
        self.assertIsNone(parse_number("JO #x"))
        self.assertIsNone(parse_number(None))

    def test_canonical_name_keeps_leading_zeros(self) -> None:
        header = parse_header("SOMSOM - TOC - #0155")
        self.assertEqual(canonical_name(header, "x"), "SOMSOM - TOC - JO #0155")
        assert header is not None
        self.assertEqual((header.jo_number, header.jo_number_int), ("#0155", 155))

    def test_department_alias_and_case(self) -> None:
        self.assertEqual(canonical_name(parse_header("ALTA-TO-JO04"), "x"), "ALTA - TOC - JO #04")
        self.assertEqual(canonical_name(parse_header("MFG - toc - JO #74"), "x"), "MFG - TOC - JO #74")

    def test_aliases_can_be_given(self) -> None:
        self.assertEqual(canonical_name(parse_header("ALTA-XX-JO04", {"xx": "AE"}), "x"), "ALTA - AE - JO #04")

    def test_a_validated_department_is_used_verbatim(self) -> None:
        header = parse_header("MFG - TO - JO #74")
        self.assertEqual(canonical_name(header, "x", department=" toc "), "MFG -  toc  - JO #74")

    def test_label_and_backslash_are_dropped(self) -> None:
        header = parse_header("JO CODE: INA-TOC-30")
        assert header is not None
        self.assertEqual((header.project, header.department, header.number), ("INA", "TOC", "30"))
        escaped = parse_header("\\LS - OE - JO #65")
        assert escaped is not None
        self.assertEqual(escaped.project, "LS")

    def test_an_unparseable_header_falls_back(self) -> None:
        for raw in (None, "", "just text", "MFG - TOC", "MFG - TOC - later"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_header(raw))
        self.assertEqual(canonical_name(None, "first line"), "first line")


class KeyTests(unittest.TestCase):
    def test_one_counter_per_project_across_departments(self) -> None:
        self.assertEqual(jo_key("page-1", 74), "page-1|74")
        self.assertIsNone(jo_key(None, 74))
        self.assertIsNone(jo_key("page-1", None))
        self.assertIsNone(jo_key("", 3))

    def test_jo_id_has_one_hash(self) -> None:
        self.assertEqual(jo_id(["MFG"], "#74"), "MFG#74")
        self.assertEqual(jo_id(["MFG"], "74"), "MFG#74")
        self.assertEqual(jo_id(["A", "B"], " # 5 "), "A/B#5")
        self.assertEqual(jo_id([], "7"), "no project#7")
        self.assertEqual(jo_id(["MFG"], None), "MFG#?")
        self.assertEqual(jo_id(["MFG"], "#"), "MFG#?")

    def test_telegram_key_round_trips(self) -> None:
        self.assertEqual(telegram_key(-100123, 42), "-100123:42")
        self.assertEqual(parse_telegram_key("-100123:42"), (-100123, 42))
        for bad in (None, "", "42", "a:b", "1:"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_telegram_key(bad))


class FormatterRuleTests(unittest.TestCase):
    def test_deliverable_codes(self) -> None:
        for good in ("FTR", "tlr", "TSR", "SHR", "Ep07", "EP07-08"):
            with self.subTest(good=good):
                self.assertTrue(is_deliverable_code(good))
        for bad in ("Ep7", "Ep07-8", "Ep07-08-09", "FTRS", "", "Feature"):
            with self.subTest(bad=bad):
                self.assertFalse(is_deliverable_code(bad))

    def test_defaults_and_shuttles(self) -> None:
        self.assertEqual(WATERMARK_DEFAULT, "none")
        self.assertEqual(MFG_SHUTTLES, ("MFG Shuttle 01", "MFG Shuttle 02", "MFG Shuttle 03", "MFG Shuttle 04"))
        self.assertTrue(is_mfg_shuttle("MFG Shuttle 04"))
        self.assertFalse(is_mfg_shuttle("MFG Shuttle 05"))

    def test_link_settings_travel_with_mode_of_transfer(self) -> None:
        self.assertTrue(needs_link_settings("Fromsmash", None))
        self.assertTrue(needs_link_settings("Fromsmash", "  "))
        self.assertFalse(needs_link_settings("Fromsmash", "dl with pw"))
        self.assertFalse(needs_link_settings(None, None))


class StatusTests(unittest.TestCase):
    def test_status_groups(self) -> None:
        table = {
            "Needs Intake Review": JobOrderStatusGroup.NOT_STARTED,
            "Pending": JobOrderStatusGroup.NOT_STARTED,
            "In progress": JobOrderStatusGroup.STARTED,
            "Needs Completion Review": JobOrderStatusGroup.STARTED,
            "Done": JobOrderStatusGroup.STARTED,
            "Cancelled": JobOrderStatusGroup.CANCELLED,
            "Something new": None,
            None: None,
        }
        for status, group in table.items():
            with self.subTest(status=status):
                self.assertEqual(status_group(status), group)

    def test_done_and_cancelled_are_not_active(self) -> None:
        self.assertFalse(is_active(JobOrderStatus.DONE))
        self.assertFalse(is_active("Cancelled"))
        self.assertTrue(is_active("In progress"))
        self.assertTrue(is_active(None))

    def test_health_flags_show_blocked_delayed_and_urgent(self) -> None:
        self.assertEqual(health_flags("🔴 Blocked"), ("Blocked",))
        self.assertEqual(health_flags("⚠️ Delayed"), ("Delayed",))
        self.assertEqual(health_flags("🔥 Urgent"), ("Urgent",))
        self.assertEqual(health_flags("🔴 Blocked ⚠️ Delayed"), ("Blocked", "Delayed"))
        self.assertEqual(health_flags("✅ OK"), ())
        self.assertEqual(health_flags(None), ())


if __name__ == "__main__":
    unittest.main()
