"""Build safe, readable XLSX exports for the operator portal."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

MOSCOW = ZoneInfo("Europe/Moscow")
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def safe_text(value: Any) -> str:
    """Return text that Excel cannot interpret as a formula."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def parse_datetime(value: Any, *, utc_to_moscow: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        if utc_to_moscow:
            parsed = parsed.astimezone(MOSCOW)
        parsed = parsed.replace(tzinfo=None)
    return parsed


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def parse_time(value: Any) -> time | None:
    try:
        return time.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def build_workbook(
    sheet_name: str,
    columns: Sequence[tuple[str, int]],
    rows: Iterable[Sequence[Any]],
    *,
    table_name: str,
) -> BytesIO:
    """Create a one-sheet workbook with a frozen, filtered header."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append([header for header, _ in columns])
    row_count = 0
    for values in rows:
        sheet.append(list(values))
        row_count += 1

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{sheet.cell(1, len(columns)).coordinate}"
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 32

    for index, (_, width) in enumerate(columns, 1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, datetime):
                cell.number_format = "dd.mm.yyyy hh:mm"
            elif isinstance(cell.value, date):
                cell.number_format = "dd.mm.yyyy"
            elif isinstance(cell.value, time):
                cell.number_format = "hh:mm"

    if row_count:
        table = Table(
            displayName=table_name,
            ref=f"A1:{sheet.cell(row_count + 1, len(columns)).coordinate}",
        )
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2", showFirstColumn=False, showLastColumn=False,
            showRowStripes=True, showColumnStripes=False,
        )
        sheet.add_table(table)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
