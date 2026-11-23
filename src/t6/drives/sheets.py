"""Sheet rows per tab, independent of the service that supplies them."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from functools import cached_property
from typing import Any, Protocol


class SheetSource(Protocol):
    """One consistent snapshot of a workbook's rows, identified for source evidence."""

    @property
    def workbook(self) -> str: ...

    def tabs(self) -> Mapping[str, Sequence[tuple[Any, ...]]]: ...


class WorkbookSheetSource:
    """Read workbook rows; tests build their workbooks in temporary folders."""

    def __init__(self, path: str | Path) -> None:
        self.workbook = str(Path(path).resolve())

    def tabs(self) -> dict[str, tuple[tuple[Any, ...], ...]]:
        return self._rows

    @cached_property
    def _rows(self) -> dict[str, tuple[tuple[Any, ...], ...]]:
        from openpyxl import load_workbook
        workbook = load_workbook(self.workbook, read_only=True, data_only=True)
        try:
            return {sheet.title: tuple(sheet.iter_rows(values_only=True)) for sheet in workbook.worksheets}
        finally:
            workbook.close()
