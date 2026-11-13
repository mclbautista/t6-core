"""Masking options and presets. The `strict` cases are ported from the notion-reports redactor's tests.

Every name here is made up.
"""

import unittest

from t6.privacy import (
    NAMES,
    STRICT,
    MaskOptions,
    Person,
    Project,
    Registry,
    Unresolved,
    pseudonymize,
)

UUID = "01234567-89ab-cdef-0123-456789abcdef"
OWN = ("Target Show", "OWN")


def strict(text: str, registry: Registry, allow: tuple[str, ...] = OWN) -> str:
    return pseudonymize(text, registry, allow, STRICT)[0].text


class StrictContractTests(unittest.TestCase):
    """The reports redactor's existing cases, through the t6 interface."""

    def test_embedded_known_values_in_notes_are_protected(self) -> None:
        raw = (
            "• [Dario Quill] and will wait for [Mira]; ids [111],111. and 111, "
            "@dario_handle tg://user?id=111 notion.so/" + UUID + " " + UUID + "; c-known p-other [Other Show] [AB]; Zephyr waits"
        )
        registry = Registry(
            people=[Person("Dario Quill", aliases=("Will",), handles=("dario_handle",)), Person("Mira")],
            projects=[Project("Other Show", "AB"), Project("Target Show", "OWN")],
            ids=("c-known", "c-mini", "111", "p-other"),
        )
        masked = strict(raw, registry)
        self.assertEqual(
            masked,
            "• [[person 1]] and [person 2] wait for [[person 3]]; ids [[id 1]],[id 1]. and [id 1], "
            "[id 2] [id 3] [id 4] [id 5]; [id 6] [id 7] [[project 1]] [[project 2]]; Zephyr waits",
        )
        for private in ("Dario", "Quill", "Mira", "will", "111", "@dario_handle", "tg://", UUID, "notion.so", "c-known", "p-other", "Other Show", "AB"):
            self.assertNotIn(private, masked)
        # A name that is not registered is not recognized as a name.
        self.assertIn("Zephyr", masked)

    def test_known_full_names_and_aliases_are_hidden_in_all_casing(self) -> None:
        for people, text, expected in (
            ([Person("Will")], "will WILL Will", "[person 1] [person 1] [person 1]"),
            ([Person("Dario Quill", aliases=("Will",))], "will WILL Will", "[person 1] [person 1] [person 1]"),
            ([Person("Dario Quill")], "[dario quill] (DARIO QUILL) Dario Quill", "[[person 1]] ([person 1]) [person 1]"),
        ):
            with self.subTest(text=text):
                self.assertEqual(strict(text, Registry(people=people)), expected)

    def test_known_short_project_codes_and_page_ids_are_protected(self) -> None:
        registry = Registry(projects=[Project("Zephyr Hour", "ZH")], ids=("p-other",))
        self.assertEqual(strict("[Zephyr Hour] (ZH) p-other", registry), "[[project 1]] ([project 2]) [id 1]")

    def test_ordinary_and_short_project_names_stay_visible_but_codes_and_ids_do_not(self) -> None:
        registry = Registry(
            projects=[Project("Home", "HM"), Project("Day", "D"), Project("Live"), Project("Zephyr")],
            ids=("p-home", "p-day"),
        )
        text = "Home and home, Day, Live; Zephyr; HM; D; p-home"
        self.assertEqual(strict(text, registry), "Home and home, Day, Live; [project 1]; [project 2]; D; [id 1]")
        session = pseudonymize("", registry, (), STRICT)[1]
        self.assertEqual(session.leaks("Home Day Live D"), [])

    def test_small_known_numeric_ids_do_not_rescan_issued_placeholder_numbers(self) -> None:
        registry = Registry(people=[Person("Dario")], ids=("c-known", "1"))
        self.assertEqual(strict("Dario [1] Dario 1 10 1.25", registry), "[person 1] [[id 1]] [person 1] [id 1] 10 1.25")

    def test_partial_name_ambiguity_never_weakens_a_known_full_name_or_alias(self) -> None:
        people = [Person("Will"), Person("Mira", aliases=("Mark",)), Person("Will Mark Quill")]
        for ordered in (people, people[::-1]):
            with self.subTest(first=ordered[0].name):
                self.assertEqual(strict("will mark", Registry(people=ordered)), "[person 1] [person 2]")

    def test_ordinary_partial_name_words_are_hidden_only_capitalised(self) -> None:
        registry = Registry(people=[Person("Dario Will Quill")])
        self.assertEqual(strict("will Will WILL dario", registry), "will [person 1] [person 1] [person 2]")

    def test_name_words_of_3_letters_or_more_that_are_not_months_are_hidden(self) -> None:
        registry = Registry(people=[Person("Al June Santos")])
        # "Al" is too short and "June" is a month; "Santos" is a name word, in any case.
        self.assertEqual(strict("Al, June, Santos santos", registry), "Al, June, [person 1] [person 1]")

    def test_restore_gives_back_names_and_hides_ids(self) -> None:
        registry = Registry(people=[Person("Dario")], projects=[Project("Other Show")])
        masked, session = pseudonymize("Dario confirmed Other Show 123456789", registry, (), STRICT)
        self.assertNotIn("123456789", masked.text)
        self.assertEqual(session.restore(masked.text), "Dario confirmed Other Show [id]")

    def test_restore_accepts_the_variants_models_write(self) -> None:
        registry = Registry(people=[Person("Dario")])
        _, session = pseudonymize("Dario", registry, (), STRICT)
        for variant in ("[Person 1]", "[PERSON 1]", "[person  1]", "person 1", "[person 1 ]"):
            with self.subTest(variant=variant):
                self.assertEqual(session.restore(f"Pending {variant}"), "Pending Dario")


class OptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = Registry(
            people=[Person("Dario Quill", handles=("dq",))],
            projects=[Project("Other Show", "AB")],
            ids=("c-known",),
        )
        self.text = "Dario Quill (dq), Quill, Other Show AB c-known @someone 1234567 notion.so/x"

    def mask(self, options: MaskOptions | str) -> str:
        return pseudonymize(self.text, self.registry, (), options)[0].text

    def test_names_preset_is_the_default(self) -> None:
        self.assertEqual(pseudonymize(self.text, self.registry)[0], pseudonymize(self.text, self.registry, (), "names")[0])
        self.assertEqual(
            self.mask(NAMES),
            "[person 1] ([person 2]), Quill, [project 1] [project 2] [id 1] @someone 1234567 notion.so/x",
        )

    def test_each_option_can_be_set_alone(self) -> None:
        self.assertEqual(
            self.mask(MaskOptions(names=False, projects=False)),
            "Dario Quill (dq), Quill, Other Show AB [id 1] @someone 1234567 notion.so/x",
        )
        self.assertIn("[person 2]", self.mask(MaskOptions(name_words=True)))
        self.assertNotIn("Quill,", self.mask(MaskOptions(name_words=True)))
        self.assertEqual(
            self.mask(MaskOptions(names=False, projects=False, ids=True)),
            "Dario Quill (dq), Quill, Other Show AB [id 1] [id 2] [id 3] [id 4]",
        )

    def test_id_restore_original_or_fixed_text(self) -> None:
        for restore, expected in (("original", "see c-known"), ("[id]", "see [id]")):
            with self.subTest(restore=restore):
                masked, session = pseudonymize("see c-known", self.registry, (), MaskOptions(id_restore=restore))  # type: ignore[arg-type]
                self.assertEqual(session.restore(masked.text), expected)

    def test_a_preset_can_be_named_and_an_unknown_one_is_refused(self) -> None:
        self.assertEqual(self.mask("strict"), self.mask(STRICT))
        with self.assertRaises(ValueError):
            self.mask("loose")

    def test_ordinary_words_hidden_only_when_capitalised_is_an_option(self) -> None:
        registry = Registry(people=[Person("Dario Will")])
        always = MaskOptions(name_words=True)
        careful = MaskOptions(name_words=True, ordinary_words="capitalised")
        self.assertEqual(pseudonymize("will Will", registry, (), always)[0].text, "[person 1] [person 1]")
        self.assertEqual(pseudonymize("will Will", registry, (), careful)[0].text, "will [person 1]")


class FailClosedTests(unittest.TestCase):
    def test_restore_fails_closed_on_an_unknown_placeholder_in_both_presets(self) -> None:
        registry = Registry(people=[Person("Dario")])
        for options in (NAMES, STRICT):
            with self.subTest(options=options):
                _, session = pseudonymize("Dario", registry, (), options)
                for token in ("Pending [person 8]", "Pending [id 9]", "Pending [project 1]"):
                    with self.assertRaises(Unresolved):
                        session.restore({"text": token})

    def test_nothing_is_restored_when_one_placeholder_is_unknown(self) -> None:
        registry = Registry(people=[Person("Dario")])
        _, session = pseudonymize("Dario", registry, (), STRICT)
        with self.assertRaises(Unresolved) as caught:
            session.restore(["[person 1]", "[person 8]"])
        self.assertEqual([token for _, token in caught.exception.tokens], ["[person 8]"])


if __name__ == "__main__":
    unittest.main()
