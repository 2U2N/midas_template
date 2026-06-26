#!/usr/bin/env python3
"""Create a conservative, sanitized data-shape report in the vault.

The report is meant to help a human researcher describe data structure to an
AI coding agent without exposing observations. It never prints file paths, raw
rows, exact values, exact timestamps, extrema, free-text examples, or stack
traces.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import re
import sqlite3
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Iterable


MISSING_STRINGS = {"", "na", "n/a", "nan", "null", "none", "."}
DISTINCT_TRACK_LIMIT = 10000
IDENTIFIER_NAME_RE = re.compile(
    r"(^|_)(id|uuid|guid|name|user|username|email|url|uri|phone|address|token|key)($|_)",
    re.IGNORECASE,
)
DATETIME_NAME_RE = re.compile(r"(date|time|timestamp|created|updated)", re.IGNORECASE)
TEXT_NAME_RE = re.compile(r"(text|body|content|message|comment|transcript|caption)", re.IGNORECASE)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a sanitized structural data-shape report."
    )
    parser.add_argument("--input", required=True, help="Vault-side data file or folder.")
    parser.add_argument(
        "--output",
        default="data_shape_report.md",
        help="Markdown report path. Review before copying anything to PROJECT_BRIEF.md.",
    )
    parser.add_argument(
        "--allow-category-labels",
        action="store_true",
        help="Opt in to printing common category labels without counts.",
    )
    parser.add_argument(
        "--category-min-count",
        type=int,
        default=20,
        help="Minimum count for any category label printed with --allow-category-labels.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    reports = scan_input(
        input_path=input_path,
        output_path=output_path,
        allow_category_labels=args.allow_category_labels,
        category_min_count=max(args.category_min_count, 2),
    )
    output_path.write_text(render_report(reports), encoding="utf-8")
    return 0


class ColumnProfile:
    def __init__(
        self,
        name: str,
        prior_rows: int,
        allow_category_labels: bool,
        category_min_count: int,
    ) -> None:
        self.name = str(name)
        self.total = prior_rows
        self.missing = prior_rows
        self.nonmissing = 0
        self.type_hits: Counter[str] = Counter()
        self.distinct_hashes: set[str] = set()
        self.distinct_overflow = False
        self.max_text_len = 0
        self.has_whitespace_text = False
        self.allow_category_labels = allow_category_labels
        self.category_min_count = category_min_count
        self.category_counts: Counter[str] = Counter()

    def observe(self, value: Any) -> None:
        self.total += 1
        if is_missing(value):
            self.missing += 1
            return

        text = normalize_value(value)
        self.nonmissing += 1
        self.type_hits[infer_value_type(value, text)] += 1
        self.max_text_len = max(self.max_text_len, len(text))
        if re.search(r"\s", text):
            self.has_whitespace_text = True

        if len(self.distinct_hashes) < DISTINCT_TRACK_LIMIT:
            digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
            self.distinct_hashes.add(digest)
        else:
            self.distinct_overflow = True

        if self.allow_category_labels and is_label_candidate(text):
            self.category_counts[text] += 1

    def inferred_type(self) -> str:
        if self.nonmissing == 0:
            return "all_missing"
        if self.type_hits["datetime"] / self.nonmissing >= 0.8:
            return "datetime_like"
        if self.type_hits["numeric"] / self.nonmissing >= 0.8:
            return "numeric_like"
        if self.type_hits["boolean"] / self.nonmissing >= 0.8:
            return "boolean_like"
        if self.is_free_text_like():
            return "free_text_like"
        if self.is_categorical_like():
            return "categorical_like"
        return "text_or_mixed"

    def is_identifier_like(self) -> bool:
        if IDENTIFIER_NAME_RE.search(self.name):
            return True
        if self.nonmissing < 10:
            return False
        if self.distinct_overflow:
            return True
        return len(self.distinct_hashes) / max(self.nonmissing, 1) >= 0.9

    def is_datetime_like(self) -> bool:
        return DATETIME_NAME_RE.search(self.name) is not None or self.inferred_type() == "datetime_like"

    def is_free_text_like(self) -> bool:
        return (
            TEXT_NAME_RE.search(self.name) is not None
            or self.max_text_len > 120
            or (self.max_text_len > 40 and self.has_whitespace_text)
        )

    def is_categorical_like(self) -> bool:
        if self.nonmissing == 0 or self.distinct_overflow or self.is_free_text_like():
            return False
        distinct_count = len(self.distinct_hashes)
        return distinct_count <= 30 and distinct_count <= max(5, self.nonmissing * 0.2)

    def duplicate_key_risk(self) -> str:
        if not IDENTIFIER_NAME_RE.search(self.name):
            return ""
        if self.distinct_overflow:
            return "duplicate_check_not_assessed"
        if self.nonmissing > len(self.distinct_hashes):
            return "possible_duplicate_key_values"
        return ""

    def flags(self) -> list[str]:
        flags: list[str] = []
        if self.is_identifier_like():
            flags.append("identifier_like")
        if self.is_categorical_like():
            flags.append("categorical_like")
        if self.inferred_type() == "numeric_like":
            flags.append("numeric_like")
        if self.is_datetime_like():
            flags.append("datetime_like")
        if self.is_free_text_like():
            flags.append("free_text_like")
        duplicate_flag = self.duplicate_key_risk()
        if duplicate_flag:
            flags.append(duplicate_flag)
        return flags

    def category_labels(self) -> str:
        if not self.allow_category_labels or not self.is_categorical_like():
            return "suppressed"
        labels = [
            label
            for label, count in self.category_counts.most_common()
            if count >= self.category_min_count
        ]
        if not labels:
            return "none printed"
        return "; ".join(markdown_cell(label) for label in labels[:10])


class DatasetProfile:
    def __init__(
        self,
        alias: str,
        file_format: str,
        allow_category_labels: bool,
        category_min_count: int,
        display_name: str | None = None,
    ) -> None:
        self.alias = alias
        self.display_name = display_name
        self.file_format = file_format
        self.row_count = 0
        self.columns: OrderedDict[str, ColumnProfile] = OrderedDict()
        self.notes: list[str] = []
        self.allow_category_labels = allow_category_labels
        self.category_min_count = category_min_count

    def ensure_column(self, name: str) -> ColumnProfile:
        name = str(name)
        if name not in self.columns:
            self.columns[name] = ColumnProfile(
                name=name,
                prior_rows=self.row_count,
                allow_category_labels=self.allow_category_labels,
                category_min_count=self.category_min_count,
            )
        return self.columns[name]

    def observe_row(self, row: dict[str, Any]) -> None:
        clean_row = {str(key): value for key, value in row.items() if key is not None}
        for name in clean_row:
            self.ensure_column(name)
        self.row_count += 1
        for name, profile in self.columns.items():
            profile.observe(clean_row.get(name))


class DatasetNote:
    def __init__(
        self,
        alias: str,
        file_format: str,
        note: str,
        display_name: str | None = None,
    ) -> None:
        self.alias = alias
        self.display_name = display_name
        self.file_format = file_format
        self.note = note


def scan_input(
    input_path: Path,
    output_path: Path,
    allow_category_labels: bool,
    category_min_count: int,
) -> list[DatasetProfile | DatasetNote]:
    if input_path.is_file():
        files = [input_path]
    elif input_path.is_dir():
        files = [
            path
            for path in sorted(input_path.rglob("*"), key=lambda item: str(item).lower())
            if path.is_file() and not has_hidden_part(path) and path.resolve() != output_path.resolve()
        ]
    else:
        return [DatasetNote("dataset_1", "unknown", "Input was not found.")]

    reports: list[DatasetProfile | DatasetNote] = []
    for index, path in enumerate(files, start=1):
        dataset_reports = read_dataset(
            path=path,
            alias=f"dataset_{index}",
            allow_category_labels=allow_category_labels,
            category_min_count=category_min_count,
        )
        for report in dataset_reports:
            report.display_name = path.name
        reports.extend(dataset_reports)
    if not reports:
        return [DatasetNote("dataset_1", "unknown", "No readable files were found.")]
    return reports


def read_dataset(
    path: Path,
    alias: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> list[DatasetProfile | DatasetNote]:
    extension = path.suffix.lower()
    try:
        if extension == ".csv":
            return [read_delimited(path, alias, "CSV", ",", allow_category_labels, category_min_count)]
        if extension in {".tsv", ".tab"}:
            return [read_delimited(path, alias, "TSV", "\t", allow_category_labels, category_min_count)]
        if extension in {".jsonl", ".ndjson"}:
            return [read_json_lines(path, alias, allow_category_labels, category_min_count)]
        if extension == ".json":
            return [read_json_file(path, alias, allow_category_labels, category_min_count)]
        if extension in {".sqlite", ".sqlite3", ".db"}:
            return read_sqlite(path, alias, allow_category_labels, category_min_count)
        if extension in {".xlsx", ".xls"}:
            return read_excel(path, alias, allow_category_labels, category_min_count)
        if extension in {".dta", ".sav", ".zsav", ".por", ".sas7bdat", ".xpt", ".parquet", ".feather"}:
            return read_with_pandas(path, alias, extension, allow_category_labels, category_min_count)
        if extension in {".rds", ".rdata"}:
            return read_with_pyreadr(path, alias, allow_category_labels, category_min_count)
        if extension == ".duckdb":
            return read_duckdb(path, alias, allow_category_labels, category_min_count)
        if extension in {".zip", ".tar", ".gz", ".tgz", ".7z", ".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".mp3", ".mp4"}:
            return [DatasetNote(alias, extension_label(extension), "Unsupported file type. Not parsed in v1.")]
        return [DatasetNote(alias, extension_label(extension), "Unsupported or unknown file type. Not parsed.")]
    except Exception as exc:  # noqa: BLE001 - intentionally suppress details that may contain paths or values.
        return [
            DatasetNote(
                alias,
                extension_label(extension),
                f"Reader failed safely ({exc.__class__.__name__}); no details printed.",
            )
        ]


def read_delimited(
    path: Path,
    alias: str,
    file_format: str,
    delimiter: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> DatasetProfile:
    profile = DatasetProfile(alias, file_format, allow_category_labels, category_min_count)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            profile.notes.append("No header row detected.")
            return profile
        for row in reader:
            profile.observe_row(row)
    return profile


def read_json_lines(
    path: Path,
    alias: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> DatasetProfile:
    profile = DatasetProfile(alias, "JSONL", allow_category_labels, category_min_count)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                profile.observe_row(flatten_shallow(item))
            else:
                profile.notes.append("Non-object records were present and skipped.")
    return profile


def read_json_file(
    path: Path,
    alias: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> DatasetProfile:
    profile = DatasetProfile(alias, "JSON", allow_category_labels, category_min_count)
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    for row in json_rows(data):
        profile.observe_row(row)
    if profile.row_count == 0:
        profile.notes.append("No object-like records detected.")
    return profile


def read_sqlite(
    path: Path,
    alias: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> list[DatasetProfile | DatasetNote]:
    reports: list[DatasetProfile | DatasetNote] = []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        table_rows = connection.execute(
            "select name from sqlite_master where type = 'table' order by name"
        ).fetchall()
        if not table_rows:
            return [DatasetNote(alias, "SQLite", "No tables detected.")]
        for table_index, (table_name,) in enumerate(table_rows, start=1):
            profile = DatasetProfile(
                f"{alias}_table_{table_index}",
                "SQLite table",
                allow_category_labels,
                category_min_count,
            )
            cursor = connection.execute(f"select * from {quote_sql_identifier(table_name)}")
            columns = [description[0] for description in cursor.description or []]
            for values in cursor:
                profile.observe_row(dict(zip(columns, values)))
            reports.append(profile)
    finally:
        connection.close()
    return reports


def read_excel(
    path: Path,
    alias: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> list[DatasetProfile | DatasetNote]:
    pandas = optional_import("pandas")
    if pandas is None:
        return [DatasetNote(alias, "Excel", "Optional dependency missing: pandas/openpyxl.")]
    sheets = pandas.read_excel(path, sheet_name=None)
    reports: list[DatasetProfile | DatasetNote] = []
    for sheet_index, frame in enumerate(sheets.values(), start=1):
        reports.append(
            profile_dataframe(
                frame,
                f"{alias}_sheet_{sheet_index}",
                "Excel sheet",
                allow_category_labels,
                category_min_count,
            )
        )
    return reports


def read_with_pandas(
    path: Path,
    alias: str,
    extension: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> list[DatasetProfile | DatasetNote]:
    pandas = optional_import("pandas")
    if pandas is None:
        return [DatasetNote(alias, extension_label(extension), "Optional dependency missing: pandas.")]
    readers = {
        ".dta": ("Stata", pandas.read_stata),
        ".sav": ("SPSS", pandas.read_spss),
        ".zsav": ("SPSS", pandas.read_spss),
        ".por": ("SPSS", pandas.read_spss),
        ".sas7bdat": ("SAS", pandas.read_sas),
        ".xpt": ("SAS transport", pandas.read_sas),
        ".parquet": ("Parquet", pandas.read_parquet),
        ".feather": ("Feather", pandas.read_feather),
    }
    file_format, reader = readers[extension]
    frame = reader(path)
    return [profile_dataframe(frame, alias, file_format, allow_category_labels, category_min_count)]


def read_with_pyreadr(
    path: Path,
    alias: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> list[DatasetProfile | DatasetNote]:
    pyreadr = optional_import("pyreadr")
    if pyreadr is None:
        return [DatasetNote(alias, "R data", "Optional dependency missing: pyreadr.")]
    result = pyreadr.read_r(path)
    reports: list[DatasetProfile | DatasetNote] = []
    for object_index, frame in enumerate(result.values(), start=1):
        reports.append(
            profile_dataframe(
                frame,
                f"{alias}_object_{object_index}",
                "R data object",
                allow_category_labels,
                category_min_count,
            )
        )
    return reports or [DatasetNote(alias, "R data", "No tabular objects detected.")]


def read_duckdb(
    path: Path,
    alias: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> list[DatasetProfile | DatasetNote]:
    duckdb = optional_import("duckdb")
    if duckdb is None:
        return [DatasetNote(alias, "DuckDB", "Optional dependency missing: duckdb.")]
    reports: list[DatasetProfile | DatasetNote] = []
    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = connection.execute("show tables").fetchall()
        for table_index, (table_name,) in enumerate(tables, start=1):
            frame = connection.execute(f"select * from {quote_sql_identifier(table_name)}").fetchdf()
            reports.append(
                profile_dataframe(
                    frame,
                    f"{alias}_table_{table_index}",
                    "DuckDB table",
                    allow_category_labels,
                    category_min_count,
                )
            )
    finally:
        connection.close()
    return reports or [DatasetNote(alias, "DuckDB", "No tables detected.")]


def profile_dataframe(
    frame: Any,
    alias: str,
    file_format: str,
    allow_category_labels: bool,
    category_min_count: int,
) -> DatasetProfile:
    profile = DatasetProfile(alias, file_format, allow_category_labels, category_min_count)
    for record in frame.to_dict(orient="records"):
        profile.observe_row(record)
    return profile


def json_rows(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield flatten_shallow(item)
    elif isinstance(data, dict):
        if all(isinstance(value, list) for value in data.values()):
            keys = list(data.keys())
            max_len = max((len(data[key]) for key in keys), default=0)
            for index in range(max_len):
                yield {
                    key: data[key][index] if index < len(data[key]) else None
                    for key in keys
                }
        elif all(isinstance(value, dict) for value in data.values()):
            for value in data.values():
                yield flatten_shallow(value)
        else:
            yield flatten_shallow(data)


def flatten_shallow(item: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, (dict, list)):
            row[str(key)] = "[nested]"
        else:
            row[str(key)] = value
    return row


def render_report(reports: list[DatasetProfile | DatasetNote]) -> str:
    lines = [
        "# Sanitized Data Shape Report",
        "",
        "Review this report before copying any part of it into `PROJECT_BRIEF.md`.",
        "",
        "The report suppresses file paths, raw rows, exact values, exact timestamps, "
        "extrema, free-text examples, stack traces, and exact row counts.",
        "",
        "Column names are printed because they are often needed for code generation. "
        "Remove or generalize any column name that is itself identifying before sharing with Midas.",
        "",
        "Base file names are printed to help users match reports to their data files. "
        "Remove or generalize file names before sharing if the names contain sensitive details.",
        "",
    ]
    for report in reports:
        if isinstance(report, DatasetNote):
            lines.extend(render_note(report))
        else:
            lines.extend(render_profile(report))
    return "\n".join(lines).rstrip() + "\n"


def render_note(note: DatasetNote) -> list[str]:
    return [
        f"## {markdown_cell(note.alias)}",
        "",
        f"- file name: {markdown_cell(note.display_name or 'not available')}",
        f"- format: {markdown_cell(note.file_format)}",
        f"- status: {markdown_cell(note.note)}",
        "",
    ]


def render_profile(profile: DatasetProfile) -> list[str]:
    lines = [
        f"## {markdown_cell(profile.alias)}",
        "",
        f"- file name: {markdown_cell(profile.display_name or 'not available')}",
        f"- format: {markdown_cell(profile.file_format)}",
        f"- row count: {row_bucket(profile.row_count)}",
        f"- column count: {len(profile.columns)}",
    ]
    if profile.notes:
        lines.append("- notes: " + "; ".join(markdown_cell(note) for note in profile.notes))
    lines.extend(
        [
            "",
            "| column | inferred type | missingness | text length | flags | category labels |",
            "|---|---|---|---|---|---|",
        ]
    )
    for column in profile.columns.values():
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(column.name),
                    markdown_cell(column.inferred_type()),
                    missingness_bucket(column.missing, column.total),
                    markdown_cell(text_length_bucket(column.max_text_len)),
                    markdown_cell(", ".join(column.flags()) or "none"),
                    markdown_cell(column.category_labels()),
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    text = normalize_value(value)
    return text.strip().lower() in MISSING_STRINGS


def normalize_value(value: Any) -> str:
    return str(value).strip()


def infer_value_type(value: Any, text: str) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "numeric"
    lowered = text.lower()
    if lowered in {"true", "false", "yes", "no"}:
        return "boolean"
    try:
        float(text)
        return "numeric"
    except ValueError:
        pass
    if looks_datetime(text):
        return "datetime"
    return "text"


def looks_datetime(text: str) -> bool:
    if len(text) < 6 or not re.search(r"\d", text):
        return False
    patterns = [
        r"^\d{4}-\d{2}-\d{2}",
        r"^\d{2}/\d{2}/\d{4}",
        r"^\d{2}\.\d{2}\.\d{4}",
        r"^\d{4}/\d{2}/\d{2}",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def row_bucket(count: int) -> str:
    if count == 0:
        return "0"
    if count < 10:
        return "1-9"
    if count < 100:
        return "10-99"
    if count < 1000:
        return "100-999"
    if count < 10000:
        return "1,000-9,999"
    if count < 100000:
        return "10,000-99,999"
    if count < 1000000:
        return "100,000-999,999"
    return "1,000,000+"


def missingness_bucket(missing: int, total: int) -> str:
    if total == 0:
        return "not assessed"
    if missing == 0:
        return "none observed"
    rate = missing / total
    if rate < 0.01:
        return "<1%"
    if rate < 0.05:
        return "1-5%"
    if rate < 0.20:
        return "5-20%"
    if rate < 0.50:
        return "20-50%"
    if rate < 1.0:
        return ">50%"
    return "all missing"


def text_length_bucket(max_len: int) -> str:
    if max_len == 0:
        return "none"
    if max_len <= 20:
        return "short"
    if max_len <= 120:
        return "medium"
    if max_len <= 500:
        return "long"
    return "very long"


def is_label_candidate(text: str) -> bool:
    return 0 < len(text) <= 60 and "\n" not in text and "\r" not in text


def markdown_cell(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def optional_import(module_name: str) -> Any | None:
    if importlib.util.find_spec(module_name) is None:
        return None
    return __import__(module_name)


def extension_label(extension: str) -> str:
    return extension.lstrip(".").upper() if extension else "unknown"


def quote_sql_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def has_hidden_part(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


if __name__ == "__main__":
    raise SystemExit(main())
