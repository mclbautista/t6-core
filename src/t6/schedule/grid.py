"""A tab's cell values and merge geometry, with A1 addressing. Pure data."""

from __future__ import annotations

from dataclasses import dataclass

from t6.schedule.contract import CellValue, Merge, ScheduleRequest


def column_letters(col: int) -> str:
    """0-based column index to letters: 0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    n = col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def a1(row: int, col: int) -> str:
    """0-based row and column to an A1 cell address."""
    return f"{column_letters(col)}{row + 1}"


def a1_range(first_row: int, last_row: int, first_col: int, last_col: int) -> str:
    """Inclusive 0-based bounds to an A1 range; a single cell when the bounds meet."""
    first = a1(first_row, first_col)
    last = a1(last_row, last_col)
    return first if first == last else f"{first}:{last}"


def merge_range(merge: Merge) -> str:
    return a1_range(merge.start_row, merge.end_row - 1, merge.start_col, merge.end_col - 1)


@dataclass(frozen=True, slots=True)
class Grid:
    """What the extractor reads: values, merges and raw fill colours of one tab."""

    values: tuple[tuple[CellValue, ...], ...]
    merges: tuple[Merge, ...]
    fills: dict[tuple[int, int], str]

    @classmethod
    def from_request(cls, request: ScheduleRequest) -> Grid:
        return cls(
            values=tuple(tuple(row) for row in request.cells),
            merges=tuple(request.merges or ()),
            fills={(f.row, f.col): f.color for f in request.fills},
        )

    @property
    def row_count(self) -> int:
        return len(self.values)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.values), default=0)

    def value(self, row: int, col: int) -> CellValue:
        if 0 <= row < len(self.values) and 0 <= col < len(self.values[row]):
            return self.values[row][col]
        return None

    def merge_at(self, row: int, col: int) -> Merge | None:
        """The merge whose top-left cell is (row, col)."""
        for merge in self.merges:
            if merge.start_row == row and merge.start_col == col:
                return merge
        return None

    def covered(self, row: int, col: int) -> bool:
        """True when (row, col) is inside a merge but is not its top-left cell."""
        for merge in self.merges:
            inside = merge.start_row <= row < merge.end_row and merge.start_col <= col < merge.end_col
            if inside and (row, col) != (merge.start_row, merge.start_col):
                return True
        return False
