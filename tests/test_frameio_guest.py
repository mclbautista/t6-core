"""A Frame.io comment with no owner is by a guest. One with no file or no time is still refused (#294)."""

import unittest

from t6.frameio.model import GUEST_ID, GUEST_NAME, ResolvedComment

PROJECT = "fp1"


def raw(**changes: object) -> dict[str, object]:
    comment: dict[str, object] = {
        "id": "c1",
        "file_id": "f1",
        "created_at": "2026-11-12T10:00:00Z",
        "owner": {"id": "u1", "name": "Ana Reyes"},
    }
    comment.update(changes)
    return comment


class ResolvedCommentGuestTest(unittest.TestCase):
    def test_a_person_keeps_their_id_and_name(self) -> None:
        found = ResolvedComment.from_api(raw(), PROJECT)
        self.assertEqual((found.author_id, found.author_name), ("u1", "Ana Reyes"))

    def test_a_null_owner_is_a_guest(self) -> None:
        found = ResolvedComment.from_api(raw(owner=None), PROJECT)
        self.assertEqual((found.author_id, found.author_name), (GUEST_ID, GUEST_NAME))

    def test_an_owner_without_an_id_is_a_guest(self) -> None:
        found = ResolvedComment.from_api(raw(owner={"name": "Someone"}), PROJECT)
        self.assertEqual((found.author_id, found.author_name), (GUEST_ID, GUEST_NAME))

    def test_a_missing_owner_is_a_guest(self) -> None:
        comment = raw()
        del comment["owner"]
        self.assertEqual(ResolvedComment.from_api(comment, PROJECT).author_id, GUEST_ID)

    def test_a_comment_without_a_file_is_still_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no file or time"):
            ResolvedComment.from_api(raw(owner=None, file_id=None), PROJECT)

    def test_a_comment_without_a_time_is_still_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no file or time"):
            ResolvedComment.from_api(raw(owner=None, created_at=None), PROJECT)


if __name__ == "__main__":
    unittest.main()
