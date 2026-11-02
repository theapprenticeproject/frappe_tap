"""Google Sheets API client and cell/range helpers used by the learning unit import."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import credentials as user_credentials
from google.oauth2 import service_account


GOOGLE_SHEETS_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
)
REQUEST_TIMEOUT = 60
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


@dataclass
class Cell:
    text: str = ""
    hyperlink: str = ""


@dataclass
class SheetRef:
    spreadsheet_id: str
    gid: int | None


class GoogleSheetsClient:
    def __init__(self, credentials_info: dict[str, Any]):
        self.service_account_email = str(credentials_info.get("client_email") or "")
        self.session = AuthorizedSession(load_credentials(credentials_info))

    def get_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        fields = (
            "properties.title,"
            "sheets.properties(sheetId,title,index,"
            "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount))"
        )
        return self._get_json(f"{SHEETS_API_BASE}/{spreadsheet_id}", {"fields": fields})

    def get_grid(self, spreadsheet_id: str, sheet_name: str, a1_range: str) -> list[list[Cell]]:
        full_range = f"{quote_sheet_name(sheet_name)}!{a1_range}"
        fields = (
            "sheets(data(startRow,startColumn,rowData(values("
            "formattedValue,effectiveValue,userEnteredValue,hyperlink,"
            "textFormatRuns(format(link),startIndex)"
            "))))"
        )
        data = self._get_json(
            f"{SHEETS_API_BASE}/{spreadsheet_id}",
            {"ranges": full_range, "includeGridData": "true", "fields": fields},
        )
        sheet_data = (data.get("sheets") or [{}])[0].get("data") or []
        if not sheet_data:
            return []

        grid = sheet_data[0]
        start_col = int(grid.get("startColumn") or 0)
        requested_width = range_width(a1_range)
        rows = []
        for row_data in grid.get("rowData") or []:
            values = row_data.get("values") or []
            row = [Cell() for _ in range(requested_width)]
            for offset, raw_cell in enumerate(values):
                col_index = start_col + offset - col_to_index(a1_range_start_col(a1_range))
                if 0 <= col_index < requested_width:
                    row[col_index] = parse_cell(raw_cell)
            rows.append(row)
        return rows

    def get_values(self, spreadsheet_id: str, sheet_name: str) -> list[list[str]]:
        range_name = quote_sheet_name(sheet_name)
        data = self._get_json(
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{quote(range_name, safe='')}",
            {"majorDimension": "ROWS"},
        )
        return [[clean(value) for value in row] for row in data.get("values") or []]

    def replace_values(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        values: list[list[str]],
        previous_values: list[list[str]] | None = None,
    ) -> None:
        clear_width = max_table_width(values, previous_values or [])
        clear_rows = max(len(values), len(previous_values or []), 1)
        clear_range = f"{quote_sheet_name(sheet_name)}!A1:{index_to_col(clear_width - 1)}{clear_rows}"
        self._post_json(
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{quote(clear_range, safe='')}:clear",
            {},
        )

        if not values:
            return

        write_width = max_table_width(values)
        write_range = f"{quote_sheet_name(sheet_name)}!A1:{index_to_col(write_width - 1)}{len(values)}"
        self._post_json(
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values:batchUpdate",
            {
                "valueInputOption": "RAW",
                "data": [{"range": write_range, "majorDimension": "ROWS", "values": values}],
            },
        )

    def format_assignment_output_cells(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        row_count: int,
        assignment_column_indexes: list[int],
        skipped_cells: list[tuple[int, int]],
    ) -> None:
        if row_count <= 1 or not assignment_column_indexes:
            return

        requests = []
        for column_index in assignment_column_indexes:
            requests.append(
                repeat_cell_text_color_request(
                    sheet_id, 1, row_count, column_index, {"red": 0.0, "green": 0.0, "blue": 0.0}
                )
            )
        for row_index, column_index in skipped_cells:
            requests.append(
                repeat_cell_text_color_request(
                    sheet_id, row_index, row_index + 1, column_index, {"red": 1.0, "green": 0.0, "blue": 0.0}
                )
            )

        self._post_json(f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate", {"requests": requests})

    def verify_write_access(self, spreadsheet_id: str, sheet_id: int, sheet_name: str) -> None:
        # Renames the tab to its own title: a no-op that fails without edit access.
        self._post_json(
            f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            {
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": int(sheet_id), "title": sheet_name},
                            "fields": "title",
                        }
                    }
                ]
            },
        )

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", url, params=params)

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", url, json_body=body)

    def _request_json(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.session.request(
            method,
            url,
            params=params or {},
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            hint = ""
            if response.status_code in (403, 404) and self.service_account_email:
                hint = f" Make sure the sheet is shared with the service account {self.service_account_email}."
            raise RuntimeError(
                f"Google Sheets API failed {response.status_code}: {response.text[:1000]}{hint}"
            )
        return response.json() if response.content else {}


def load_credentials(credentials_info: dict[str, Any]):
    credential_type = credentials_info.get("type")

    if credential_type == "service_account":
        return service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=list(GOOGLE_SHEETS_SCOPES),
        )

    if credential_type == "authorized_user" or credentials_info.get("refresh_token"):
        # Do not force scopes for authorized-user credentials. The OAuth client
        # rejects explicit scopes but works with its stored grant.
        return user_credentials.Credentials.from_authorized_user_info(credentials_info)

    present_keys = ", ".join(sorted(credentials_info)) or "none"
    raise RuntimeError(
        "Google credentials in GCS Settings must be a service account key "
        "(\"type\": \"service_account\") or an authorized user file "
        f"(\"type\": \"authorized_user\"). Found type={credential_type!r}, keys: {present_keys}"
    )


def repeat_cell_text_color_request(
    sheet_id: int,
    start_row_index: int,
    end_row_index: int,
    column_index: int,
    color: dict[str, float],
) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": int(sheet_id),
                "startRowIndex": start_row_index,
                "endRowIndex": end_row_index,
                "startColumnIndex": column_index,
                "endColumnIndex": column_index + 1,
            },
            "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": color}}},
            "fields": "userEnteredFormat.textFormat.foregroundColor",
        }
    }


def parse_cell(raw_cell: dict[str, Any]) -> Cell:
    return Cell(text=cell_text(raw_cell), hyperlink=cell_hyperlink(raw_cell))


def cell_text(raw_cell: dict[str, Any]) -> str:
    if "formattedValue" in raw_cell:
        return str(raw_cell.get("formattedValue") or "")

    effective = raw_cell.get("effectiveValue") or {}
    for key in ("stringValue", "numberValue", "boolValue"):
        if key in effective:
            return str(effective[key])

    entered = raw_cell.get("userEnteredValue") or {}
    for key in ("stringValue", "numberValue", "boolValue"):
        if key in entered:
            return str(entered[key])
    return ""


def cell_hyperlink(raw_cell: dict[str, Any]) -> str:
    if raw_cell.get("hyperlink"):
        return str(raw_cell["hyperlink"])

    for run in raw_cell.get("textFormatRuns") or []:
        uri = ((run.get("format") or {}).get("link") or {}).get("uri")
        if uri:
            return str(uri)

    formula = (raw_cell.get("userEnteredValue") or {}).get("formulaValue") or ""
    match = re.search(r'HYPERLINK\("([^"]+)"', formula, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def cell_url(cell: Cell) -> str:
    if not cell.hyperlink:
        return ""
    if is_blankish(cell.text):
        return ""
    return cell.hyperlink.strip()


def strip_url_tracking_parameters(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    retained_parameters = [(key, value) for key, value in parameters if not key.lower().startswith("utm_")]
    if len(retained_parameters) == len(parameters):
        return url

    return parsed._replace(query=urlencode(retained_parameters)).geturl()


def parse_sheet_ref(url: str, fallback_spreadsheet_id: str = "") -> SheetRef:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if match:
        spreadsheet_id = match.group(1)
    elif fallback_spreadsheet_id:
        spreadsheet_id = fallback_spreadsheet_id
    else:
        raise ValueError(f"Could not parse Google spreadsheet id from URL: {url}")

    parsed = urlparse(url)
    gid = None
    query_gid = parse_qs(parsed.query).get("gid")
    if query_gid:
        gid = int(query_gid[0])
    else:
        fragment_gid = re.search(r"gid=(\d+)", parsed.fragment or "")
        if fragment_gid:
            gid = int(fragment_gid.group(1))
    return SheetRef(spreadsheet_id=spreadsheet_id, gid=gid)


def resolve_sheet_name(metadata: dict[str, Any], gid: int | None, preferred_name: str) -> str:
    sheets = metadata.get("sheets") or []
    if gid is not None:
        for sheet in sheets:
            props = sheet.get("properties") or {}
            if int(props.get("sheetId")) == int(gid):
                return str(props.get("title") or "")

    preferred = normalize(preferred_name)
    if preferred:
        for sheet in sheets:
            title = str((sheet.get("properties") or {}).get("title") or "")
            if normalize(title) == preferred:
                return title

    if sheets:
        first = sorted(sheets, key=lambda item: int((item.get("properties") or {}).get("index") or 0))[0]
        return str((first.get("properties") or {}).get("title") or "")
    raise RuntimeError("Spreadsheet has no sheets")


def sheet_id_for_name(metadata: dict[str, Any], sheet_name: str) -> int:
    for sheet in metadata.get("sheets") or []:
        props = sheet.get("properties") or {}
        if props.get("title") == sheet_name:
            return int(props.get("sheetId"))
    raise RuntimeError(f"Could not resolve sheet id for tab: {sheet_name}")


def sheet_row_count(metadata: dict[str, Any], sheet_name: str) -> int:
    for sheet in metadata.get("sheets") or []:
        props = sheet.get("properties") or {}
        if props.get("title") == sheet_name:
            grid = props.get("gridProperties") or {}
            return int(grid.get("rowCount") or 1000)
    return 1000


def get_cell_text(row: list[Cell], index: int) -> str:
    return row[index].text if 0 <= index < len(row) else ""


def clean(value: Any) -> str:
    return str(value or "").strip()


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def is_blankish(value: Any) -> bool:
    return normalize(value) in {"", "na", "n/a", "none", "null", "-", "tbd"}


def quote_sheet_name(sheet_name: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def col_to_index(column: str) -> int:
    index = 0
    for char in column.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def index_to_col(index: int) -> str:
    index += 1
    letters = []
    while index:
        index, remainder = divmod(index - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def a1_range_start_col(a1_range: str) -> str:
    match = re.match(r"([A-Z]+)\d+", a1_range, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"Could not parse A1 start column: {a1_range}")
    return match.group(1).upper()


def a1_range_end_col(a1_range: str) -> str:
    match = re.match(r"[A-Z]+\d+:([A-Z]+)\d+", a1_range, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"Could not parse A1 end column: {a1_range}")
    return match.group(1).upper()


def range_width(a1_range: str) -> int:
    return col_to_index(a1_range_end_col(a1_range)) - col_to_index(a1_range_start_col(a1_range)) + 1


def max_table_width(*tables: list[list[Any]]) -> int:
    widths = [len(row) for table in tables for row in table]
    return max(widths or [1])
