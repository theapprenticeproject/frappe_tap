from __future__ import annotations

import csv
from io import StringIO
from typing import Sequence


GLIFIC_CONTACTS_FOLDER = "Glific_contacts"
GLIFIC_NEW_STUDENT_CSV_HEADERS = [
    "name",
    "phone",
    "language",
    "delete",
    "school_id",
    "state",
    "model",
    "buddy_name",
    "batch_id",
    "grade",
    "level",
    "course",
    "student_id",
]
GLIFIC_EXISTING_STUDENT_CSV_HEADERS = [
    "name",
    "phone",
    "school_id",
    "state",
    "model",
    "buddy_name",
    "batch_id",
    "grade",
    "level",
    "course",
    "student_id",
]
GLIFIC_CSV_HEADERS = GLIFIC_NEW_STUDENT_CSV_HEADERS


def render_glific_contact_csv(
    rows: list[dict],
    headers: Sequence[str] | None = None,
) -> bytes:
    fieldnames = list(headers or GLIFIC_CSV_HEADERS)
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows([
        {header: _csv_value(row.get(header)) for header in fieldnames}
        for row in rows
    ])
    return buffer.getvalue().encode("utf-8")


def _csv_value(value: object) -> str:
    return "" if value is None else str(value)
