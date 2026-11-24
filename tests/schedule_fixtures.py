"""Builds synthetic monthly-calendar tabs for the schedule tests. Every text is invented."""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from t6.schedule.contract import ScheduleRequest

WEEKDAYS = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")


class TabBuilder:
    """Lays out the title, a legend and month blocks the way the layout doc describes."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], Any] = {}
        self.merges: list[dict[str, int]] = []
        self.fills: list[dict[str, Any]] = []
        self.row = 0
        self._title_and_legend()

    def _merge(self, row: int, col: int, rows: int, cols: int) -> None:
        self.merges.append(
            {"start_row": row, "end_row": row + rows, "start_col": col, "end_col": col + cols}
        )

    def _title_and_legend(self) -> None:
        self.cells[(0, 0)] = "Synthetic Timetable"
        self._merge(0, 0, 2, 14)
        labels = ("Meetings", "Offline", "Reviews")
        for line in range(3):
            for k, label in enumerate(labels):
                col = k * 4
                self.cells[(2 + line, col)] = label
                self._merge(2 + line, col, 1, 3)
                self.cells[(2 + line, col + 3)] = None
                self.fills.append({"row": 2 + line, "col": col + 3, "color": "#aabbcc"})
        self.row = 6

    def add_month(
        self,
        year: int,
        month: int,
        entries: dict[int, list[str]] | None = None,
        *,
        weekday_header: bool = True,
        wide: dict[int, tuple[int, str]] | None = None,
        day_value: Any = float,
    ) -> int:
        """Add one month block. `entries` maps a day to its single-day texts.

        `wide` maps a day to (last_day, text): a multi-day merge starting that day.
        Returns the row of the month header.
        """
        entries = entries or {}
        wide = wide or {}
        top = self.row
        self.cells[(top, 0)] = calendar.month_name[month].upper()
        self._merge(top, 0, 2, 14)
        self.cells[(top, 14)] = "NOTES"
        self.row += 2
        if weekday_header:
            for k, name in enumerate(WEEKDAYS):
                self.cells[(self.row, k * 2)] = name
                self._merge(self.row, k * 2, 1, 2)
        self.row += 1
        first = date(year, month, 1)
        weeks: dict[int, list[int]] = {}
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            week = (day + (first.weekday() + 1) % 7 - 1) // 7
            weeks.setdefault(week, []).append(day)
        for week in sorted(weeks):
            number_row = self.row
            height = 4 + week % 3
            for day in weeks[week]:
                col = ((date(year, month, day).weekday() + 1) % 7) * 2
                self.cells[(number_row, col)] = day_value(day) if callable(day_value) else day_value
                for i, text in enumerate(entries.get(day, [])):
                    self.cells[(number_row + 1 + i, col)] = text
                    self._merge(number_row + 1 + i, col, 1, 2)
                if day in wide:
                    last_day, text = wide[day]
                    span = last_day - day + 1
                    self.cells[(number_row + 1, col)] = text
                    self._merge(number_row + 1, col, 1, 2 * span)
            self.row += height
        return top

    def request(self, **extra: Any) -> ScheduleRequest:
        rows = max(r for r, _ in self.cells) + 1
        grid: list[list[Any]] = [[None] * 15 for _ in range(rows)]
        for (r, c), v in self.cells.items():
            grid[r][c] = v
        payload: dict[str, Any] = {
            "source_scope": {"document_id": "doc-synthetic", "tab_id": "tab-1", "tab_title": "as of Nov 1"},
            "cells": grid,
            "merges": self.merges,
            "fills": self.fills,
            "rows_total": rows,
            "columns_total": 15,
        }
        payload.update(extra)
        return ScheduleRequest.model_validate(payload)
