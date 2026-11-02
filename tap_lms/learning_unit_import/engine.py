"""Import learning units (Course Level, Assignment, VideoClass, Quiz, LearningUnit) from
Google Sheets activity sheets.

Ported from scripts/private/import_learning_units_*.py. The module-level constants of those
scripts now come from ImportConfig, built from a Learning Unit Import Run record.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from tap_lms.learning_unit_import.sheets import (
    Cell,
    GoogleSheetsClient,
    cell_url,
    clean,
    col_to_index,
    get_cell_text,
    is_blankish,
    normalize,
    parse_sheet_ref,
    resolve_sheet_name,
    sheet_id_for_name,
    sheet_row_count,
    strip_url_tracking_parameters,
)


SOURCE_TYPE_MASTER_INDEX = "Master Index"
SOURCE_TYPE_SOURCE_TABS = "Source Tabs"
MATCH_EXACT = "Exact"
MATCH_PREFIX = "Prefix"

DEFAULT_QUIZ_PASSING_SCORE = 60
DEFAULT_VIDEO_COMPLETION_THRESHOLD = 70
DEFAULT_QUIZ_POINTS = 1
DEFAULT_QUIZ_FAILED_POINTS = 0

# The activity sheets have Hinglish links, then Hindi/Marathi/Punjabi links.
# The LMS default VideoClass fields are used for the Hinglish/default links.
CREATE_DEFAULT_LANGUAGE_VIDEO_TRANSLATION = False

COURSE_VERTICAL_NAME_MAP = {
    "STEM-Coding": "Coding",
    "STEM-Science": "Science",
    "VA": "Arts",
    "Fin-Lit": "Financial Literacy",
}
ASSIGNMENT_FLOW_COURSE_NAME_MAP = {
    "Science": "Science Lab",
}

LANGUAGES = ("English", "Hindi", "Marathi", "Punjabi")
TRANSLATION_LANGUAGES = ("Hindi", "Marathi", "Punjabi")


# ---------------------------------------------------------------------------
# Sheet columns.
# ---------------------------------------------------------------------------

COL_UNIT_ACTIVITY = "A"
COL_ACTIVITY_TYPE = "B"
COL_ACTIVITY_NAME = "C"
COL_DESCRIPTION = "E"
COL_OBJECTIVE = "F"
COL_OUTCOMES = "G"
COL_ENGLISH_SCRIPT = "N"
COL_HINDI_SCRIPT = "O"
COL_MARATHI_SCRIPT = "P"
COL_PUNJABI_SCRIPT = "Q"

MAIN_QUIZ_START_COL = "R"
MAIN_QUIZ_END_COL = "DM"

COL_DRIVE_HINGLISH = "GP"
COL_DRIVE_HINDI = "GQ"
COL_DRIVE_MARATHI = "GR"
COL_DRIVE_PUNJABI = "GS"

COL_YOUTUBE_HINGLISH = "GT"
COL_YOUTUBE_HINDI = "GU"
COL_YOUTUBE_MARATHI = "GV"
COL_YOUTUBE_PUNJABI = "GW"

COL_PLIO_HINGLISH = "GX"
COL_PLIO_HINDI = "GY"
COL_PLIO_MARATHI = "GZ"
COL_PLIO_PUNJABI = "HA"

COL_CANVA_HINGLISH = "HB"
COL_CANVA_HINDI = "HC"
COL_CANVA_MARATHI = "HD"
COL_CANVA_PUNJABI = "HE"

COL_VIDEO_DESCRIPTION_ENGLISH = "HF"
COL_VIDEO_DESCRIPTION_HINDI = "HG"
COL_VIDEO_DESCRIPTION_MARATHI = "HH"
COL_VIDEO_DESCRIPTION_PUNJABI = "HI"
COL_ASSIGNMENT_SUBMISSION_GUIDELINES = "HK"
HK_EMPTY_SKIP_OUTPUT = "Skipped: Valid/Invalid not found"

ACTIVITY_READ_END_COL = "HY"
EXTERNAL_QUIZ_READ_END_COL = "GR"


@dataclass
class SourceTab:
    sheet_name: str
    course: str
    level: str
    grade: str = ""


@dataclass
class ImportConfig:
    source_type: str
    spreadsheet_url: str
    course_level_program: str
    master_sheet_name: str = ""
    source_vertical: str = ""
    source_tabs: list[SourceTab] = field(default_factory=list)
    target_vertical: str = ""
    target_course: str = ""
    target_level: str = ""
    # Each entry maps an activity name in column A to the week number used in
    # Course Level.learning_units. Empty list imports all matched rows as default_week_no.
    unit_filters: list[tuple[str, int]] = field(default_factory=list)
    unit_match_mode: str = MATCH_EXACT
    default_week_no: int = 0
    assignment_flow_spreadsheet_url: str = ""
    assignment_flow_sheet_name: str = ""
    dry_run: bool = True
    update_existing: bool = False
    stop_on_first_error: bool = True
    difficulty_tier: str = "Basic"
    default_status: str = "Published"
    link_learning_units_to_course_level: bool = True

    @property
    def is_source_tabs(self) -> bool:
        return self.source_type == SOURCE_TYPE_SOURCE_TABS

    @property
    def assignment_flow_key(self) -> str:
        # Master index sheets key the assignment flow tab by level, source tabs by grade.
        return "grade" if self.is_source_tabs else "level"


@dataclass
class MasterEntry:
    vertical: str
    course: str
    level: str
    sheet_url: str
    sheet_name: str = ""
    grade: str = ""


@dataclass
class ActivityRow:
    master: MasterEntry
    spreadsheet_id: str
    sheet_name: str
    row_number: int
    row: list[Cell]
    week_no: int
    skip_reason: str = ""

    @property
    def unit_activity(self) -> str:
        return clean(self.row[col_to_index(COL_UNIT_ACTIVITY)].text)

    @property
    def activity_name(self) -> str:
        return clean(self.row[col_to_index(COL_ACTIVITY_NAME)].text)


@dataclass
class QuizQuestionData:
    question_number: int
    question: str
    options: list[str]
    correct_option: int
    translations: dict[str, str]
    option_translations: dict[str, list[str]]


@dataclass
class ActivityResult:
    activity: ActivityRow
    status: str  # Imported / Skipped / Error
    message: str = ""
    week_no: int = 0
    course_level: str = ""
    assignment: str = ""
    video_class: str = ""
    quiz: str = ""
    learning_unit: str = ""


class Writer(Protocol):
    def upsert_by_filters(
        self,
        doctype: str,
        filters: list[list[Any]],
        payload: dict[str, Any],
        label: str,
        log_result: bool = True,
    ) -> tuple[str, str]: ...

    def update_doc(self, doctype: str, name: str, values: dict[str, Any]) -> None: ...

    def link_learning_unit_to_course_level(
        self, course_level_name: str, learning_unit_name: str, week_no: int, idx: int
    ) -> str: ...

    def rollback(self) -> None: ...


class Reporter(Protocol):
    def log(self, message: str) -> None: ...

    def activity_finished(self, result: ActivityResult, counts: dict[str, int]) -> None: ...


@dataclass
class ImportContext:
    config: ImportConfig
    sheets: GoogleSheetsClient
    writer: Writer
    reporter: Reporter

    @property
    def log(self) -> Callable[[str], None]:
        return self.reporter.log


def run_import(
    config: ImportConfig,
    sheets: GoogleSheetsClient,
    writer: Writer,
    reporter: Reporter,
) -> dict[str, int]:
    ctx = ImportContext(config, sheets, writer, reporter)
    if not clean(config.course_level_program) and config.link_learning_units_to_course_level:
        raise RuntimeError("Course Level Program is required before creating Course Level records.")

    verify_assignment_flow_sheet_access(ctx)

    if config.is_source_tabs:
        entries = read_source_tab_entries(ctx)
    else:
        entries = read_master_entries(ctx)

    target_entries = [
        entry
        for entry in entries
        if entry.vertical in COURSE_VERTICAL_NAME_MAP
        and matches_target_filter(entry.vertical, config.target_vertical)
        and matches_target_filter(entry.course, config.target_course)
        and matches_target_filter(entry.level, config.target_level)
    ]
    if not target_entries:
        raise RuntimeError(
            f"No source entries match vertical={config.target_vertical!r}, "
            f"course={config.target_course!r}, level={config.target_level!r}"
        )
    ctx.log(f"Matched {len(target_entries)} course/level sheets")

    summary = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
    output_rows: list[dict[str, str]] = []
    try:
        for entry in target_entries:
            try:
                activities = read_activity_rows(ctx, entry)
            except Exception as exc:
                summary["errors"] += 1
                writer.rollback()
                ctx.log(f"ERROR {entry.vertical} | {entry.course} | {entry.level}: {exc}")
                if config.stop_on_first_error:
                    raise
                continue

            for activity in activities:
                try:
                    counts, output_row, result = import_activity_bundle(ctx, activity)
                except Exception as exc:
                    summary["errors"] += 1
                    writer.rollback()
                    ctx.log(
                        f"ERROR activity row {activity.row_number}: "
                        f"{activity.unit_activity} | {activity.activity_name}\n{exc}"
                    )
                    reporter.activity_finished(
                        ActivityResult(activity, "Error", str(exc), activity.week_no),
                        {"errors": 1},
                    )
                    if config.stop_on_first_error:
                        raise
                    continue

                for key, value in counts.items():
                    summary[key] = summary.get(key, 0) + value
                output_rows.append(output_row)
                reporter.activity_finished(result, counts)
    except Exception:
        # Bundles imported before the failure are already committed, so still record
        # their assignment IDs in the assignment flow sheet.
        if output_rows:
            try:
                write_assignment_flow_sheet(ctx, output_rows)
            except Exception as write_exc:
                ctx.log(f"ERROR writing assignment flow sheet after failure: {write_exc}")
        raise

    write_assignment_flow_sheet(ctx, output_rows)

    ctx.log("Import summary: " + ", ".join(f"{key}={summary[key]}" for key in sorted(summary)))
    return summary


def write_assignment_flow_sheet(ctx: ImportContext, rows: list[dict[str, str]]) -> None:
    config = ctx.config
    if not config.assignment_flow_spreadsheet_url:
        return
    if config.dry_run:
        rows = [row for row in rows if row.get("assignment_value_kind") == "skipped"]
        if not rows:
            ctx.log("DRY RUN: not updating assignment flow sheet with placeholder assignment IDs")
            return
        ctx.log("DRY RUN: merging HK-empty skip messages only; not writing placeholder assignment IDs")

    key_field = config.assignment_flow_key
    sheet_ref = parse_sheet_ref(config.assignment_flow_spreadsheet_url)
    metadata = ctx.sheets.get_metadata(sheet_ref.spreadsheet_id)
    sheet_name = resolve_sheet_name(metadata, sheet_ref.gid, config.assignment_flow_sheet_name)
    existing_values = ctx.sheets.get_values(sheet_ref.spreadsheet_id, sheet_name)
    fieldnames = ["course", key_field]
    assignments_by_key: dict[tuple[str, str], dict[str, str]] = {}
    assignment_columns = []
    for row in rows:
        course = clean(row.get("course"))
        key_value = clean(row.get(key_field))
        assignment_column = clean(row.get("assignment_column"))
        assignment_value = clean(row.get("assignment_value"))
        if not course or not key_value or not assignment_column or not assignment_value:
            continue
        key = (normalize(course), normalize(key_value))
        assignments_by_key.setdefault(key, {})[assignment_column] = assignment_value
        if assignment_column not in assignment_columns:
            assignment_columns.append(assignment_column)

    if not assignments_by_key:
        ctx.log("No assignment IDs or skip messages to merge into assignment flow sheet")
        return

    output_row_by_key = {
        (normalize(row["course"]), normalize(row[key_field])): row
        for row in rows
        if clean(row.get("course")) and clean(row.get(key_field))
    }

    sheet_rows = []
    updated_keys = set()
    if existing_values:
        fieldnames = normalize_output_headers(existing_values[0], fieldnames)
        for values in existing_values[1:]:
            row = sheet_values_to_dict(fieldnames, values)
            key = (normalize(row.get("course")), normalize(row.get(key_field)))
            if key in assignments_by_key:
                row.update(assignments_by_key[key])
                updated_keys.add(key)
            sheet_rows.append(row)

    existing_keys = {(normalize(row.get("course")), normalize(row.get(key_field))) for row in sheet_rows}
    for key, assignments in assignments_by_key.items():
        if key in existing_keys:
            continue
        output_row = output_row_by_key[key]
        sheet_rows.append(
            {
                "course": clean(output_row.get("course")),
                key_field: clean(output_row.get(key_field)),
                **assignments,
            }
        )
        updated_keys.add(key)

    for assignment_column in assignment_columns:
        if assignment_column not in fieldnames:
            fieldnames.append(assignment_column)

    values = [fieldnames] + [[clean(row.get(fieldname)) for fieldname in fieldnames] for row in sheet_rows]
    ctx.sheets.replace_values(sheet_ref.spreadsheet_id, sheet_name, values, existing_values)
    assignment_column_indexes = [fieldnames.index(column) for column in assignment_columns]
    skipped_cells = [
        (row_index, column_index)
        for row_index, row_values in enumerate(values[1:], start=1)
        for column_index in assignment_column_indexes
        if clean(row_values[column_index]) == HK_EMPTY_SKIP_OUTPUT
    ]
    ctx.sheets.format_assignment_output_cells(
        sheet_ref.spreadsheet_id,
        sheet_id_for_name(metadata, sheet_name),
        len(values),
        assignment_column_indexes,
        skipped_cells,
    )
    ctx.log(
        f"Merged assignment output for {len(updated_keys)} course/{key_field} rows into "
        f"Google Sheet tab: {sheet_name}"
    )


def verify_assignment_flow_sheet_access(ctx: ImportContext) -> None:
    config = ctx.config
    if not config.assignment_flow_spreadsheet_url:
        return

    try:
        sheet_ref = parse_sheet_ref(config.assignment_flow_spreadsheet_url)
        metadata = ctx.sheets.get_metadata(sheet_ref.spreadsheet_id)
        sheet_name = resolve_sheet_name(metadata, sheet_ref.gid, config.assignment_flow_sheet_name)
        sheet_id = sheet_id_for_name(metadata, sheet_name)
        ctx.sheets.verify_write_access(sheet_ref.spreadsheet_id, sheet_id, sheet_name)
    except Exception as exc:
        raise RuntimeError(
            "Google Sheet preflight failed before any Frappe update. "
            f"Fix output Sheet read/write access, then rerun. Original error: {exc}"
        ) from exc

    ctx.log(f"Verified Google Sheet read/write access: {sheet_name}")


def normalize_output_headers(raw_headers: list[Any], required_headers: list[str]) -> list[str]:
    required_by_normalized = {normalize(header): header for header in required_headers}
    headers = []
    seen = set()
    for raw_header in raw_headers:
        header = clean(raw_header)
        if not header:
            continue
        header = required_by_normalized.get(normalize(header), header)
        if header in seen:
            continue
        headers.append(header)
        seen.add(header)

    for index, required_header in enumerate(required_headers):
        if required_header not in seen:
            headers.insert(index, required_header)
            seen.add(required_header)
    return headers


def sheet_values_to_dict(fieldnames: list[str], values: list[Any]) -> dict[str, str]:
    return {
        fieldname: clean(values[index]) if index < len(values) else ""
        for index, fieldname in enumerate(fieldnames)
    }


def read_master_entries(ctx: ImportContext) -> list[MasterEntry]:
    config = ctx.config
    sheet_ref = parse_sheet_ref(config.spreadsheet_url)
    metadata = ctx.sheets.get_metadata(sheet_ref.spreadsheet_id)
    sheet_name = resolve_sheet_name(metadata, sheet_ref.gid, config.master_sheet_name)
    row_count = sheet_row_count(metadata, sheet_name)
    rows = ctx.sheets.get_grid(sheet_ref.spreadsheet_id, sheet_name, f"A1:D{row_count}")

    entries = []
    current_vertical = ""
    header = rows[0] if rows else []
    vertical_col = find_header_index(header, ("vertical",), "A")
    course_col = find_header_index(header, ("course",), "B")
    level_col = find_header_index(header, ("level",), "C")
    sheet_link_col = find_header_index(header, ("link", "sheet", "implementation"), "D")
    for row in rows[1:]:
        vertical = clean(row[vertical_col].text)
        if vertical:
            current_vertical = vertical
        course = clean(row[course_col].text)
        level = clean(row[level_col].text)
        sheet_url = cell_url(row[sheet_link_col])

        if not course or not level or not sheet_url:
            continue
        entries.append(MasterEntry(current_vertical, course, level, sheet_url))
    return entries


def read_source_tab_entries(ctx: ImportContext) -> list[MasterEntry]:
    config = ctx.config
    sheet_ref = parse_sheet_ref(config.spreadsheet_url)
    metadata = ctx.sheets.get_metadata(sheet_ref.spreadsheet_id)

    entries = []
    for tab in config.source_tabs:
        sheet_name = clean(tab.sheet_name)
        if not sheet_name:
            raise RuntimeError(f"Configured source tab is missing sheet_name: {tab!r}")

        resolved_sheet_name = resolve_sheet_name(metadata, None, sheet_name)
        if normalize(resolved_sheet_name) != normalize(sheet_name):
            raise RuntimeError(f"Configured source sheet tab not found: {sheet_name!r}")

        level = clean(tab.level)
        if not level:
            raise RuntimeError(f"Configured source tab is missing level: {tab!r}")

        entries.append(
            MasterEntry(
                vertical=config.source_vertical,
                course=clean(tab.course) or resolved_sheet_name,
                level=level,
                sheet_url=config.spreadsheet_url,
                sheet_name=resolved_sheet_name,
                grade=clean(tab.grade),
            )
        )
    return entries


def find_header_index(header: list[Cell], keywords: tuple[str, ...], fallback_column: str) -> int:
    normalized_keywords = [normalize(keyword) for keyword in keywords]
    for index, cell in enumerate(header):
        header_text = normalize(cell.text)
        if header_text and any(keyword in header_text for keyword in normalized_keywords):
            return index
    return col_to_index(fallback_column)


def read_activity_rows(ctx: ImportContext, entry: MasterEntry) -> list[ActivityRow]:
    ref = parse_sheet_ref(entry.sheet_url)
    metadata = ctx.sheets.get_metadata(ref.spreadsheet_id)
    preferred_sheet_name = entry.sheet_name or entry.level
    gid = None if entry.sheet_name else ref.gid
    sheet_name = resolve_sheet_name(metadata, gid, preferred_sheet_name)
    row_count = sheet_row_count(metadata, sheet_name)
    rows = ctx.sheets.get_grid(ref.spreadsheet_id, sheet_name, f"A1:{ACTIVITY_READ_END_COL}{row_count}")

    activities = []
    activities_by_unit: dict[str, ActivityRow] = {}
    for row_index, row in enumerate(rows[2:], start=3):
        unit_activity = clean(row[col_to_index(COL_UNIT_ACTIVITY)].text)
        activity_name = clean(row[col_to_index(COL_ACTIVITY_NAME)].text)
        if not unit_activity or not activity_name:
            continue
        matched, week_no = match_unit_filter(ctx.config, unit_activity)
        if not matched:
            continue
        submission_guidelines = clean(row[col_to_index(COL_ASSIGNMENT_SUBMISSION_GUIDELINES)].text)
        activity = ActivityRow(
            entry,
            ref.spreadsheet_id,
            sheet_name,
            row_index,
            row,
            week_no,
            HK_EMPTY_SKIP_OUTPUT if is_blankish(submission_guidelines) else "",
        )

        if not ctx.config.is_source_tabs:
            activities.append(activity)
            continue

        # Source tabs can repeat an activity row; keep one per unit, preferring the
        # row that has Valid/Invalid filled in.
        activity_key = normalize(unit_activity)
        existing = activities_by_unit.get(activity_key)
        if existing:
            if existing.skip_reason and not activity.skip_reason:
                activities_by_unit[activity_key] = activity
                ctx.log(
                    f"IGNORE duplicate activity row {existing.row_number}: {unit_activity}; "
                    f"using row {row_index} because Valid/Invalid is present"
                )
            else:
                ctx.log(
                    f"IGNORE duplicate activity row {row_index}: {unit_activity}; "
                    f"using row {existing.row_number}"
                )
            continue
        activities_by_unit[activity_key] = activity

    if ctx.config.is_source_tabs:
        activities = list(activities_by_unit.values())

    ctx.log(f"Matched {len(activities)} activity rows for {entry.vertical} | {entry.course} | {entry.level}")
    return activities


def import_activity_bundle(
    ctx: ImportContext,
    activity: ActivityRow,
) -> tuple[dict[str, int], dict[str, str], ActivityResult]:
    config = ctx.config
    counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
    activity_name = activity.activity_name
    unit_activity = activity.unit_activity
    course_vertical = resolve_course_vertical(activity.master.vertical)

    if activity.skip_reason:
        counts["skipped"] += 1
        ctx.log(f"SKIP activity row {activity.row_number}: {activity.skip_reason} for {unit_activity} | {activity_name}")
        return (
            counts,
            assignment_flow_output_row(activity, course_vertical, activity.skip_reason),
            ActivityResult(activity, "Skipped", activity.skip_reason, activity.week_no),
        )

    course_level_name = ""
    if config.link_learning_units_to_course_level:
        course_level_name, status = create_course_level(ctx, activity, course_vertical)
        counts[status] += 1

    assignment_name, status = create_assignment(ctx, activity, course_vertical)
    counts[status] += 1

    video_name, status = create_video_class(ctx, activity, course_vertical, assignment_name)
    counts[status] += 1

    quiz_name = ""
    quiz_questions = parse_activity_quiz(ctx, activity)
    if quiz_questions:
        quiz_name, quiz_counts = create_quiz(ctx, activity, quiz_questions)
        for key, value in quiz_counts.items():
            counts[key] = counts.get(key, 0) + value
    else:
        ctx.log(f"SKIP Quiz: no main quiz found for {unit_activity} | {activity_name}")

    learning_unit_name, status = create_learning_unit(
        ctx,
        activity,
        course_vertical,
        course_level_name,
        video_name,
        quiz_name,
    )
    counts[status] += 1

    if config.link_learning_units_to_course_level:
        if config.dry_run:
            ctx.log(f"DRY RUN link Course Level.learning_units: {course_level_name} -> {learning_unit_name}")
            status = "skipped"
        else:
            status = ctx.writer.link_learning_unit_to_course_level(
                course_level_name,
                learning_unit_name,
                activity.week_no,
                activity.row_number,
            )
            ctx.log(f"{status.upper()} Course Level.learning_units: {course_level_name} -> {learning_unit_name}")
        counts[status] += 1

    ctx.log(
        f"Bundle ready: {unit_activity} | {activity_name} | "
        f"week={activity.week_no} | CourseLevel={course_level_name or '-'} | "
        f"LearningUnit={learning_unit_name}"
    )
    result = ActivityResult(
        activity,
        "Imported",
        week_no=activity.week_no,
        course_level=course_level_name,
        assignment=assignment_name,
        video_class=video_name,
        quiz=quiz_name,
        learning_unit=learning_unit_name,
    )
    return counts, assignment_flow_output_row(activity, course_vertical, assignment_name), result


def create_course_level(
    ctx: ImportContext,
    activity: ActivityRow,
    course_vertical: str,
) -> tuple[str, str]:
    program = ctx.config.course_level_program
    course_name = course_level_course_name(program, activity.master.course)
    payload = {
        "name1": course_name,
        "program": program,
        "vertical": course_vertical,
        "level": activity.master.level,
    }
    filters = [
        ["name1", "=", course_name],
        ["program", "=", program],
        ["vertical", "=", course_vertical],
        ["level", "=", activity.master.level],
    ]
    label = f"{course_name} {activity.master.level}"
    return ctx.writer.upsert_by_filters("Course Level", filters, payload, label)


def create_assignment(
    ctx: ImportContext,
    activity: ActivityRow,
    course_vertical: str,
) -> tuple[str, str]:
    row = activity.row
    difficulty_tier = ctx.config.difficulty_tier
    activity_type = clean(row[col_to_index(COL_ACTIVITY_TYPE)].text)
    assignment_name = f"{activity.activity_name} Assignment"

    payload = {
        "name": make_doc_name("ASN", assignment_name),
        "assignment_name": assignment_name,
        "description": clean(row[col_to_index(COL_DESCRIPTION)].text),
        "assignment_type": "Practical",
        "subject": course_vertical,
        "submission_guidelines": clean(row[col_to_index(COL_ASSIGNMENT_SUBMISSION_GUIDELINES)].text),
        "difficulty_tier": difficulty_tier,
        "activity_type": map_assignment_activity_type(activity_type),
        "assignment_translations": build_assignment_translations(row, assignment_name),
        "submission_media_links": build_submission_media_links(row),
    }
    filters = [
        ["assignment_name", "=", assignment_name],
        ["difficulty_tier", "=", difficulty_tier],
    ]
    return ctx.writer.upsert_by_filters("Assignment", filters, payload, assignment_name)


def create_video_class(
    ctx: ImportContext,
    activity: ActivityRow,
    course_vertical: str,
    assignment_name: str,
) -> tuple[str, str]:
    row = activity.row
    difficulty_tier = ctx.config.difficulty_tier
    video_name = activity.activity_name

    payload = {
        "name": make_doc_name("VC", video_name),
        "video_name": video_name,
        "description": clean(row[col_to_index(COL_VIDEO_DESCRIPTION_ENGLISH)].text),
        "duration": "00:00:00",
        "video_youtube_url": cell_url(row[col_to_index(COL_YOUTUBE_HINGLISH)]),
        "video_plio_url": cell_url(row[col_to_index(COL_PLIO_HINGLISH)]),
        "completion_threshold": DEFAULT_VIDEO_COMPLETION_THRESHOLD,
        "difficulty_tier": difficulty_tier,
        "course_vertical": course_vertical,
        "video_translations": build_video_translations(row),
        "assessments": [
            {
                "doctype": "AssessmentList",
                "assessment_type": "Assignment",
                "assessment": assignment_name,
            }
        ],
    }
    filters = [
        ["video_name", "=", video_name],
        ["difficulty_tier", "=", difficulty_tier],
    ]
    return ctx.writer.upsert_by_filters("VideoClass", filters, payload, video_name)


def create_quiz(
    ctx: ImportContext,
    activity: ActivityRow,
    questions: list[QuizQuestionData],
) -> tuple[str, dict[str, int]]:
    row = activity.row
    difficulty_tier = ctx.config.difficulty_tier
    quiz_name = f"{activity.activity_name} Quiz"
    counts = {"created": 0, "updated": 0, "skipped": 0}

    question_links = []
    for question in questions:
        question_name, status = create_quiz_question(
            ctx,
            question,
            make_question_key(activity.unit_activity, activity.activity_name, question.question_number),
        )
        counts[status] += 1
        question_links.append(
            {
                "doctype": "QuizQuestionList",
                "question": question_name,
                "question_number": question.question_number,
            }
        )

    payload = {
        "name": make_doc_name("QZ", quiz_name),
        "quiz_name": quiz_name,
        "description": clean(row[col_to_index(COL_DESCRIPTION)].text),
        "quiz_type": "Main",
        "questions": question_links,
        "passing_score": DEFAULT_QUIZ_PASSING_SCORE,
        "total_questions": len(question_links),
        "status": ctx.config.default_status,
        "difficulty_tier": difficulty_tier,
    }
    filters = [
        ["quiz_name", "=", quiz_name],
        ["difficulty_tier", "=", difficulty_tier],
    ]
    created_quiz_name, status = ctx.writer.upsert_by_filters("Quiz", filters, payload, quiz_name)
    counts[status] += 1
    return created_quiz_name, counts


def create_quiz_question(
    ctx: ImportContext,
    question: QuizQuestionData,
    question_key: str,
) -> tuple[str, str]:
    question_payload = {
        "name": make_doc_name("QQ", question_key),
        "question_name": question_key,
        "question": question.question,
        "question_type": "Multiple Choice",
        "points": DEFAULT_QUIZ_POINTS,
        "failed_points": DEFAULT_QUIZ_FAILED_POINTS,
        "correct_option": question.correct_option,
        "question_translations": [
            {
                "doctype": "QuizQuestionTranslation",
                "language": language,
                "translated_question": translated_question,
            }
            for language, translated_question in question.translations.items()
            if translated_question
        ],
    }
    question_filters = [["question_name", "=", question_key]]
    question_doc_name, status = ctx.writer.upsert_by_filters(
        "QuizQuestion",
        question_filters,
        question_payload,
        question_key,
        log_result=False,
    )

    if ctx.config.dry_run:
        return question_doc_name, status

    option_links = []
    for index, option_text in enumerate(question.options, start=1):
        option_name = upsert_quiz_option(
            ctx,
            question_doc_name,
            index,
            option_text,
            question.option_translations,
        )
        option_links.append({"doctype": "QuizOptionList", "options": option_name, "order_number": index})

    ctx.writer.update_doc(
        "QuizQuestion",
        question_doc_name,
        {
            "options": option_links,
            "correct_option": question.correct_option,
        },
    )
    return question_doc_name, status


def upsert_quiz_option(
    ctx: ImportContext,
    question_doc_name: str,
    option_number: int,
    option_text: str,
    option_translations: dict[str, list[str]],
) -> str:
    translations = []
    for language, translated_options in option_translations.items():
        translated_option = get_list_value(translated_options, option_number - 1)
        if translated_option:
            translations.append(
                {
                    "doctype": "QuizOptionTranslation",
                    "language": language,
                    "translated_option": translated_option,
                }
            )

    payload = {
        "option_text": option_text,
        "option_number": option_number,
        "question_id": question_doc_name,
        "option_translations": translations,
    }
    filters = [
        ["question_id", "=", question_doc_name],
        ["option_number", "=", option_number],
    ]
    option_doc_name, _status = ctx.writer.upsert_by_filters(
        "QuizOption",
        filters,
        payload,
        f"{question_doc_name} option {option_number}",
        log_result=False,
    )
    return option_doc_name


def create_learning_unit(
    ctx: ImportContext,
    activity: ActivityRow,
    course_vertical: str,
    course_level_name: str,
    video_name: str,
    quiz_name: str,
) -> tuple[str, str]:
    row = activity.row
    difficulty_tier = ctx.config.difficulty_tier
    activity_name = activity.activity_name
    unit_activity = activity.unit_activity
    activity_type = clean(row[col_to_index(COL_ACTIVITY_TYPE)].text)

    content_items = [
        {
            "doctype": "UnitContentItem",
            "content_type": "VideoClass",
            "content": video_name,
            "is_optional": 0,
        }
    ]
    if quiz_name:
        content_items.append(
            {
                "doctype": "UnitContentItem",
                "content_type": "Quiz",
                "content": quiz_name,
                "is_optional": 0,
            }
        )

    course_level_for_name = course_level_name or f"{activity.master.course} {activity.master.level}"
    payload = {
        "name": make_doc_name("LU", f"{course_level_for_name} {unit_activity}"),
        "unit_name": activity_name,
        "description": clean(row[col_to_index(COL_DESCRIPTION)].text),
        "order": activity.row_number,
        "unit_type": "Lesson",
        "difficulty_tier": difficulty_tier,
        "course_vertical": course_vertical,
        "activity_type": map_learning_unit_activity_type(activity_type),
        "status": ctx.config.default_status,
        "content_items": content_items,
        # Transient value for LearningUnit's current autoname expression. This
        # doctype does not define a course_level field, but autoname references it.
        "course_level": course_level_for_name,
    }
    filters = [
        ["unit_name", "=", activity_name],
        ["difficulty_tier", "=", difficulty_tier],
        ["order", "=", activity.row_number],
    ]
    return ctx.writer.upsert_by_filters("LearningUnit", filters, payload, unit_activity)


def parse_activity_quiz(ctx: ImportContext, activity: ActivityRow) -> list[QuizQuestionData]:
    row = activity.row
    linked_quiz = find_external_quiz_link(row)
    if linked_quiz:
        link_url, referenced_rows = linked_quiz
        return parse_external_quiz_sheet(ctx.sheets, link_url, referenced_rows, activity.spreadsheet_id)

    activity_headers = read_activity_headers(ctx.sheets, activity.spreadsheet_id, activity.sheet_name)
    return parse_structured_quiz_questions(activity_headers[0], activity_headers[1], row)


def read_activity_headers(
    sheets: GoogleSheetsClient,
    spreadsheet_id: str,
    sheet_name: str,
) -> list[list[Cell]]:
    rows = sheets.get_grid(spreadsheet_id, sheet_name, f"A1:{ACTIVITY_READ_END_COL}2")
    if len(rows) < 2:
        raise RuntimeError(f"Activity sheet has fewer than two header rows: {sheet_name}")
    return rows[:2]


def find_external_quiz_link(row: list[Cell]) -> tuple[str, list[int]] | None:
    start = col_to_index(MAIN_QUIZ_START_COL)
    end = col_to_index(MAIN_QUIZ_END_COL)
    link_url = ""
    notes = []
    for cell in row[start : end + 1]:
        text = clean(cell.text)
        if link_url and normalize(text) == "link" and cell.hyperlink:
            break
        if text:
            notes.append(text)
        if not link_url and normalize(text) == "link" and cell.hyperlink:
            link_url = cell.hyperlink
    if not link_url:
        return None
    return link_url, parse_referenced_rows("\n".join(notes))


def parse_external_quiz_sheet(
    sheets: GoogleSheetsClient,
    quiz_url: str,
    referenced_rows: list[int],
    fallback_spreadsheet_id: str = "",
) -> list[QuizQuestionData]:
    ref = parse_sheet_ref(quiz_url, fallback_spreadsheet_id=fallback_spreadsheet_id)
    metadata = sheets.get_metadata(ref.spreadsheet_id)
    sheet_name = resolve_sheet_name(metadata, ref.gid, "")
    row_count = sheet_row_count(metadata, sheet_name)
    rows = sheets.get_grid(ref.spreadsheet_id, sheet_name, f"A1:{EXTERNAL_QUIZ_READ_END_COL}{row_count}")

    if len(rows) < 3:
        return []

    header_1, header_2 = rows[0], rows[1]
    referenced_rows = referenced_rows or parse_referenced_rows_from_url(quiz_url)
    source_row_indexes = [row_number - 1 for row_number in referenced_rows if row_number > 2]
    if not source_row_indexes:
        source_row_indexes = [
            index
            for index, row in enumerate(rows[2:], start=2)
            if row_has_structured_english_question(header_1, header_2, row)
        ][:1]

    questions = []
    for source_row_index in source_row_indexes:
        if 0 <= source_row_index < len(rows):
            questions.extend(parse_structured_quiz_questions(header_1, header_2, rows[source_row_index]))
    return renumber_questions(questions)


def parse_structured_quiz_questions(
    header_1: list[Cell],
    header_2: list[Cell],
    row: list[Cell],
) -> list[QuizQuestionData]:
    blocks_by_language = collect_quiz_blocks(header_1, header_2, row)
    english_blocks = blocks_by_language.get("English") or {}
    questions = []

    for question_number in sorted(english_blocks):
        english = english_blocks[question_number]
        if not is_valid_question_block(english):
            continue

        translations = {}
        option_translations = {}
        for language in TRANSLATION_LANGUAGES:
            translated = (blocks_by_language.get(language) or {}).get(question_number)
            if translated and not is_blankish(translated["question"]):
                translations[language] = translated["question"]
                option_translations[language] = translated["options"]

        questions.append(
            QuizQuestionData(
                question_number=question_number,
                question=english["question"],
                options=english["options"],
                correct_option=english["correct_option"],
                translations=translations,
                option_translations=option_translations,
            )
        )

    return renumber_questions(questions)


def collect_quiz_blocks(
    header_1: list[Cell],
    header_2: list[Cell],
    row: list[Cell],
) -> dict[str, dict[int, dict[str, Any]]]:
    sections = fill_merged_header_sections(header_1)
    starts = []
    for index, cell in enumerate(header_2):
        if normalize(cell.text) != "correct option":
            continue
        section = sections[index] if index < len(sections) else ""
        language = language_from_quiz_section(section)
        if language:
            starts.append((index, language))

    result: dict[str, dict[int, dict[str, Any]]] = {language: {} for language in LANGUAGES}
    start_indexes = [start for start, _language in starts]
    for position, (start, language) in enumerate(starts):
        next_start = start_indexes[position + 1] if position + 1 < len(start_indexes) else len(header_2)
        question_number = parse_question_number(get_cell_text(header_2, start + 1)) or (len(result[language]) + 1)
        question = clean(get_cell_text(row, start + 1))
        options = []
        for index in range(start + 2, min(next_start, len(header_2), len(row))):
            if not normalize(get_cell_text(header_2, index)).startswith("option"):
                continue
            option = clean(get_cell_text(row, index))
            if not is_blankish(option):
                options.append(option)

        result[language][question_number] = {
            "question": question,
            "options": options,
            "correct_option": parse_correct_option(clean(get_cell_text(row, start))),
        }
    return result


def row_has_structured_english_question(
    header_1: list[Cell],
    header_2: list[Cell],
    row: list[Cell],
) -> bool:
    blocks = collect_quiz_blocks(header_1, header_2, row).get("English") or {}
    return any(is_valid_question_block(block) for block in blocks.values())


def is_valid_question_block(block: dict[str, Any]) -> bool:
    return (
        not is_blankish(block.get("question"))
        and normalize(block.get("question")) != "link"
        and len(block.get("options") or []) >= 2
    )


def build_video_translations(row: list[Cell]) -> list[dict[str, Any]]:
    configs = {
        "English": {
            "description": COL_VIDEO_DESCRIPTION_ENGLISH,
            "youtube": COL_YOUTUBE_HINGLISH,
            "plio": COL_PLIO_HINGLISH,
        },
        "Hindi": {
            "description": COL_VIDEO_DESCRIPTION_HINDI,
            "youtube": COL_YOUTUBE_HINDI,
            "plio": COL_PLIO_HINDI,
        },
        "Marathi": {
            "description": COL_VIDEO_DESCRIPTION_MARATHI,
            "youtube": COL_YOUTUBE_MARATHI,
            "plio": COL_PLIO_MARATHI,
        },
        "Punjabi": {
            "description": COL_VIDEO_DESCRIPTION_PUNJABI,
            "youtube": COL_YOUTUBE_PUNJABI,
            "plio": COL_PLIO_PUNJABI,
        },
    }
    translations = []
    for language, config in configs.items():
        if language == "English" and not CREATE_DEFAULT_LANGUAGE_VIDEO_TRANSLATION:
            continue
        translated_description = clean(row[col_to_index(config["description"])].text)
        youtube_url = cell_url(row[col_to_index(config["youtube"])])
        plio_url = cell_url(row[col_to_index(config["plio"])])
        if not translated_description and not youtube_url and not plio_url:
            continue
        translations.append(
            {
                "doctype": "VideoTranslation",
                "language": language,
                "translated_description": translated_description,
                "video_youtube_url": youtube_url,
                "video_plio_url": plio_url,
            }
        )
    return translations


def build_assignment_translations(row: list[Cell], assignment_name: str) -> list[dict[str, str]]:
    configs = {
        "Hindi": COL_HINDI_SCRIPT,
        "Marathi": COL_MARATHI_SCRIPT,
        "Punjabi": COL_PUNJABI_SCRIPT,
    }
    translations = []
    for language, column in configs.items():
        translated_guidelines = clean(row[col_to_index(column)].text)
        if is_blankish(translated_guidelines):
            continue
        translations.append(
            {
                "doctype": "AssignmentTranslation",
                "language": language,
                "translated_title": assignment_name,
                "translated_guidelines": translated_guidelines,
            }
        )
    return translations


def build_submission_media_links(row: list[Cell]) -> list[dict[str, str]]:
    links = []
    configs = [
        ("Drive", "English", COL_DRIVE_HINGLISH),
        ("Drive", "Hindi", COL_DRIVE_HINDI),
        ("Drive", "Marathi", COL_DRIVE_MARATHI),
        ("Drive", "Punjabi", COL_DRIVE_PUNJABI),
        ("YouTube", "English", COL_YOUTUBE_HINGLISH),
        ("YouTube", "Hindi", COL_YOUTUBE_HINDI),
        ("YouTube", "Marathi", COL_YOUTUBE_MARATHI),
        ("YouTube", "Punjabi", COL_YOUTUBE_PUNJABI),
        ("Canva", "English", COL_CANVA_HINGLISH),
        ("Canva", "Hindi", COL_CANVA_HINDI),
        ("Canva", "Marathi", COL_CANVA_MARATHI),
        ("Canva", "Punjabi", COL_CANVA_PUNJABI),
    ]
    for link_type, language, column in configs:
        url = strip_url_tracking_parameters(cell_url(row[col_to_index(column)]))
        if not url:
            continue
        links.append(
            {
                "doctype": "Assignment Submission Media Link",
                "link_type": link_type,
                "language": language,
                "link": url,
            }
        )
    return links


def resolve_course_vertical(master_vertical: str) -> str:
    vertical = clean(master_vertical)
    if vertical not in COURSE_VERTICAL_NAME_MAP:
        allowed = ", ".join(sorted(COURSE_VERTICAL_NAME_MAP))
        raise RuntimeError(f"Unsupported vertical from source sheet: {vertical!r}. Expected one of: {allowed}")
    return COURSE_VERTICAL_NAME_MAP[vertical]


def assignment_flow_course_name(course_vertical: str) -> str:
    return ASSIGNMENT_FLOW_COURSE_NAME_MAP.get(course_vertical, course_vertical)


def assignment_flow_output_row(
    activity: ActivityRow,
    course_vertical: str,
    assignment_value: str,
) -> dict[str, str]:
    return {
        "course": assignment_flow_course_name(course_vertical),
        "level": activity.master.level,
        "grade": activity.master.grade,
        "assignment_column": assignment_flow_column_name(activity.unit_activity),
        "assignment_value": clean(assignment_value),
        "assignment_value_kind": "skipped" if clean(assignment_value) == HK_EMPTY_SKIP_OUTPUT else "assignment",
    }


def assignment_flow_column_name(unit_activity: str) -> str:
    column_suffix = re.sub(r"[^A-Za-z0-9]+", "_", clean(unit_activity)).strip("_")
    return f"assignment_id_{column_suffix or 'unit'}"


def course_level_course_name(program: str, course: str) -> str:
    program = clean(program)
    course_name = clean(course)
    if not program or normalize(course_name).startswith(normalize(program)):
        return course_name
    return f"{program} - {course_name}"


def parse_unit_filters(text: str) -> list[tuple[str, int]]:
    """Parse unit filters written like the scripts' UNIT_FILTERS: [("Baseline", 0), ("Unit 1", 3)].

    A single ("Baseline", 0) pair is accepted too. Blank text or [] means no filters.
    """
    if not clean(text):
        return []

    try:
        value = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f'Unit Filters must look like [("Baseline", 0), ("Unit 1 Activity CA1", 3)]: {exc}') from exc

    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError('Unit Filters must be a list, e.g. [("Baseline", 0)]')

    filters = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"Each unit filter must be (unit activity, week no), got {item!r}")
        unit_activity, week_no = item
        if not isinstance(unit_activity, str) or not clean(unit_activity):
            raise ValueError(f"Unit activity must be non-empty text, got {unit_activity!r}")
        if isinstance(week_no, bool) or not isinstance(week_no, int) or week_no < 0:
            raise ValueError(f"Week no must be a whole number of 0 or more, got {week_no!r} for {unit_activity!r}")
        filters.append((clean(unit_activity), week_no))
    return filters


def match_unit_filter(config: ImportConfig, unit_activity: str) -> tuple[bool, int]:
    if not config.unit_filters:
        return True, config.default_week_no

    normalized_unit = normalize(unit_activity)
    for unit_filter, week_no in config.unit_filters:
        normalized_filter = normalize(unit_filter)
        if config.unit_match_mode == MATCH_PREFIX:
            matched = normalized_unit.startswith(normalized_filter)
        else:
            matched = normalized_unit == normalized_filter
        if matched:
            return True, week_no
    return False, config.default_week_no


def matches_target_filter(value: str, target: str) -> bool:
    if not clean(target):
        return True
    return normalize(value) == normalize(target)


def map_learning_unit_activity_type(activity_type: str) -> str:
    value = normalize(activity_type)
    if "baseline" in value:
        return "baseline"
    if "challenge" in value:
        return "challenge"
    if "engagement" in value:
        return "engagement"
    return "regular"


def map_assignment_activity_type(activity_type: str) -> str:
    return "Challenge" if "challenge" in normalize(activity_type) else "Regular"


def fill_merged_header_sections(header: list[Cell]) -> list[str]:
    sections = []
    current = ""
    for cell in header:
        text = normalize(cell.text)
        if text:
            current = text
        sections.append(current)
    return sections


def language_from_quiz_section(section: str) -> str:
    normalized_section = normalize(section)
    for language in LANGUAGES:
        if normalized_section == f"{normalize(language)} quiz":
            return language
    return ""


def parse_question_number(text: str) -> int | None:
    match = re.search(r"question\s*(\d+)", text or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_correct_option(text: str) -> int:
    match = re.search(r"option\s*([A-Z])", text or "", flags=re.IGNORECASE)
    if match:
        return ord(match.group(1).upper()) - ord("A") + 1
    match = re.search(r"\b([A-D])\b", text or "", flags=re.IGNORECASE)
    if match:
        return ord(match.group(1).upper()) - ord("A") + 1
    if str(text).strip().isdigit():
        return int(str(text).strip())
    return 0


def parse_referenced_rows(text: str) -> list[int]:
    match = re.search(r"rows?\s*:?\s*([0-9,\s-]+)", text or "", flags=re.IGNORECASE)
    if not match:
        return []
    values = []
    for chunk in re.split(r"[,\s]+", match.group(1).strip()):
        if "-" in chunk:
            start, end = chunk.split("-", 1)
            if start.isdigit() and end.isdigit():
                values.extend(range(int(start), int(end) + 1))
        elif chunk.isdigit():
            values.append(int(chunk))
    return values


def parse_referenced_rows_from_url(url: str) -> list[int]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    fragment_query = parse_qs(parsed.fragment)
    range_values = query.get("range") or fragment_query.get("range") or []
    if not range_values:
        fragment_range = re.search(r"(?:^|&)range=([^&]+)", parsed.fragment or "")
        if fragment_range:
            range_values = [fragment_range.group(1)]

    if not range_values:
        return []
    return parse_rows_from_a1_reference(range_values[0])


def parse_rows_from_a1_reference(reference: str) -> list[int]:
    match = re.search(r"[A-Z]+(\d+)(?::[A-Z]+(\d+))?", reference or "", flags=re.IGNORECASE)
    if not match:
        return []
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return list(range(start, end + 1))


def renumber_questions(questions: list[QuizQuestionData]) -> list[QuizQuestionData]:
    return [
        QuizQuestionData(
            question_number=index,
            question=question.question,
            options=question.options,
            correct_option=question.correct_option,
            translations=question.translations,
            option_translations=question.option_translations,
        )
        for index, question in enumerate(questions, start=1)
    ]


def make_question_key(unit_activity: str, activity_name: str, question_number: int) -> str:
    return f"{slug(unit_activity)}-{slug(activity_name)}-Q{question_number}"


def get_list_value(values: list[str], index: int) -> str:
    return values[index] if 0 <= index < len(values) else ""


def slug(value: str) -> str:
    clean_value = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-")
    return clean_value[:120] or "item"


def make_doc_name(prefix: str, value: str) -> str:
    return f"{prefix}-{slug(value)}"[:140]
