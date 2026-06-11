#!/usr/bin/env python3
from __future__ import annotations

import html
import csv
import difflib
import io
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


HOST = "0.0.0.0"
PORT = 8080
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
REFERENCE_DIR = Path(os.getenv("REFERENCE_DIR", str(APP_DIR / "reference_formats")))
INPUT_DIR = DATA_DIR / "input"
SUPPORT_DIR = DATA_DIR / "support"
OUTPUT_DIR = DATA_DIR / "output"
ARCHIVE_DIR = DATA_DIR / "archive"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
APP_TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)

USER_ROLE_COLUMNS = ("User_contact_number", "Role")
USER_ROLE_VALUES = {"FE", "ADMIN", "AM"}
TITAN_ENTITY_TYPES = {"ACTIVATE", "DEACTIVATE", "AADHAR", "PANCARD"}
DYNAMIC_VALUE_REFERENCE_JOBS = {
    "fmsc_migration",
    "Linehaul_route_mappings_v2",
    "lmsc_migration_cd",
    "sort_code_update",
    "fm_new_partner_location_onboarding",
    "lm_existing_partner_location_onboarding",
    "lm_new_partner_location_onboarding",
    "pending_manifest_corrections",
    "titan_user_migration_titan",
}


class ValidationReport:
    def __init__(
        self,
        *,
        job_key: str,
        uploaded_csv_name: str,
        expected_headers: list[str],
        actual_headers: list[str],
        row_count: int,
        findings: list[str],
        fixes: list[str],
        technical_summary: str,
    ) -> None:
        self.job_key = job_key
        self.uploaded_csv_name = uploaded_csv_name
        self.expected_headers = expected_headers
        self.actual_headers = actual_headers
        self.row_count = row_count
        self.findings = findings
        self.fixes = fixes
        self.technical_summary = technical_summary

    @property
    def has_issues(self) -> bool:
        return bool(self.findings)

    def prompt_summary(self) -> str:
        lines = [
            f"Selected Jenkins job: {self.job_key}",
            f"Uploaded CSV: {self.uploaded_csv_name}",
            f"Expected columns: {', '.join(self.expected_headers) if self.expected_headers else 'None'}",
            f"Uploaded columns: {', '.join(self.actual_headers) if self.actual_headers else 'None'}",
            f"Uploaded data rows: {self.row_count}",
        ]
        if self.findings:
            lines.append("Validation findings:")
            lines.extend(f"- {finding}" for finding in self.findings[:20])
            lines.append("Required fixes:")
            lines.extend(f"- {fix}" for fix in self.fixes[:20])
        else:
            lines.append("Validation findings: Uploaded CSV matches the stored correct format.")
            lines.append("Required fixes: None.")
        return "\n".join(lines)

    def plain_english_explanation(self) -> str:
        if not self.findings:
            return "\n".join(
                [
                    "Issue:",
                    "No CSV format problem was found for the selected Jenkins job.",
                    "",
                    "Fix:",
                    "No CSV format fix is needed. Check the Jenkins log for any script or system error.",
                    "",
                    "Problem found:",
                    "The uploaded CSV matches the stored correct format.",
                ]
            )

        lines = [
            "Issue:",
            f"The uploaded CSV does not match the correct format for {self.job_key}.",
            "",
            "Fix:",
        ]
        lines.extend(f"{index}. {fix}" for index, fix in enumerate(self.fixes, start=1))
        lines.extend(["", "Problem found:"])
        lines.extend(f"- {finding}" for finding in self.findings[:30])
        return "\n".join(lines)


def ensure_dirs() -> None:
    for folder in (INPUT_DIR, SUPPORT_DIR, OUTPUT_DIR, ARCHIVE_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def format_app_time(value: datetime) -> str:
    return value.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def now_app_time() -> str:
    return format_app_time(datetime.now(APP_TIMEZONE))


def timestamp_app_time(timestamp: float) -> str:
    return format_app_time(datetime.fromtimestamp(timestamp, APP_TIMEZONE))


def safe_filename(name: str) -> str:
    name = Path(name).name.strip() or "jenkins-log.txt"
    name = re.sub(r"[^A-Za-z0-9_.#() -]+", "_", name)
    if "." not in name:
        name += ".txt"
    return name


def output_name(input_name: str) -> str:
    stem = Path(input_name).stem.replace(" ", "_").replace("(", "").replace(")", "")
    stem = re.sub(r"[^A-Za-z0-9_.#-]+", "_", stem)
    return f"ollama_failure_reason_{stem}.txt"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def save_upload(part: object, folder: Path, fallback_name: str) -> Path:
    filename = safe_filename(part.get_filename() or fallback_name)
    output_path = folder / filename
    if output_path.exists():
        output_path = folder / f"{int(time.time())}-{filename}"
    output_path.write_bytes(part.get_payload(decode=True) or b"")
    return output_path


def decode_bytes(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "UTF-8 OK"
    except UnicodeDecodeError as error:
        text = data.decode("utf-8", errors="replace")
        return (
            text,
            f"UTF-8 decode error at byte {error.start}: invalid byte 0x{data[error.start]:02x}",
        )


def csv_rows_from_text(text: str) -> list[list[str]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    try:
        return list(csv.reader(io.StringIO(text, newline=""), dialect))
    except (csv.Error, ValueError):
        return list(csv.reader(io.StringIO(text, newline=""), csv.excel))


def looks_like_csv_rows(rows: list[list[str]]) -> bool:
    non_empty_rows = [row for row in rows[:8] if any(cell.strip() for cell in row)]
    if len(non_empty_rows) < 2:
        return False
    header_width = len(non_empty_rows[0])
    if header_width < 2:
        return False
    matching_rows = sum(1 for row in non_empty_rows[1:] if len(row) == header_width)
    return matching_rows >= 1


def reference_jobs() -> list[str]:
    if not REFERENCE_DIR.exists():
        return []
    jobs = []
    for folder in REFERENCE_DIR.iterdir():
        if folder.is_dir() and (folder / "correct_input.csv").exists():
            jobs.append(folder.name)
    return sorted(jobs, key=str.lower)


def valid_job_key(job_key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_. -]+", job_key)) and (REFERENCE_DIR / job_key / "correct_input.csv").exists()


def reference_csv_path(job_key: str) -> Path:
    if not valid_job_key(job_key):
        raise ValueError("Unknown Jenkins job selected.")
    return REFERENCE_DIR / job_key / "correct_input.csv"


def load_csv_rows(path: Path) -> tuple[list[list[str]], str]:
    text, encoding_status = decode_bytes(path.read_bytes())
    return csv_rows_from_text(text), encoding_status


def non_empty_data_rows(rows: list[list[str]]) -> list[tuple[int, list[str]]]:
    return [(index, row) for index, row in enumerate(rows[1:], start=2) if any(cell.strip() for cell in row)]


def infer_reference_rules(headers: list[str], reference_rows: list[list[str]]) -> dict[str, dict[str, object]]:
    rules: dict[str, dict[str, object]] = {}
    data_rows = [row for _, row in non_empty_data_rows(reference_rows)]
    for column_index, header in enumerate(headers):
        values = [row[column_index].strip() for row in data_rows if column_index < len(row) and row[column_index].strip()]
        unique_values = sorted(set(values))
        rule: dict[str, object] = {"required": bool(values)}
        if values and all(value.isdigit() for value in values):
            lengths = sorted({len(value) for value in values})
            rule["digits_only"] = True
            if len(lengths) == 1:
                rule["length"] = lengths[0]
        elif values and 1 < len(unique_values) <= 20:
            rule["allowed_values"] = unique_values
        rules[header] = rule
    return rules


def is_uppercase_value(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    return bool(letters) and all(character.isupper() for character in letters)


def has_special_character(value: str, *, allow_commas: bool = False) -> bool:
    allowed = r"A-Za-z0-9 "
    if allow_commas:
        allowed += r","
    return bool(re.search(rf"[^{allowed}]", value))


def has_at_least_two_words(value: str) -> bool:
    return len([word for word in value.strip().split() if word]) >= 2


def validate_fm_existing_partner_location_onboarding(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(header: str) -> str:
        index = header_index.get(header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    fmcode = value_for("fmcode")
    client_location_name = value_for("clientLocationName")
    partner_id = value_for("partner_id")
    contact_number = value_for("contactNumber")
    branch_admin_name = value_for("branch_admin_name")
    email = value_for("email")
    loczipcode = value_for("loczipcode")
    pickup_pincodes = value_for("pickupPincodes")
    is_migrated_location = value_for("isMigratedLocation")

    if fmcode and client_location_name and fmcode != client_location_name:
        findings.append(
            f"Row {row_number}: fmcode and clientLocationName must be exactly same, found {fmcode!r} and {client_location_name!r}."
        )
        add_fix(f"Make row {row_number} fmcode and clientLocationName exactly same.")

    for header, value in (("fmcode", fmcode), ("clientLocationName", client_location_name)):
        if value and not is_uppercase_value(value):
            findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")

    if not partner_id:
        blank_finding = f"Row {row_number}, column partner_id: value is blank."
        if blank_finding not in findings:
            findings.append(blank_finding)
            add_fix(f"Fill row {row_number}, column partner_id; partner_id is required.")

    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        findings.append(f"Row {row_number}, column email: expected a valid email address, found {email!r}.")
        add_fix(f"Enter a valid email address in row {row_number}, column email.")

    if loczipcode and loczipcode.isdigit() and len(loczipcode) != 6:
        length_finding = f"Row {row_number}, column loczipcode: expected 6 digits, found {len(loczipcode)}."
        if length_finding not in findings:
            findings.append(length_finding)
            add_fix(f"Change row {row_number}, column loczipcode to exactly 6 digits.")

    if pickup_pincodes:
        pincodes = [pincode.strip() for pincode in pickup_pincodes.split(",")]
        if any(not pincode for pincode in pincodes) or any(not re.fullmatch(r"\d{6}", pincode) for pincode in pincodes):
            findings.append(
                f"Row {row_number}, column pickupPincodes: expected comma-separated 6-digit pincodes, found {pickup_pincodes!r}."
            )
            add_fix(f"Use comma-separated 6-digit pincodes in row {row_number}, column pickupPincodes.")

    branch_admin_digits = re.sub(r"\D", "", branch_admin_name)
    if len(branch_admin_digits) > 16:
        findings.append(
            f"Row {row_number}, column branch_admin_name: contains more than 16 digits."
        )
        add_fix(f"Reduce row {row_number}, column branch_admin_name so it does not contain more than 16 digits.")

    if is_migrated_location != "0":
        findings.append(
            f"Row {row_number}, column isMigratedLocation: expected 0, found {is_migrated_location!r}."
        )
        add_fix(f"Change row {row_number}, column isMigratedLocation to 0.")

    comma_allowed_columns = set()
    special_allowed_columns = {"fmcodeaddress", "email", "pickupPincodes"}
    for header, index in header_index.items():
        if index >= len(row):
            continue
        value = row[index].strip()
        if not value or header in special_allowed_columns:
            continue
        if has_special_character(value, allow_commas=header in comma_allowed_columns):
            findings.append(f"Row {row_number}, column {header}: special characters are not allowed, found {value!r}.")
            add_fix(f"Remove special characters from row {row_number}, column {header}.")


def validate_fm_new_partner_location_onboarding(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(header: str) -> str:
        index = header_index.get(header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    fmcode = value_for("fmcode")
    client_location_name = value_for("clientLocationName")
    partner_name = value_for("Partner_name")
    branch_admin_name = value_for("branch_admin_name")
    email = value_for("email")
    loczipcode = value_for("loczipcode")
    pickup_pincodes = value_for("pickupPincodes")
    is_loadshare_partner = value_for("isLoadsharePartner")
    is_migrated_location = value_for("isMigratedLocation")

    if fmcode and client_location_name and fmcode != client_location_name:
        findings.append(
            f"Row {row_number}: fmcode and clientLocationName must be exactly same, found {fmcode!r} and {client_location_name!r}."
        )
        add_fix(f"Make row {row_number} fmcode and clientLocationName exactly same.")

    for header, value in (("fmcode", fmcode), ("clientLocationName", client_location_name)):
        if value and not is_uppercase_value(value):
            findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")

    if partner_name and not re.fullmatch(r"[A-Za-z ]+", partner_name):
        findings.append(f"Row {row_number}, column Partner_name: expected alphabets and spaces only, found {partner_name!r}.")
        add_fix(f"Change row {row_number}, column Partner_name to alphabets only with no special characters.")

    if len(partner_name) > 20:
        findings.append(f"Row {row_number}, column Partner_name: expected maximum 20 characters, found {len(partner_name)}.")
        add_fix(f"Shorten row {row_number}, column Partner_name to 20 characters or less.")

    if branch_admin_name and not re.fullmatch(r"[A-Za-z ]+", branch_admin_name):
        findings.append(f"Row {row_number}, column branch_admin_name: expected alphabets and spaces only, found {branch_admin_name!r}.")
        add_fix(f"Change row {row_number}, column branch_admin_name to alphabets and spaces only.")

    if len(branch_admin_name) > 20:
        findings.append(
            f"Row {row_number}, column branch_admin_name: expected maximum 20 characters, found {len(branch_admin_name)}."
        )
        add_fix(f"Shorten row {row_number}, column branch_admin_name to 20 characters or less.")

    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        findings.append(f"Row {row_number}, column email: expected a valid email address, found {email!r}.")
        add_fix(f"Enter a valid email address in row {row_number}, column email.")

    if loczipcode and loczipcode.isdigit() and len(loczipcode) != 6:
        length_finding = f"Row {row_number}, column loczipcode: expected 6 digits, found {len(loczipcode)}."
        if length_finding not in findings:
            findings.append(length_finding)
            add_fix(f"Change row {row_number}, column loczipcode to exactly 6 digits.")

    if pickup_pincodes:
        pincodes = [pincode.strip() for pincode in pickup_pincodes.split(",")]
        if any(not pincode for pincode in pincodes) or any(not re.fullmatch(r"\d{6}", pincode) for pincode in pincodes):
            findings.append(
                f"Row {row_number}, column pickupPincodes: expected comma-separated 6-digit pincodes, found {pickup_pincodes!r}."
            )
            add_fix(f"Use comma-separated 6-digit pincodes in row {row_number}, column pickupPincodes.")

    branch_admin_digits = re.sub(r"\D", "", branch_admin_name)
    if len(branch_admin_digits) > 16:
        findings.append(
            f"Row {row_number}, column branch_admin_name: contains more than 16 digits."
        )
        add_fix(f"Reduce row {row_number}, column branch_admin_name so it does not contain more than 16 digits.")

    if is_loadshare_partner != "0":
        findings.append(
            f"Row {row_number}, column isLoadsharePartner: expected 0, found {is_loadshare_partner!r}."
        )
        add_fix(f"Change row {row_number}, column isLoadsharePartner to 0.")

    if is_migrated_location != "0":
        findings.append(
            f"Row {row_number}, column isMigratedLocation: expected 0, found {is_migrated_location!r}."
        )
        add_fix(f"Change row {row_number}, column isMigratedLocation to 0.")

    comma_allowed_columns = set()
    special_allowed_columns = {"fmcodeaddress", "email", "Partner_name", "pickupPincodes"}
    for header, index in header_index.items():
        if index >= len(row):
            continue
        value = row[index].strip()
        if not value or header in special_allowed_columns:
            continue
        if has_special_character(value, allow_commas=header in comma_allowed_columns):
            findings.append(f"Row {row_number}, column {header}: special characters are not allowed, found {value!r}.")
            add_fix(f"Remove special characters from row {row_number}, column {header}.")


def validate_lm_existing_partner_location_onboarding(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(header: str) -> str:
        index = header_index.get(header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    dccode = value_for("dccode")
    client_location_name = value_for("clientLocationName")
    dcaddress = value_for("dcaddress")
    loczipcode = value_for("loczipcode")
    delivery_pincodes = value_for("deliveryPincodes")
    sc = value_for("sc")

    if not dccode:
        return

    if dccode != dccode.upper():
        findings.append(f"Row {row_number}, column dccode: value must be uppercase, found {dccode!r}.")
        add_fix(f"Change row {row_number}, column dccode to uppercase.")

    if " " in dccode:
        findings.append(f"Row {row_number}, column dccode: internal spaces are not allowed, found {dccode!r}.")
        add_fix(f"Remove spaces from row {row_number}, column dccode.")

    dccode_parts = [part.strip() for part in dccode.split("/")]
    if len(dccode_parts) < 4 or any(not part for part in dccode_parts[:4]):
        findings.append(
            f"Row {row_number}, column dccode: expected format like N1/LD4S/PN9/NYU, found {dccode!r}."
        )
        add_fix(f"Change row {row_number}, column dccode to the correct slash-separated format.")
        return

    expected_sc = dccode_parts[1].replace(" ", "").upper()
    expected_client_location_name = dccode_parts[3].replace(" ", "").upper()

    for header, value in (("sc", sc), ("clientLocationName", client_location_name)):
        if value and value != value.upper():
            findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")

    if sc and sc != expected_sc:
        findings.append(
            f"Row {row_number}, column sc: expected {expected_sc!r} from dccode, found {sc!r}."
        )
        add_fix(f"Change row {row_number}, column sc to {expected_sc!r}.")

    if client_location_name and client_location_name != expected_client_location_name:
        findings.append(
            f"Row {row_number}, column clientLocationName: expected {expected_client_location_name!r} from dccode, found {client_location_name!r}."
        )
        add_fix(f"Change row {row_number}, column clientLocationName to {expected_client_location_name!r}.")

    if loczipcode and loczipcode.isdigit() and len(loczipcode) != 6:
        length_finding = f"Row {row_number}, column loczipcode: expected 6 digits, found {len(loczipcode)}."
        if length_finding not in findings:
            findings.append(length_finding)
            add_fix(f"Change row {row_number}, column loczipcode to exactly 6 digits.")

    if delivery_pincodes:
        pincodes = [pincode.strip() for pincode in delivery_pincodes.split(",")]
        if any(not pincode for pincode in pincodes) or any(not re.fullmatch(r"\d{6}", pincode) for pincode in pincodes):
            findings.append(
                f"Row {row_number}, column deliveryPincodes: expected comma-separated 6-digit pincodes, found {delivery_pincodes!r}."
            )
            add_fix(f"Use comma-separated 6-digit pincodes in row {row_number}, column deliveryPincodes.")

    if dcaddress and len(dcaddress) < 10:
        findings.append(f"Row {row_number}, column dcaddress: address looks too short, found {dcaddress!r}.")
        add_fix(f"Enter a complete dcaddress in row {row_number}; it should be at least 10 characters.")


def validate_lm_new_partner_location_onboarding(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(header: str) -> str:
        index = header_index.get(header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    partner_name = value_for("Partner_name")
    email = value_for("email")
    dccode = value_for("dccode")
    client_location_name = value_for("clientLocationName")
    dcaddress = value_for("dcaddress")
    loczipcode = value_for("loczipcode")
    delivery_pincodes = value_for("deliveryPincodes")
    sc = value_for("sc")
    branch_admin_name = value_for("branch_admin_name")
    is_loadshare_partner = value_for("isLoadsharePartner")

    if dccode:
        if dccode != dccode.upper():
            findings.append(f"Row {row_number}, column dccode: value must be uppercase, found {dccode!r}.")
            add_fix(f"Change row {row_number}, column dccode to uppercase.")

        if " " in dccode:
            findings.append(f"Row {row_number}, column dccode: internal spaces are not allowed, found {dccode!r}.")
            add_fix(f"Remove spaces from row {row_number}, column dccode.")

        dccode_parts = [part.strip() for part in dccode.split("/")]
        if len(dccode_parts) < 4 or any(not part for part in dccode_parts[:4]):
            findings.append(
                f"Row {row_number}, column dccode: expected format like S1/HYDS/B02/LXW, found {dccode!r}."
            )
            add_fix(f"Change row {row_number}, column dccode to the correct slash-separated format.")
        else:
            expected_sc = dccode_parts[1].replace(" ", "").upper()
            expected_client_location_name = dccode_parts[3].replace(" ", "").upper()
            for header, value in (("sc", sc), ("clientLocationName", client_location_name)):
                if value and value != value.upper():
                    findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
                    add_fix(f"Change row {row_number}, column {header} to uppercase.")
            if sc and sc != expected_sc:
                findings.append(
                    f"Row {row_number}, column sc: expected {expected_sc!r} from dccode, found {sc!r}."
                )
                add_fix(f"Change row {row_number}, column sc to {expected_sc!r}.")
            if client_location_name and client_location_name != expected_client_location_name:
                findings.append(
                    f"Row {row_number}, column clientLocationName: expected {expected_client_location_name!r} from dccode, found {client_location_name!r}."
                )
                add_fix(f"Change row {row_number}, column clientLocationName to {expected_client_location_name!r}.")

    if partner_name and not has_at_least_two_words(partner_name):
        findings.append(f"Row {row_number}, column Partner_name: expected at least 2 words, found {partner_name!r}.")
        add_fix(f"Change row {row_number}, column Partner_name to at least 2 words.")

    if branch_admin_name and not re.fullmatch(r"[A-Za-z ]+", branch_admin_name):
        findings.append(f"Row {row_number}, column branch_admin_name: expected alphabets and spaces only, found {branch_admin_name!r}.")
        add_fix(f"Change row {row_number}, column branch_admin_name to alphabets and spaces only.")

    if branch_admin_name and not has_at_least_two_words(branch_admin_name):
        findings.append(f"Row {row_number}, column branch_admin_name: expected at least 2 words, found {branch_admin_name!r}.")
        add_fix(f"Change row {row_number}, column branch_admin_name to at least 2 words.")

    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        findings.append(f"Row {row_number}, column email: expected a valid email address, found {email!r}.")
        add_fix(f"Enter a valid email address in row {row_number}, column email.")

    if loczipcode and loczipcode.isdigit() and len(loczipcode) != 6:
        length_finding = f"Row {row_number}, column loczipcode: expected 6 digits, found {len(loczipcode)}."
        if length_finding not in findings:
            findings.append(length_finding)
            add_fix(f"Change row {row_number}, column loczipcode to exactly 6 digits.")

    if delivery_pincodes:
        pincodes = [pincode.strip() for pincode in delivery_pincodes.split(",")]
        if any(not pincode for pincode in pincodes) or any(not re.fullmatch(r"\d{6}", pincode) for pincode in pincodes):
            findings.append(
                f"Row {row_number}, column deliveryPincodes: expected comma-separated 6-digit pincodes, found {delivery_pincodes!r}."
            )
            add_fix(f"Use comma-separated 6-digit pincodes in row {row_number}, column deliveryPincodes.")

    if dcaddress and len(dcaddress) < 10:
        findings.append(f"Row {row_number}, column dcaddress: address looks too short, found {dcaddress!r}.")
        add_fix(f"Enter a complete dcaddress in row {row_number}; it should be at least 10 characters.")

    if is_loadshare_partner != "0":
        findings.append(
            f"Row {row_number}, column isLoadsharePartner: expected 0, found {is_loadshare_partner!r}."
        )
        add_fix(f"Change row {row_number}, column isLoadsharePartner to 0.")


def validate_pending_manifest_corrections(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    row_values: dict[str, str] = {}
    for header in ("current_location", "destination_locations", "next_location"):
        index = header_index.get(header, -1)
        value = row[index].strip() if 0 <= index < len(row) else ""
        row_values[header] = value
        if not value:
            continue
        if " " in value:
            findings.append(f"Row {row_number}, column {header}: internal spaces are not allowed, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} from {value!r} to {value.replace(' ', '')!r}.")
        if value != value.upper():
            findings.append(f"Row {row_number}, column {header}: location code must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")
        has_only_space_problem = " " in value and bool(re.fullmatch(r"[A-Za-z0-9]+", value.replace(" ", "")))
        if not has_only_space_problem and not re.fullmatch(r"[A-Za-z0-9]+", value):
            findings.append(f"Row {row_number}, column {header}: only letters and numbers are allowed, found {value!r}.")
            cleaned_value = re.sub(r"[^A-Za-z0-9]", "", value)
            if cleaned_value:
                add_fix(f"Change row {row_number}, column {header} from {value!r} to {cleaned_value!r}.")
            else:
                add_fix(f"Remove special characters from row {row_number}, column {header}.")

    values = [row_values.get(header, "") for header in ("current_location", "destination_locations", "next_location")]
    if all(values) and len(set(values)) == 1:
        findings.append(
            f"Row {row_number}: current_location, destination_locations, and next_location are all {values[0]!r}."
        )
        add_fix(f"Change row {row_number}; current_location, destination_locations, and next_location should not all be same.")


def validate_pending_manifest_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    seen: dict[tuple[str, str, str], int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        values = []
        for header in ("current_location", "destination_locations", "next_location"):
            index = header_index.get(header, -1)
            values.append(row[index].strip() if 0 <= index < len(row) else "")
        key = tuple(values)
        if not all(key):
            continue
        if key in seen:
            first_row = seen[key]
            current_location, destination_locations, next_location = key
            findings.append(
                f"Row {row_number} repeats row {first_row}: {current_location}, {destination_locations}, {next_location}."
            )
            add_fix(
                f"Remove duplicate row {row_number}; the same manifest correction is already present in row {first_row}."
            )
        else:
            seen[key] = row_number


def validate_user_role_access_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    header_aliases: dict[str, str],
    findings: list[str],
    add_fix: object,
) -> None:
    source_header = "User_contact_number" if "User_contact_number" in header_index else header_aliases.get("User_contact_number", "")
    if not source_header or source_header not in header_index:
        return

    contact_index = header_index[source_header]
    seen: dict[str, int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        contact_number = row[contact_index].strip() if contact_index < len(row) else ""
        if not contact_number:
            continue
        if contact_number in seen:
            first_row = seen[contact_number]
            display_column = source_header if source_header == "User_contact_number" else f"{source_header} -> User_contact_number"
            findings.append(
                f"Row {row_number}, column {display_column}: duplicate User_contact_number already present in row {first_row}."
            )
            add_fix(
                f"Remove duplicate User_contact_number in row {row_number}; the same number is already present in row {first_row}."
            )
        else:
            seen[contact_number] = row_number


def validate_deactivate_nlc_bulk(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    id_index = header_index.get("id", -1)
    value = row[id_index].strip() if 0 <= id_index < len(row) else ""
    if not value:
        return

    if " " in value:
        findings.append(f"Row {row_number}, column id: spaces are not allowed, found {value!r}.")
        add_fix(f"Remove spaces from row {row_number}, column id.")


def validate_deactivate_nlc_bulk_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    id_index = header_index.get("id", -1)
    if id_index < 0:
        return

    seen: dict[str, int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        value = row[id_index].strip() if id_index < len(row) else ""
        if not value:
            continue
        if value in seen:
            first_row = seen[value]
            findings.append(f"Row {row_number}, column id: duplicate id already present in row {first_row}.")
            add_fix(f"Remove duplicate id in row {row_number}; the same id is already present in row {first_row}.")
        else:
            seen[value] = row_number


def validate_deactivate_nlc_bulk_blank_rows(
    *,
    uploaded_rows: list[list[str]],
    findings: list[str],
    add_fix: object,
) -> None:
    for row_number, row in enumerate(uploaded_rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            findings.append(f"Row {row_number}, column id: value is blank.")
            add_fix(f"Fill row {row_number}, column id; id is required.")


def validate_fmsc_migration(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    header_aliases: dict[str, str],
    findings: list[str],
    add_fix: object,
) -> None:
    def source_for(header: str) -> str:
        return header if header in header_index else header_aliases.get(header, "")

    def display_for(header: str) -> str:
        source_header = source_for(header)
        return header if source_header == header else f"{source_header} -> {header}"

    def value_for(header: str) -> str:
        source_header = source_for(header)
        index = header_index.get(source_header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    for header in ("FMH", "FMCD", "FMSC", "is_manifest_correction_required"):
        value = value_for(header)
        if not value:
            continue
        if " " in value:
            findings.append(f"Row {row_number}, column {header}: internal spaces are not allowed, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} from {value!r} to {value.replace(' ', '')!r}.")

    for header in ("FMH", "FMSC"):
        value = value_for(header)
        if not value:
            required_finding = f"Row {row_number}, column {display_for(header)}: value is blank."
            if required_finding not in findings:
                findings.append(required_finding)
                add_fix(f"Fill row {row_number}, column {source_for(header) or header}; {header} is required.")

    correction_value = value_for("is_manifest_correction_required")
    if correction_value != "1":
        findings.append(
            f"Row {row_number}, column is_manifest_correction_required: expected 1, found {correction_value!r}."
        )
        add_fix(f"Change row {row_number}, column is_manifest_correction_required to 1.")

    for header in ("FMH", "FMCD", "FMSC"):
        value = value_for(header)
        if not value:
            continue
        if value != value.upper():
            findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")
        has_only_space_problem = " " in value and bool(re.fullmatch(r"[A-Za-z0-9]+", value.replace(" ", "")))
        if not has_only_space_problem and not re.fullmatch(r"[A-Za-z0-9]+", value):
            findings.append(f"Row {row_number}, column {header}: only letters and numbers are allowed, found {value!r}.")
            cleaned_value = re.sub(r"[^A-Za-z0-9]", "", value)
            if cleaned_value:
                add_fix(f"Change row {row_number}, column {header} from {value!r} to {cleaned_value!r}.")
            else:
                add_fix(f"Remove special characters from row {row_number}, column {header}.")


def validate_fmsc_migration_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    header_aliases: dict[str, str],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(row: list[str], header: str) -> str:
        source_header = header if header in header_index else header_aliases.get(header, "")
        index = header_index.get(source_header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    seen: dict[tuple[str, str, str, str], int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        values = [value_for(row, header) for header in ("FMH", "FMCD", "FMSC", "is_manifest_correction_required")]
        key = tuple(values)
        if not key[0] or not key[2]:
            continue
        if key in seen:
            first_row = seen[key]
            findings.append(f"Row {row_number} repeats row {first_row}: {', '.join(key)}.")
            add_fix(f"Remove duplicate row {row_number}; the same FMSC migration is already present in row {first_row}.")
        else:
            seen[key] = row_number


def validate_titan_user_migration_titan(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    for header in ("contact_number", "location_id"):
        column_index = header_index.get(header, -1)
        value = row[column_index].strip() if 0 <= column_index < len(row) else ""
        if not value:
            continue
        if " " in value:
            findings.append(f"Row {row_number}, column {header}: internal spaces are not allowed, found {value!r}.")
            add_fix(f"Remove spaces from row {row_number}, column {header}.")


def validate_titan_user_migration_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    contact_index = header_index.get("contact_number", -1)
    location_index = header_index.get("location_id", -1)
    if contact_index < 0 or location_index < 0:
        return

    seen_contacts: dict[str, int] = {}
    seen_rows: dict[tuple[str, str], int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        contact_number = row[contact_index].strip() if contact_index < len(row) else ""
        location_id = row[location_index].strip() if location_index < len(row) else ""

        if contact_number:
            if contact_number in seen_contacts:
                first_row = seen_contacts[contact_number]
                findings.append(
                    f"Row {row_number}, column contact_number: duplicate contact_number already present in row {first_row}."
                )
                add_fix(
                    f"Remove duplicate contact_number in row {row_number}; the same contact_number is already present in row {first_row}."
                )
            else:
                seen_contacts[contact_number] = row_number

        if not contact_number or not location_id:
            continue
        key = (contact_number, location_id)
        if key in seen_rows:
            first_row = seen_rows[key]
            findings.append(f"Row {row_number} repeats row {first_row}: {contact_number}, {location_id}.")
            add_fix(f"Remove duplicate row {row_number}; the same user migration is already present in row {first_row}.")
        else:
            seen_rows[key] = row_number


def validate_titan_entity_corrections(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    type_index = header_index.get("entity_type", -1)
    code_index = header_index.get("entity_code", -1)
    entity_type = row[type_index].strip() if 0 <= type_index < len(row) else ""
    entity_code = row[code_index].strip() if 0 <= code_index < len(row) else ""

    for header, value in (("entity_type", entity_type), ("entity_code", entity_code)):
        if not value:
            continue
        if " " in value:
            findings.append(f"Row {row_number}, column {header}: spaces are not allowed, found {value!r}.")
            add_fix(f"Remove spaces from row {row_number}, column {header}.")

    if entity_type and entity_type != entity_type.upper():
        findings.append(f"Row {row_number}, column entity_type: value must be uppercase, found {entity_type!r}.")
        add_fix(f"Change row {row_number}, column entity_type to uppercase.")

    normalized_type = entity_type.upper()
    if not entity_code:
        return

    if normalized_type == "AADHAR":
        if re.fullmatch(r"\d+(?:\.\d+)?[Ee][+-]?\d+", entity_code):
            findings.append(
                f"Row {row_number}, column entity_code: Aadhaar must be 12 plain digits, not Excel/scientific format {entity_code!r}."
            )
            add_fix(f"Change row {row_number}, column entity_code to the full 12-digit Aadhaar number.")
        elif not entity_code.isdigit():
            findings.append(
                f"Row {row_number}, column entity_code: Aadhaar must contain digits only, found {mask_value(entity_code)}."
            )
            add_fix(f"Change row {row_number}, column entity_code to digits only.")
        elif len(entity_code) != 12:
            findings.append(
                f"Row {row_number}, column entity_code: Aadhaar must be exactly 12 digits, found {len(entity_code)}."
            )
            add_fix(f"Change row {row_number}, column entity_code to exactly 12 digits.")
    elif normalized_type == "PANCARD":
        if entity_code != entity_code.upper():
            findings.append(f"Row {row_number}, column entity_code: PAN must be uppercase, found {entity_code!r}.")
            add_fix(f"Change row {row_number}, column entity_code to uppercase.")
        if not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", entity_code):
            findings.append(
                f"Row {row_number}, column entity_code: PAN must be 5 uppercase letters, 4 digits, and 1 uppercase letter, found {entity_code!r}."
            )
            add_fix(f"Change row {row_number}, column entity_code to valid PAN format like CBSPK1234M.")
    elif normalized_type in {"ACTIVATE", "DEACTIVATE"}:
        if not entity_code.isdigit():
            findings.append(
                f"Row {row_number}, column entity_code: {normalized_type} code must be numeric only, found {entity_code!r}."
            )
            add_fix(f"Change row {row_number}, column entity_code to numbers only.")


def validate_titan_entity_corrections_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    type_index = header_index.get("entity_type", -1)
    code_index = header_index.get("entity_code", -1)
    if type_index < 0 or code_index < 0:
        return

    seen_rows: dict[tuple[str, str], int] = {}
    numeric_actions_by_code: dict[str, tuple[str, int]] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        entity_type = row[type_index].strip().upper() if type_index < len(row) else ""
        entity_code = row[code_index].strip() if code_index < len(row) else ""
        if not entity_type or not entity_code:
            continue

        key = (entity_type, entity_code)
        if key in seen_rows:
            first_row = seen_rows[key]
            findings.append(f"Row {row_number} repeats row {first_row}: {entity_type}, {entity_code}.")
            add_fix(f"Remove duplicate row {row_number}; the same entity correction is already present in row {first_row}.")
        else:
            seen_rows[key] = row_number

        if entity_type not in {"ACTIVATE", "DEACTIVATE"} or not entity_code.isdigit():
            continue
        opposite = "DEACTIVATE" if entity_type == "ACTIVATE" else "ACTIVATE"
        existing = numeric_actions_by_code.get(entity_code)
        if existing and existing[0] == opposite:
            findings.append(
                f"Row {row_number}, column entity_code: conflicting action for {entity_code}; row {existing[1]} uses {opposite}."
            )
            add_fix(
                f"Keep only one action for entity_code {entity_code}; do not upload both ACTIVATE and DEACTIVATE."
            )
        else:
            numeric_actions_by_code[entity_code] = (entity_type, row_number)


def validate_linehaul_route_mappings_v2(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(header: str) -> str:
        index = header_index.get(header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    fmsc = value_for("FMSC")
    lmsc = value_for("LMSC")
    correction_required = value_for("is_manifest_correction_required")

    for header in ("FMSC", "FMCD", "LMCD", "LMSC"):
        value = value_for(header)
        if not value:
            continue
        if " " in value:
            findings.append(f"Row {row_number}, column {header}: internal spaces are not allowed, found {value!r}.")
            add_fix(f"Remove spaces from row {row_number}, column {header}.")
        if value != value.upper():
            findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")
        has_only_space_problem = " " in value and bool(re.fullmatch(r"[A-Za-z0-9]+", value.replace(" ", "")))
        if not has_only_space_problem and not re.fullmatch(r"[A-Za-z0-9]+", value):
            findings.append(f"Row {row_number}, column {header}: only letters and numbers are allowed, found {value!r}.")
            cleaned_value = re.sub(r"[^A-Za-z0-9]", "", value)
            if cleaned_value:
                add_fix(f"Change row {row_number}, column {header} from {value!r} to {cleaned_value!r}.")
            else:
                add_fix(f"Remove special characters from row {row_number}, column {header}.")

    if fmsc and lmsc and fmsc == lmsc:
        findings.append(f"Row {row_number}: FMSC and LMSC should not be same, found {fmsc!r}.")
        add_fix(f"Change row {row_number} so FMSC and LMSC are different route codes.")

    if correction_required != "1":
        findings.append(
            f"Row {row_number}, column is_manifest_correction_required: expected 1, found {correction_required!r}."
        )
        add_fix(f"Change row {row_number}, column is_manifest_correction_required to 1.")


def validate_linehaul_route_mappings_v2_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    required_headers = ("FMSC", "FMCD", "LMCD", "LMSC", "is_manifest_correction_required")
    if any(header not in header_index for header in required_headers):
        return

    seen: dict[tuple[str, str, str, str, str], int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        key = tuple(
            row[header_index[header]].strip() if header_index[header] < len(row) else ""
            for header in required_headers
        )
        if not key[0] or not key[3]:
            continue
        if key in seen:
            first_row = seen[key]
            findings.append(f"Row {row_number} repeats row {first_row}: {', '.join(key)}.")
            add_fix(f"Remove duplicate row {row_number}; the same linehaul route mapping is already present in row {first_row}.")
        else:
            seen[key] = row_number


def validate_lmsc_migration_cd(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(header: str) -> str:
        index = header_index.get(header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    def sort_code_parts(header: str, value: str) -> list[str]:
        if not value:
            return []
        parts = value.split("/")
        if len(parts) != 4 or any(not part for part in parts):
            findings.append(f"Row {row_number}, column {header}: expected format like S2/BLFS/B11/AR1, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to 4 slash-separated parts like S2/BLFS/B11/AR1.")
            return []
        for part in parts:
            if part != part.upper():
                findings.append(f"Row {row_number}, column {header}: sort code must be uppercase, found {value!r}.")
                add_fix(f"Change row {row_number}, column {header} to uppercase.")
                break
            if not re.fullmatch(r"[A-Z0-9-]+", part):
                findings.append(
                    f"Row {row_number}, column {header}: sort code parts must contain only letters, numbers, and hyphen, found {value!r}."
                )
                add_fix(f"Remove invalid characters from row {row_number}, column {header}.")
                break
        return parts

    lmdc = value_for("LMDC")
    current_centre = value_for("Current Sort Centre")
    new_centre = value_for("New Sort Centre")
    current_code = value_for("Current Sort Code")
    new_code = value_for("New Sort Code")
    normalized_lmdc = lmdc.replace(" ", "").upper()
    normalized_current_centre = current_centre.replace(" ", "").upper()
    normalized_new_centre = new_centre.replace(" ", "").upper()

    for header in ("LMDC", "Current Sort Centre", "New Sort Centre", "Current Sort Code", "New Sort Code"):
        value = value_for(header)
        if not value:
            continue
        if " " in value:
            findings.append(f"Row {row_number}, column {header}: internal spaces are not allowed, found {value!r}.")
            add_fix(f"Remove spaces from row {row_number}, column {header}.")
        if value != value.upper():
            findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")

    for header, value in (("LMDC", lmdc), ("Current Sort Centre", current_centre), ("New Sort Centre", new_centre)):
        has_only_space_problem = " " in value and bool(re.fullmatch(r"[A-Za-z0-9]+", value.replace(" ", "")))
        if value and not has_only_space_problem and not re.fullmatch(r"[A-Za-z0-9]+", value):
            findings.append(f"Row {row_number}, column {header}: only letters and numbers are allowed, found {value!r}.")
            cleaned_value = re.sub(r"[^A-Za-z0-9]", "", value).upper()
            if cleaned_value:
                add_fix(f"Change row {row_number}, column {header} from {value!r} to {cleaned_value!r}.")
            else:
                add_fix(f"Remove invalid characters from row {row_number}, column {header}.")

    current_parts = sort_code_parts("Current Sort Code", current_code)
    new_parts = sort_code_parts("New Sort Code", new_code)

    if current_parts and normalized_lmdc and current_parts[3] != normalized_lmdc:
        findings.append(
            f"Row {row_number}: LMDC {lmdc!r} must match last part of Current Sort Code {current_parts[3]!r}."
        )
        add_fix(f"Make row {row_number} LMDC match the last part of Current Sort Code.")

    if new_parts and normalized_lmdc and new_parts[3] != normalized_lmdc:
        findings.append(f"Row {row_number}: LMDC {lmdc!r} must match last part of New Sort Code {new_parts[3]!r}.")
        add_fix(f"Make row {row_number} LMDC match the last part of New Sort Code.")

    if current_parts and normalized_current_centre and current_parts[1] != normalized_current_centre:
        findings.append(
            f"Row {row_number}: Current Sort Centre {current_centre!r} must match Current Sort Code centre {current_parts[1]!r}."
        )
        add_fix(f"Make row {row_number} Current Sort Centre match the second part of Current Sort Code.")

    if new_parts and normalized_new_centre and new_parts[1] != normalized_new_centre:
        findings.append(f"Row {row_number}: New Sort Centre {new_centre!r} must match New Sort Code centre {new_parts[1]!r}.")
        add_fix(f"Make row {row_number} New Sort Centre match the second part of New Sort Code.")

    if current_centre and new_centre and current_centre == new_centre:
        findings.append(f"Row {row_number}: Current Sort Centre and New Sort Centre should not be same, found {current_centre!r}.")
        add_fix(f"Change row {row_number} so Current Sort Centre and New Sort Centre are different.")

    if current_code and new_code and current_code == new_code:
        findings.append(f"Row {row_number}: Current Sort Code and New Sort Code should not be same, found {current_code!r}.")
        add_fix(f"Change row {row_number} so Current Sort Code and New Sort Code are different.")


def validate_lmsc_migration_cd_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    required_headers = ("LMDC", "Current Sort Centre", "New Sort Centre", "Current Sort Code", "New Sort Code")
    if any(header not in header_index for header in required_headers):
        return

    seen_rows: dict[tuple[str, str, str, str, str], int] = {}
    seen_lmdc: dict[str, int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        key = tuple(
            row[header_index[header]].strip() if header_index[header] < len(row) else ""
            for header in required_headers
        )
        lmdc = key[0]
        if lmdc:
            if lmdc in seen_lmdc:
                first_row = seen_lmdc[lmdc]
                findings.append(f"Row {row_number}, column LMDC: duplicate LMDC already present in row {first_row}.")
                add_fix(f"Remove duplicate LMDC in row {row_number}; the same LMDC is already present in row {first_row}.")
            else:
                seen_lmdc[lmdc] = row_number

        if not all(key):
            continue
        if key in seen_rows:
            first_row = seen_rows[key]
            findings.append(f"Row {row_number} repeats row {first_row}: {', '.join(key)}.")
            add_fix(f"Remove duplicate row {row_number}; the same LMSC migration is already present in row {first_row}.")
        else:
            seen_rows[key] = row_number


def validate_sort_code_update(
    *,
    row_number: int,
    row: list[str],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    def value_for(header: str) -> str:
        index = header_index.get(header, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    def sort_code_parts(header: str, value: str) -> list[str]:
        if not value:
            return []
        parts = value.split("/")
        if len(parts) != 4 or any(not part for part in parts):
            findings.append(f"Row {row_number}, column {header}: expected format like W2/NNS/11/M6M, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to 4 slash-separated parts like W2/NNS/11/M6M.")
            return []
        for part in parts:
            if part != part.upper():
                findings.append(f"Row {row_number}, column {header}: sort code must be uppercase, found {value!r}.")
                add_fix(f"Change row {row_number}, column {header} to uppercase.")
                break
            if not re.fullmatch(r"[A-Z0-9-]+", part):
                findings.append(
                    f"Row {row_number}, column {header}: sort code parts must contain only letters, numbers, and hyphen, found {value!r}."
                )
                add_fix(f"Remove invalid characters from row {row_number}, column {header}.")
                break
        return parts

    lmdc = value_for("LMDC")
    current_code = value_for("Current Sort Code")
    new_code = value_for("New Sort Code")
    normalized_lmdc = lmdc.replace(" ", "").upper()

    for header in ("LMDC", "Current Sort Code", "New Sort Code"):
        value = value_for(header)
        if not value:
            continue
        if " " in value:
            findings.append(f"Row {row_number}, column {header}: internal spaces are not allowed, found {value!r}.")
            add_fix(f"Remove spaces from row {row_number}, column {header}.")
        if value != value.upper():
            findings.append(f"Row {row_number}, column {header}: value must be uppercase, found {value!r}.")
            add_fix(f"Change row {row_number}, column {header} to uppercase.")

    if lmdc:
        has_only_space_problem = " " in lmdc and bool(re.fullmatch(r"[A-Za-z0-9]+", lmdc.replace(" ", "")))
        if not has_only_space_problem and not re.fullmatch(r"[A-Za-z0-9]+", lmdc):
            findings.append(f"Row {row_number}, column LMDC: only letters and numbers are allowed, found {lmdc!r}.")
            cleaned_value = re.sub(r"[^A-Za-z0-9]", "", lmdc).upper()
            if cleaned_value:
                add_fix(f"Change row {row_number}, column LMDC from {lmdc!r} to {cleaned_value!r}.")
            else:
                add_fix(f"Remove invalid characters from row {row_number}, column LMDC.")

    current_parts = sort_code_parts("Current Sort Code", current_code)
    new_parts = sort_code_parts("New Sort Code", new_code)

    if current_parts and normalized_lmdc and current_parts[3] != normalized_lmdc:
        findings.append(f"Row {row_number}: LMDC {lmdc!r} must match last part of Current Sort Code {current_parts[3]!r}.")
        add_fix(f"Make row {row_number} LMDC match the last part of Current Sort Code.")

    if new_parts and normalized_lmdc and new_parts[3] != normalized_lmdc:
        findings.append(f"Row {row_number}: LMDC {lmdc!r} must match last part of New Sort Code {new_parts[3]!r}.")
        add_fix(f"Make row {row_number} LMDC match the last part of New Sort Code.")

    if current_code and new_code and current_code == new_code:
        findings.append(f"Row {row_number}: Current Sort Code and New Sort Code should not be same, found {current_code!r}.")
        add_fix(f"Change row {row_number} so Current Sort Code and New Sort Code are different.")

    if current_parts and new_parts and current_parts[2] == new_parts[2]:
        findings.append(
            f"Row {row_number}: current sort code number and new sort code number should not be same, found {current_parts[2]!r}."
        )
        add_fix(f"Change row {row_number} New Sort Code number; current and new sort code number should not be same.")


def validate_sort_code_update_duplicates(
    *,
    uploaded_rows: list[list[str]],
    header_index: dict[str, int],
    findings: list[str],
    add_fix: object,
) -> None:
    required_headers = ("LMDC", "Current Sort Code", "New Sort Code")
    if any(header not in header_index for header in required_headers):
        return

    seen_rows: dict[tuple[str, str, str], int] = {}
    seen_lmdc: dict[str, int] = {}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        key = tuple(
            row[header_index[header]].strip() if header_index[header] < len(row) else ""
            for header in required_headers
        )
        lmdc = key[0]
        if lmdc:
            if lmdc in seen_lmdc:
                first_row = seen_lmdc[lmdc]
                findings.append(f"Row {row_number}, column LMDC: duplicate LMDC already present in row {first_row}.")
                add_fix(f"Remove duplicate LMDC in row {row_number}; the same LMDC is already present in row {first_row}.")
            else:
                seen_lmdc[lmdc] = row_number

        if not all(key):
            continue
        if key in seen_rows:
            first_row = seen_rows[key]
            findings.append(f"Row {row_number} repeats row {first_row}: {', '.join(key)}.")
            add_fix(f"Remove duplicate row {row_number}; the same sort code update is already present in row {first_row}.")
        else:
            seen_rows[key] = row_number


def validate_csv_against_reference(job_key: str, uploaded_csv: Path) -> ValidationReport:
    reference_path = reference_csv_path(job_key)
    reference_rows, reference_encoding = load_csv_rows(reference_path)
    uploaded_rows, uploaded_encoding = load_csv_rows(uploaded_csv)

    reference_headers = [cell for cell in reference_rows[0]] if reference_rows else []
    uploaded_headers = [cell for cell in uploaded_rows[0]] if uploaded_rows else []
    expected_headers = [header.strip() for header in reference_headers]
    actual_headers = [header.strip() for header in uploaded_headers]
    rules = infer_reference_rules(expected_headers, reference_rows)
    if job_key in DYNAMIC_VALUE_REFERENCE_JOBS:
        for rule in rules.values():
            rule.pop("allowed_values", None)
    if job_key == "fm_existing_partner_location_onboarding":
        for header in ("partner_id", "city_id"):
            rules.get(header, {}).pop("length", None)
        for header in ("contactNumber", "branch_admin_name", "email"):
            rules.get(header, {})["required"] = False
    if job_key == "fm_new_partner_location_onboarding":
        for rule_name in ("digits_only", "length"):
            rules.get("pickupPincodes", {}).pop(rule_name, None)
    if job_key == "lm_existing_partner_location_onboarding":
        for rule_name in ("digits_only", "length"):
            rules.get("deliveryPincodes", {}).pop(rule_name, None)
        rules.get("city_id", {}).pop("length", None)
    if job_key == "lm_new_partner_location_onboarding":
        for rule_name in ("digits_only", "length"):
            rules.get("deliveryPincodes", {}).pop(rule_name, None)
        rules.get("city_id", {}).pop("length", None)
    if job_key == "deactivate_nlc_bulk":
        rules.get("id", {}).pop("length", None)
    if job_key == "fmsc_migration":
        rules.get("FMCD", {})["required"] = False
    if job_key == "titan_user_migration_titan":
        rules.get("location_id", {}).pop("length", None)
    if job_key == "titan_entity_corrections":
        rules.get("entity_code", {}).pop("allowed_values", None)
        rules.get("entity_type", {})["allowed_values"] = sorted(TITAN_ENTITY_TYPES)
    if job_key == "Linehaul_route_mappings_v2":
        for header in ("FMCD", "LMCD"):
            rules.get(header, {})["required"] = False
    if job_key == "lmsc_migration_cd":
        for header in ("Current Sort Code", "New Sort Code"):
            rules.get(header, {}).pop("allowed_values", None)
    if job_key == "sort_code_update":
        for header in ("Current Sort Code", "New Sort Code"):
            rules.get(header, {}).pop("allowed_values", None)

    findings: list[str] = []
    fixes: list[str] = []
    header_aliases: dict[str, str] = {}

    def add_fix(fix: str) -> None:
        if fix not in fixes:
            fixes.append(fix)

    if uploaded_encoding != "UTF-8 OK":
        findings.append(f"Input CSV encoding issue: {uploaded_encoding}.")
        add_fix("Re-save the uploaded input file as UTF-8 CSV.")

    for header in uploaded_headers:
        if header != header.strip():
            findings.append(f"Header has extra spaces: {header!r} should be {header.strip()!r}.")
            add_fix(f"Remove extra spaces from header {header!r}; it should be {header.strip()!r}.")

    if actual_headers != expected_headers:
        findings.append(f"Header mismatch. Expected exactly: {', '.join(expected_headers)}.")
        missing = [header for header in expected_headers if header not in actual_headers]
        extra = [header for header in actual_headers if header and header not in expected_headers]
        if missing:
            findings.append(f"Missing column(s): {', '.join(missing)}.")
        if extra:
            findings.append(f"Unexpected or misspelled column(s): {', '.join(extra)}.")

        unmatched_extra = extra[:]
        for missing_header in missing:
            match = difflib.get_close_matches(missing_header, unmatched_extra, n=1, cutoff=0.45)
            if match:
                wrong_header = match[0]
                header_aliases[missing_header] = wrong_header
                add_fix(f"Rename column {wrong_header!r} to {missing_header!r}.")
                unmatched_extra.remove(wrong_header)
            else:
                add_fix(f"Add missing column {missing_header!r} with the exact same spelling.")
        for wrong_header in unmatched_extra:
            add_fix(f"Remove or correct unexpected column {wrong_header!r}.")

        if sorted(actual_headers) == sorted(expected_headers):
            findings.append("Column names exist, but the order is different from the correct format.")
            add_fix(f"Reorder columns exactly as: {', '.join(expected_headers)}.")

    header_index = {header: index for index, header in enumerate(actual_headers)}
    for row_number, row in non_empty_data_rows(uploaded_rows):
        if len(row) != len(actual_headers):
            findings.append(
                f"Row {row_number}: column count mismatch. Expected {len(actual_headers)} values, found {len(row)}."
            )
            add_fix(f"Fix row {row_number} so it has exactly {len(actual_headers)} comma-separated values.")
        for header in expected_headers:
            source_header = header if header in header_index else header_aliases.get(header, "")
            if not source_header or source_header not in header_index:
                continue
            column_index = header_index[source_header]
            raw_value = row[column_index] if column_index < len(row) else ""
            value = raw_value.strip()
            display_column = header if source_header == header else f"{source_header} -> {header}"
            if raw_value != value:
                findings.append(f"Row {row_number}, column {display_column}: value has extra leading/trailing spaces.")
                add_fix(f"Change row {row_number}, column {source_header} from {raw_value!r} to {value!r}.")
            rule = rules.get(header, {})
            if rule.get("required") and not value:
                findings.append(f"Row {row_number}, column {display_column}: value is blank.")
                add_fix(f"Fill row {row_number}, column {source_header}; it cannot be blank.")
                continue
            if rule.get("digits_only") and value and not value.isdigit():
                findings.append(f"Row {row_number}, column {display_column}: expected digits only, found {mask_value(value)}.")
                add_fix(f"Change row {row_number}, column {source_header} to digits only.")
            expected_length = rule.get("length")
            if isinstance(expected_length, int) and value and value.isdigit() and len(value) != expected_length:
                findings.append(
                    f"Row {row_number}, column {display_column}: expected {expected_length} digits, found {len(value)}."
                )
                add_fix(f"Change row {row_number}, column {source_header} to exactly {expected_length} digits.")
            allowed_values = rule.get("allowed_values")
            if isinstance(allowed_values, list) and value and value not in allowed_values:
                findings.append(
                    f"Row {row_number}, column {display_column}: expected one of {', '.join(allowed_values)}, found {value!r}."
                )
                unquoted_value = value.strip('"').strip("'")
                if unquoted_value in allowed_values:
                    add_fix(f"Remove quotes from row {row_number}, column {source_header}; use {unquoted_value}.")
                else:
                    case_match = next((allowed for allowed in allowed_values if allowed.lower() == value.lower()), "")
                    if case_match:
                        add_fix(f"Change row {row_number}, column {source_header} from {value!r} to {case_match!r}.")
                    else:
                        add_fix(f"Change row {row_number}, column {source_header} to one of: {', '.join(allowed_values)}.")

        if job_key == "fm_existing_partner_location_onboarding":
            validate_fm_existing_partner_location_onboarding(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "fm_new_partner_location_onboarding":
            validate_fm_new_partner_location_onboarding(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "lm_existing_partner_location_onboarding":
            validate_lm_existing_partner_location_onboarding(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "lm_new_partner_location_onboarding":
            validate_lm_new_partner_location_onboarding(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "pending_manifest_corrections":
            validate_pending_manifest_corrections(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "deactivate_nlc_bulk":
            validate_deactivate_nlc_bulk(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "fmsc_migration":
            validate_fmsc_migration(
                row_number=row_number,
                row=row,
                header_index=header_index,
                header_aliases=header_aliases,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "titan_user_migration_titan":
            validate_titan_user_migration_titan(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "titan_entity_corrections":
            validate_titan_entity_corrections(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "Linehaul_route_mappings_v2":
            validate_linehaul_route_mappings_v2(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "lmsc_migration_cd":
            validate_lmsc_migration_cd(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )
        elif job_key == "sort_code_update":
            validate_sort_code_update(
                row_number=row_number,
                row=row,
                header_index=header_index,
                findings=findings,
                add_fix=add_fix,
            )

    if job_key == "pending_manifest_corrections":
        validate_pending_manifest_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "User_role_access":
        validate_user_role_access_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            header_aliases=header_aliases,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "deactivate_nlc_bulk":
        validate_deactivate_nlc_bulk_blank_rows(
            uploaded_rows=uploaded_rows,
            findings=findings,
            add_fix=add_fix,
        )
        validate_deactivate_nlc_bulk_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "fmsc_migration":
        validate_fmsc_migration_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            header_aliases=header_aliases,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "titan_user_migration_titan":
        validate_titan_user_migration_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "titan_entity_corrections":
        validate_titan_entity_corrections_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "Linehaul_route_mappings_v2":
        validate_linehaul_route_mappings_v2_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "lmsc_migration_cd":
        validate_lmsc_migration_cd_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            findings=findings,
            add_fix=add_fix,
        )
    elif job_key == "sort_code_update":
        validate_sort_code_update_duplicates(
            uploaded_rows=uploaded_rows,
            header_index=header_index,
            findings=findings,
            add_fix=add_fix,
        )

    uploaded_row_count = len(non_empty_data_rows(uploaded_rows))
    lines = [
        f"Selected Jenkins job: {job_key}",
        f"Reference CSV: {reference_path.name}",
        f"Uploaded CSV: {uploaded_csv.name}",
        f"Reference encoding: {reference_encoding}",
        f"Uploaded encoding: {uploaded_encoding}",
        f"Expected columns: {', '.join(expected_headers) if expected_headers else 'None'}",
        f"Uploaded columns: {', '.join(actual_headers) if actual_headers else 'None'}",
        f"Uploaded data rows: {uploaded_row_count}",
    ]
    if findings:
        lines.append("Validation findings:")
        lines.extend(f"- {finding}" for finding in findings[:40])
        lines.append("Required fixes:")
        lines.extend(f"- {fix}" for fix in fixes[:40])
    else:
        lines.append("Validation findings: Uploaded CSV matches the stored correct format.")
        lines.append("Required fixes: None.")
    return ValidationReport(
        job_key=job_key,
        uploaded_csv_name=uploaded_csv.name,
        expected_headers=expected_headers,
        actual_headers=actual_headers,
        row_count=uploaded_row_count,
        findings=findings,
        fixes=fixes,
        technical_summary="\n".join(lines),
    )


def mask_value(value: str) -> str:
    value = value.strip()
    if not value:
        return "blank"
    if len(value) <= 4:
        return value
    return f"{value[:2]}***{value[-2:]}"


def looks_like_user_role_headers(headers: list[str]) -> bool:
    normalized = [re.sub(r"[^a-z0-9]+", "", header.lower()) for header in headers]
    role_like = {"role", "roel"}
    contact_like = {
        "usercontactnumber",
        "usercontact",
        "usernumber",
    }
    has_role = any(header in role_like for header in normalized)
    has_user_contact = any(header in contact_like or ("user" in header and "contact" in header) for header in normalized)
    return has_role or has_user_contact


def user_role_validation(rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["No rows found."]

    headers = [cell.strip() for cell in rows[0]]
    if not looks_like_user_role_headers(headers):
        return []

    issues: list[str] = []
    if headers[:2] != list(USER_ROLE_COLUMNS):
        missing = [column for column in USER_ROLE_COLUMNS if column not in headers]
        extra = [column for column in headers if column and column not in USER_ROLE_COLUMNS]
        issues.append(f"Header mismatch. Expected: {', '.join(USER_ROLE_COLUMNS)}.")
        if missing:
            issues.append(f"Missing column(s): {', '.join(missing)}.")
        if extra:
            issues.append(f"Unexpected or misspelled column(s): {', '.join(extra)}.")

    if not all(column in headers for column in USER_ROLE_COLUMNS):
        return issues

    contact_index = headers.index("User_contact_number")
    role_index = headers.index("Role")
    seen_contacts: dict[str, int] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        contact = row[contact_index].strip() if contact_index < len(row) else ""
        role = row[role_index].strip() if role_index < len(row) else ""
        if not contact:
            issues.append(f"Row {row_number}: User_contact_number is blank.")
        elif not contact.isdigit():
            issues.append(f"Row {row_number}: User_contact_number has non-digit data ({mask_value(contact)}).")
        elif len(contact) != 10:
            issues.append(
                f"Row {row_number}: User_contact_number must be 10 digits, found {len(contact)} digits."
            )
        elif contact in seen_contacts:
            issues.append(
                f"Row {row_number}: duplicate User_contact_number already present in row {seen_contacts[contact]}."
            )
        elif contact:
            seen_contacts[contact] = row_number

        if not role:
            issues.append(f"Row {row_number}: Role is blank.")
        elif role not in USER_ROLE_VALUES:
            issues.append(
                f"Row {row_number}: Role must be FE, ADMIN, or AM. Found {role!r}."
            )

    return issues


def csv_summary(path: Path) -> str:
    text, encoding_status = decode_bytes(path.read_bytes())
    return csv_summary_from_text(path.name, text, encoding_status)


def csv_summary_from_text(file_name: str, text: str, encoding_status: str) -> str:
    rows = csv_rows_from_text(text)
    headers = rows[0] if rows else []
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    issues = user_role_validation(rows)

    lines = [
        f"File: {file_name}",
        "Detected type: CSV/text table",
        f"Encoding: {encoding_status}",
        f"Columns: {', '.join(headers) if headers else 'None detected'}",
        f"Data rows: {len(data_rows)}",
    ]
    if issues:
        lines.append("Validation findings:")
        lines.extend(f"- {issue}" for issue in issues[:30])
    else:
        lines.append("Validation findings: No obvious CSV format issues found.")
    return "\n".join(lines)


def xlsx_shared_strings(zipped: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(zipped.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings: list[str] = []
    for item in root.findall("a:si", namespace):
        parts = [text.text or "" for text in item.findall(".//a:t", namespace)]
        strings.append("".join(parts))
    return strings


def xlsx_first_sheet_rows(path: Path, max_rows: int = 80) -> list[list[str]]:
    with zipfile.ZipFile(path) as zipped:
        shared_strings = xlsx_shared_strings(zipped)
        sheet_names = sorted(name for name in zipped.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
        if not sheet_names:
            return []
        root = ElementTree.fromstring(zipped.read(sheet_names[0]))

    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row_node in root.findall(".//a:sheetData/a:row", namespace)[:max_rows]:
        values: list[str] = []
        for cell in row_node.findall("a:c", namespace):
            value_node = cell.find("a:v", namespace)
            raw_value = value_node.text if value_node is not None else ""
            if cell.attrib.get("t") == "s" and raw_value.isdigit():
                value = shared_strings[int(raw_value)] if int(raw_value) < len(shared_strings) else raw_value
            else:
                value = raw_value
            values.append(value.strip())
        rows.append(values)
    return rows


def xlsx_summary(path: Path) -> str:
    try:
        rows = xlsx_first_sheet_rows(path)
    except Exception as error:
        return "\n".join(
            [
                f"File: {path.name}",
                "Detected type: Excel workbook",
                f"Could not inspect workbook: {error}",
            ]
        )

    headers = rows[0] if rows else []
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    issues = user_role_validation(rows)
    lines = [
        f"File: {path.name}",
        "Detected type: Excel workbook",
        f"Columns from first sheet: {', '.join(headers) if headers else 'None detected'}",
        f"Data rows inspected: {len(data_rows)}",
    ]
    if issues:
        lines.append("Validation findings:")
        lines.extend(f"- {issue}" for issue in issues[:30])
    else:
        lines.append("Validation findings: No obvious workbook format issues found.")
    return "\n".join(lines)


def upload_summary(path: Path) -> str:
    suffix = path.suffix.lower()
    data = path.read_bytes()
    text, encoding_status = decode_bytes(data)

    try:
        rows = csv_rows_from_text(text)
    except csv.Error:
        rows = []

    if suffix in {".csv", ".tsv"} or looks_like_csv_rows(rows):
        return csv_summary(path)
    if suffix == ".xlsx":
        return xlsx_summary(path)
    if suffix in {".txt", ".log", ".out", ".json", ".xml", ".yaml", ".yml"}:
        preview = text[:3000]
        return "\n".join(
            [
                f"File: {path.name}",
                "Detected type: text file",
                f"Encoding: {encoding_status}",
                "Text preview:",
                preview,
            ]
        )

    printable = sum(1 for character in text[:2000] if character.isprintable() or character.isspace())
    if text and printable / max(1, min(len(text), 2000)) > 0.85:
        return "\n".join(
            [
                f"File: {path.name}",
                "Detected type: text-like file",
                f"Encoding: {encoding_status}",
                "Text preview:",
                text[:3000],
            ]
        )
    return "\n".join(
        [
            f"File: {path.name}",
            f"Detected type: unsupported binary or unknown format ({path.suffix or 'no extension'})",
            f"Size: {len(data)} bytes",
            "The app stored this file, but could not safely inspect its contents.",
        ]
    )


def status_line(log_text: str) -> str:
    match = re.search(r"Finished:\s+(\w+)", log_text)
    return match.group(1) if match else "Unknown"


def clean_job_name(value: str) -> str:
    value = unquote(value).strip().strip('"').strip("'")
    value = value.replace("\\", "/")

    workspace_marker = "/workspace/"
    if workspace_marker in value:
        value = value.split(workspace_marker, 1)[1]
    elif value.startswith("workspace/"):
        value = value.removeprefix("workspace/")

    value = re.sub(r"@(?:tmp|script|libs|\d+)(?:/.*)?$", "", value)
    value = value.strip("/")
    return value or "Unknown"


def job_name(log_text: str, job_hint: str = "") -> str:
    if job_hint.strip():
        return clean_job_name(job_hint)

    patterns = (
        r"(?im)^Full project name:\s*(.+)$",
        r"(?im)^Project name:\s*(.+)$",
        r"(?im)^Job:\s*(.+)$",
        r"(?im)^Building in workspace\s+(.+)$",
        r"(?im)^Running on .+ in (?:workspace|remote workspace)\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, log_text)
        if match:
            return clean_job_name(match.group(1))

    return "Unknown"


def relevant_log_context(log_text: str) -> str:
    lines = log_text.splitlines()
    if not lines:
        return ""

    error_words = (
        "error",
        "exception",
        "traceback",
        "failed",
        "failure",
        "fatal",
        "invalid",
        "syntaxerror",
        "unicodeerror",
        "unicodedecodeerror",
        "permission denied",
        "missing required column",
        "no such file",
        "not found",
        "timed out",
        "timeout",
        "finished:",
    )

    selected_indexes: set[int] = set()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "# timeout=" in lowered:
            continue
        if "selected git installation does not exist" in lowered:
            continue
        if "the recommended git tool is:" in lowered:
            continue
        if any(word in lowered for word in error_words):
            start = max(0, index - 4)
            end = min(len(lines), index + 8)
            selected_indexes.update(range(start, end))

    if not selected_indexes:
        selected_indexes.update(range(max(0, len(lines) - 160), len(lines)))

    selected = [lines[index] for index in sorted(selected_indexes)]
    context = "\n".join(selected)
    if len(context) > 12000:
        context = context[-12000:]
    return context


def missing_input_file_name(log_text: str) -> str:
    patterns = (
        r"(?im)cannot stat ['\"]([^'\"]+\.csv)['\"]:\s+No such file or directory",
        r"(?im)([A-Za-z0-9_. -]+\.csv):\s+No such file or directory",
        r"(?im)No such file or directory:\s*['\"]([^'\"]+\.csv)['\"]",
        r"(?im)FileNotFoundError:.*No such file or directory:\s*['\"]([^'\"]+\.csv)['\"]",
        r"(?im)(?:cp|mv|cat|ls):\s+cannot (?:stat|access) ['\"]?([^'\"\n]+\.csv)['\"]?",
    )
    for pattern in patterns:
        match = re.search(pattern, log_text)
        if match:
            return match.group(1).strip()
    return ""


def log_indicates_missing_input_file(log_text: str) -> bool:
    patterns = (
        r"(?i)no file (?:was )?uploaded",
        r"(?i)input file .*not uploaded",
        r"(?i)uploaded file .*not found",
        r"(?i)file parameter .*empty",
        r"(?i)No such file or directory",
        r"(?i)cannot stat",
        r"(?i)cannot access",
        r"(?i)FileNotFoundError",
    )
    return any(re.search(pattern, log_text) for pattern in patterns)


def missing_input_file_explanation(log_text: str, *, assume_missing: bool = False) -> str:
    missing_file = missing_input_file_name(log_text)
    if not missing_file and not assume_missing and not log_indicates_missing_input_file(log_text):
        return ""
    expected_file = missing_file or "the required Jenkins input CSV"

    return "\n".join(
        [
            "Issue:",
            "The Jenkins input CSV was not uploaded or could not be found.",
            "",
            "Failed row/column:",
            "Not applicable. Jenkins failed before reading CSV rows.",
            "",
            "Simple reason:",
            f"The job expected {expected_file}, but Jenkins did not receive a readable CSV file for this run.",
            "",
            "Possible fix:",
            "Upload the required CSV file in Jenkins, then run the job again.",
            "",
            "Confidence:",
            "High",
        ]
    )


def location_before(log_text: str, position: int) -> str:
    matches = list(re.finditer(r"(?im)^Iteration:\s*\d+/\d+\s*\|\s*Location:\s*(.+)$", log_text[:position]))
    return matches[-1].group(1).strip() if matches else "Unknown"


def add_log_issue(issues: list[dict[str, str]], issue: dict[str, str]) -> None:
    key = (issue["location"], issue["field"], issue["problem"], issue["fix"])
    existing_keys = {(item["location"], item["field"], item["problem"], item["fix"]) for item in issues}
    if key not in existing_keys:
        issues.append(issue)


def log_failure_issues(log_text: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    current_location = "Unknown"

    for line in log_text.splitlines():
        location_match = re.search(r"(?i)^Iteration:\s*\d+/\d+\s*\|\s*Location:\s*(.+)$", line)
        if location_match:
            current_location = location_match.group(1).strip()
            continue

        missing_column_match = re.search(r"Missing required column:\s*(.+)$", line, re.I)
        if missing_column_match:
            column_name = missing_column_match.group(1).strip().strip("'\"")
            add_log_issue(
                issues,
                {
                    "location": current_location,
                    "field": column_name,
                    "problem": "required CSV column is missing",
                    "detail": f"Missing required column: {column_name}",
                    "fix": f"Add or rename the CSV header to exactly {column_name}.",
                },
            )
            continue

        if "[FAILED]" not in line:
            continue

        client_match = re.search(r"clientLocationName\s+['\"]([^'\"]+)['\"]\s+already present in system", line, re.I)
        if client_match:
            add_log_issue(
                issues,
                {
                    "location": current_location if current_location != "Unknown" else client_match.group(1).strip(),
                    "field": "clientLocationName",
                    "problem": "location already onboarded",
                    "detail": f"clientLocationName {client_match.group(1).strip()!r}",
                    "fix": "Do not upload this location again; use the correct update/existing-location process if this is an update.",
                },
            )
            continue

        pincode_match = re.search(r"Location with pincode\s*,?\s*(\d+)\s+already exists", line, re.I)
        if pincode_match:
            add_log_issue(
                issues,
                {
                    "location": current_location,
                    "field": "pincode",
                    "problem": f"pincode {pincode_match.group(1)} already exists for another location",
                    "detail": f"pincode {pincode_match.group(1)}",
                    "fix": "Use a different valid pincode, or check the existing location record before onboarding again.",
                },
            )
            continue

        duplicate_contact_match = re.search(
            r"Duplicate entry ['\"](\d{10})['\"] for key ['\"]users\.(?:contact_number|username_UNIQUE)['\"]",
            line,
            re.I,
        )
        if duplicate_contact_match or re.search(r"Admin User with this contact number is already present", line, re.I):
            contact_number = duplicate_contact_match.group(1) if duplicate_contact_match else ""
            fix = (
                "This contact number is already deactivated or blacklisted in the users table. "
                "Use a new valid 10-digit contact number."
            ) if duplicate_contact_match else (
                "This contact number is already present in the users table. "
                "Use a new valid 10-digit contact number."
            )
            add_log_issue(
                issues,
                {
                    "location": current_location,
                    "field": "contactNumber",
                    "problem": "admin contact number already used",
                    "detail": f"contactNumber {mask_value(contact_number)}" if contact_number else "contactNumber already present",
                    "fix": fix,
                },
            )
            continue

        titan_contact_match = re.search(
            r"\bcontact[_ ]number\b[^,\n]*(?:not found|not present|invalid|does not exist)",
            line,
            re.I,
        )
        if titan_contact_match:
            value_match = re.search(r"\bcontact[_ ]number\b[^0-9]*(\d{4,})", line, re.I)
            contact_number = value_match.group(1) if value_match else ""
            add_log_issue(
                issues,
                {
                    "location": current_location,
                    "field": "contact_number",
                    "problem": "contact_number is invalid or not available in the backend table",
                    "detail": f"contact_number {mask_value(contact_number)}" if contact_number else "contact_number",
                    "fix": "Recheck contact_number in the backend table and upload the correct existing contact_number.",
                },
            )
            continue

        titan_location_match = re.search(
            r"\blocation[_ ]id\b[^,\n]*(?:not found|not present|invalid|does not exist)",
            line,
            re.I,
        )
        if titan_location_match:
            value_match = re.search(r"\blocation[_ ]id\b[^0-9]*(\d+)", line, re.I)
            location_id = value_match.group(1) if value_match else ""
            add_log_issue(
                issues,
                {
                    "location": current_location,
                    "field": "location_id",
                    "problem": "location_id is invalid or not available in the backend table",
                    "detail": f"location_id {location_id}" if location_id else "location_id",
                    "fix": "Recheck location_id in the backend table and upload the correct existing location_id.",
                },
            )
            continue

        city_match = re.search(r"Invalid or missing city_id:\s*['\"]?([^'\"\n]*)['\"]?", line, re.I)
        if city_match:
            city_id = city_match.group(1).strip()
            add_log_issue(
                issues,
                {
                    "location": current_location,
                    "field": "city_id",
                    "problem": f"city_id is {'blank' if not city_id else 'invalid'}",
                    "detail": f"city_id {city_id or 'blank'}",
                    "fix": "Recheck the city_id in the Titan cities table and upload the correct active city_id.",
                },
            )
            continue

        if re.search(r"Invalid admin user name", line, re.I):
            add_log_issue(
                issues,
                {
                    "location": current_location,
                    "field": "branch_admin_name",
                    "problem": "admin user name is invalid",
                    "detail": "branch_admin_name",
                    "fix": "Use a branch_admin_name with at least 2 words.",
                },
            )

    return issues


def deterministic_log_explanation(log_text: str) -> str:
    issues = log_failure_issues(log_text)
    if not issues:
        return ""

    lines = [
        "Issue:",
        f"Jenkins found {len(issues)} failed row(s) in the log.",
        "",
        "Failed row/column:",
    ]
    for index, issue in enumerate(issues, start=1):
        detail = f" ({issue['detail']})" if issue["detail"] and issue["detail"] != issue["field"] else ""
        lines.append(
            f"{index}. Location {issue['location']!r}; {issue['field']}: {issue['problem']}{detail}."
        )

    unique_reasons = []
    for issue in issues:
        reason = f"{issue['field']}: {issue['problem']}"
        if reason not in unique_reasons:
            unique_reasons.append(reason)

    lines.extend(["", "Simple reason:"])
    lines.extend(f"- {reason}." for reason in unique_reasons)

    unique_fixes = []
    for issue in issues:
        if issue["fix"] not in unique_fixes:
            unique_fixes.append(issue["fix"])

    lines.extend(["", "Possible fix:"])
    lines.extend(f"{index}. {fix}" for index, fix in enumerate(unique_fixes, start=1))
    lines.extend(["", "Confidence:", "High"])
    return "\n".join(lines)


def existing_location_explanation(log_text: str) -> str:
    failed_match = re.search(
        r"(?im)\[FAILED\]:\s*clientLocationName\s+['\"]([^'\"]+)['\"]\s+already present in system",
        log_text,
    )
    if not failed_match:
        return ""

    client_location_name = failed_match.group(1).strip()
    location = location_before(log_text, failed_match.start())
    if location == "Unknown":
        location = client_location_name

    return "\n".join(
        [
            "Issue:",
            "This location is already onboarded in the system.",
            "",
            "Failed row/column:",
            f"clientLocationName {client_location_name!r}; Location {location!r}.",
            "",
            "Simple reason:",
            f"{location} is already present in the location table, so Jenkins skipped this row.",
            "",
            "Possible fix:",
            "Do not upload this location again. If this is a genuine update, use the correct update/existing-location process instead of onboarding it as new.",
            "",
            "Confidence:",
            "High",
        ]
    )


def existing_pincode_explanation(log_text: str) -> str:
    failed_match = re.search(
        r"(?im)\[FAILED\]:\s*API failure:\s*status=500,\s*msg=Location with pincode\s*,?\s*(\d+)\s+already exists",
        log_text,
    )
    if not failed_match:
        return ""

    pincode = failed_match.group(1).strip()
    location = location_before(log_text, failed_match.start())

    return "\n".join(
        [
            "Issue:",
            "Another location already exists with the same pincode.",
            "",
            "Failed row/column:",
            f"Location {location!r}; pincode {pincode}.",
            "",
            "Simple reason:",
            f"Jenkins tried to onboard {location}, but pincode {pincode} is already present for another location in the location table.",
            "",
            "Possible fix:",
            "Use a different valid pincode for this location, or check the existing location record before onboarding again.",
            "",
            "Confidence:",
            "High",
        ]
    )


def duplicate_admin_contact_explanation(log_text: str) -> str:
    duplicate_match = re.search(
        r"(?im)Duplicate entry ['\"](\d{10})['\"] for key ['\"]users\.(?:contact_number|username_UNIQUE)['\"]",
        log_text,
    )
    admin_match = re.search(
        r"(?im)Admin User with this contact number is already present",
        log_text,
    )
    if not duplicate_match and not admin_match:
        return ""

    match = duplicate_match or admin_match
    location = location_before(log_text, match.start())
    contact_number = duplicate_match.group(1) if duplicate_match else ""
    masked_contact = mask_value(contact_number) if contact_number else "the uploaded contactNumber"
    simple_reason = (
        "This contact number is already deactivated or blacklisted in the users table."
        if duplicate_match
        else "This contact number is already present in the users table."
    )

    return "\n".join(
        [
            "Issue:",
            "The admin contact number is already used in the system.",
            "",
            "Failed row/column:",
            f"Location {location!r}; contactNumber {masked_contact}.",
            "",
            "Simple reason:",
            simple_reason,
            "",
            "Possible fix:",
            "Use a new valid 10-digit contact number.",
            "",
            "Confidence:",
            "High",
        ]
    )


def invalid_city_explanation(log_text: str) -> str:
    failed_match = re.search(r"(?im)Invalid or missing city_id:\s*['\"]?([^'\"\n]*)['\"]?", log_text)
    if not failed_match:
        return ""

    city_id = failed_match.group(1).strip()
    location = location_before(log_text, failed_match.start())
    city_display = city_id or "blank"

    return "\n".join(
        [
            "Issue:",
            "The city_id is missing or invalid.",
            "",
            "Failed row/column:",
            f"Location {location!r}; city_id {city_display}.",
            "",
            "Simple reason:",
            "The uploaded city_id is blank, invalid, or not available as an active city in the Titan cities table.",
            "",
            "Possible fix:",
            "Recheck the city_id in the Titan cities table and upload the correct active city_id.",
            "",
            "Confidence:",
            "High",
        ]
    )


def invalid_admin_name_explanation(log_text: str) -> str:
    failed_match = re.search(r"(?im)Invalid admin user name", log_text)
    if not failed_match:
        return ""

    location = location_before(log_text, failed_match.start())

    return "\n".join(
        [
            "Issue:",
            "The admin user name is invalid.",
            "",
            "Failed row/column:",
            f"Location {location!r}; branch_admin_name.",
            "",
            "Simple reason:",
            "The branch_admin_name format is invalid. This job expects the admin name to have at least 2 words.",
            "",
            "Possible fix:",
            "Use a branch_admin_name with at least 2 words, then run the job again.",
            "",
            "Confidence:",
            "High",
        ]
    )


def network_metadata_id_explanation(log_text: str) -> str:
    patterns = (
        r"(?im)(?:network_metadata|network metadata).{0,120}(?:id\s*)?['\"]?(\d+)['\"]?.{0,80}(?:not found|not present|no record)",
        r"(?im)(?:id\s*)['\"]?(\d+)['\"]?.{0,120}(?:not found|not present|no record).{0,80}(?:network_metadata|network metadata)",
        r"(?im)(?:no record found|not found|not present).{0,120}(?:network_metadata|network metadata).{0,80}(?:id\s*)?['\"]?(\d+)['\"]?",
    )
    for pattern in patterns:
        match = re.search(pattern, log_text)
        if match:
            network_id = match.group(1)
            return "\n".join(
                [
                    "Issue:",
                    "The id is not present in the network_metadata table.",
                    "",
                    "Failed row/column:",
                    f"id {network_id}.",
                    "",
                    "Simple reason:",
                    "Jenkins could not find this id in network_metadata, so it cannot deactivate that NLC.",
                    "",
                    "Possible fix:",
                    "Recheck the id and upload an id that exists in the network_metadata table.",
                    "",
                    "Confidence:",
                    "High",
                ]
            )
    return ""


def build_prompt(file_name: str, log_text: str, job_hint: str = "", file_analysis: str = "") -> str:
    focused_log_text = relevant_log_context(log_text)
    detected_job_name = job_name(log_text, job_hint)
    analysis_block = file_analysis.strip() or "No additional input/supporting file analysis was provided."

    return f"""You are a Jenkins failure explainer for support engineers.

Read this focused Jenkins console log context and the uploaded Jenkins input CSV validation result.
Explain the real failure in simple plain text.

Rules:
- Focus on the actual error, not normal setup lines like git checkout or package already installed.
- If the build succeeded, say no failure was detected.
- Do not invent evidence.
- Give a practical possible fix.
- Keep the language simple for a new colleague.
- If there is not enough evidence, say exactly what is missing.
- For Job, use the detected Jenkins job exactly. If it is Unknown, say Unknown.
- If the CSV validation result shows a bad row, bad column, missing column, invalid value, invalid phone number, or encoding issue, include that exact row/column in the explanation.
- The CSV validation result is authoritative for uploaded input CSV problems.
- If the CSV validation result includes Required fixes, copy those fixes into Possible fix in plain English and do not add unrelated Jenkins setup fixes.
- Do not say the CSV has an encoding issue unless the validation result explicitly says it has an encoding issue.
- If the Jenkins log and CSV validation result point to different problems, say both clearly and explain that the selected job, log, and CSV may not belong to the same failed run.
- Do not suggest installing Git only because the log says "Selected Git installation does not exist"; ignore it when later git commands run or the CSV validation has concrete findings.
- If Jenkins status is SUCCESS but the log or CSV validation shows skipped/invalid input rows, explain the input validation issue instead of saying everything is fine.
- If the log says "API failure: status=500" and "Pincodes ... are not found", explain that the pincode is not available in the backend pincode table and the team should add or activate that pincode before retrying.
- If the log says city_id is inactive or invalid, explain that the city_id must be an active integer city ID in the backend cities table.
- If no input CSV validation was provided and the log says "cannot stat" or "No such file or directory" for a CSV file, explain that the Jenkins input CSV was not uploaded or the file name did not match.
- If the log says "clientLocationName '<value>' already present in system", explain that the full Location value is already onboarded and already present in the location table.
- If the log says "Location with pincode <value> already exists", explain that another location already exists with the same pincode in the location table.
- If the log says the admin contact number is already present, explain that contactNumber is already used for another user or location and a new number is needed.
- If the log says "Invalid or missing city_id", explain that city_id is blank, invalid, or not active/present in the Titan cities table.
- If the log says "Invalid admin user name", explain that branch_admin_name format is invalid and should have at least 2 words.
- If the log says an id is not found or not present in network_metadata, explain that the id must exist in the network_metadata table before this job can deactivate it.
- If the log says contact_number or location_id is not found, not present, invalid, or does not exist for titan_user_migration_titan, explain that the value must exist in the required backend table and the user should upload the correct value.
- If the log says a sort centre, sort center, sort code, or LMDC is invalid, inactive, not found, or not present for lmsc_migration_cd, explain that the sort centre/code must exist and be active in the backend table before this migration can run.
- If the log says a sort code or LMDC is invalid, inactive, not found, or not present for sort_code_update, explain that the sort code must exist and be active in the backend table before this update can run.

Always use this short format:
Issue:
Failed row/column:
Simple reason:
Possible fix:
Confidence:

Keep the response under 10 lines. Do not repeat the full validation details.

File name:
{file_name}

Detected Jenkins status:
{status_line(log_text)}

Detected Jenkins job:
{detected_job_name}

Uploaded Jenkins input CSV validation:
{analysis_block}

Focused Jenkins console log context:
{focused_log_text}
"""


def ask_ollama(file_name: str, log_text: str, job_hint: str = "", file_analysis: str = "") -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_prompt(file_name, log_text, job_hint, file_analysis),
        "stream": False,
        "options": {"temperature": 0.2},
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["response"].strip()


def process_log(path: Path, job_hint: str = "", support_path: Path | None = None) -> Path:
    log_text = read_text(path)
    detected_job_name = job_name(log_text, job_hint)
    validation_report = validate_csv_against_reference(job_hint, support_path) if support_path is not None else None
    file_analysis = validation_report.prompt_summary() if validation_report is not None else ""

    should_assume_missing_input = (
        support_path is None
        and bool(job_hint)
        and status_line(log_text) == "FAILURE"
        and not log_failure_issues(log_text)
    )
    missing_file_explanation = (
        missing_input_file_explanation(log_text, assume_missing=should_assume_missing_input) if support_path is None else ""
    )
    log_explanation = deterministic_log_explanation(log_text)
    existing_location = existing_location_explanation(log_text)
    existing_pincode = existing_pincode_explanation(log_text)
    duplicate_admin_contact = duplicate_admin_contact_explanation(log_text)
    invalid_city = invalid_city_explanation(log_text)
    invalid_admin_name = invalid_admin_name_explanation(log_text)
    network_metadata_id = network_metadata_id_explanation(log_text)
    if missing_file_explanation:
        explanation = missing_file_explanation
    elif log_explanation:
        explanation = log_explanation
    elif network_metadata_id:
        explanation = network_metadata_id
    elif existing_location:
        explanation = existing_location
    elif existing_pincode:
        explanation = existing_pincode
    elif duplicate_admin_contact:
        explanation = duplicate_admin_contact
    elif invalid_city:
        explanation = invalid_city
    elif validation_report is not None and validation_report.has_issues:
        explanation = validation_report.plain_english_explanation()
    elif invalid_admin_name:
        explanation = invalid_admin_name
    elif status_line(log_text) == "SUCCESS" and validation_report is not None and not validation_report.has_issues:
        explanation = (
            "Issue:\n"
            "No failure detected. Jenkins finished successfully and the uploaded CSV matches the stored reference format.\n\n"
            "Fix:\n"
            "No CSV format fix is needed for this run.\n\n"
            "Problem found:\n"
            "No problem found in the Jenkins log or uploaded CSV."
        )
    else:
        try:
            explanation = ask_ollama(path.name, log_text, job_hint, file_analysis)
        except Exception as error:
            explanation = (
                "Issue:\n"
                "Could not get Ollama explanation.\n\n"
                "Fix:\n"
                "Check that Ollama is running and the selected model is downloaded.\n\n"
                f"Problem found:\n{error}"
            )

    created_at = now_app_time()
    output_path = OUTPUT_DIR / output_name(path.name)
    output_path.write_text(
        "\n".join(
            [
                "Jenkins AI Failure Explanation",
                "==============================",
                "",
                f"Jenkins log file: {path.name}",
                f"Uploaded input CSV: {support_path.name if support_path else 'None'}",
                f"Selected Jenkins job: {job_hint}",
                f"Detected Jenkins job: {detected_job_name}",
                f"Detected Jenkins status: {status_line(log_text)}",
                f"Created at: {created_at}",
                "",
                "Plain-English Explanation",
                "-------------------------",
                explanation,
                "",
            ]
        ),
        encoding="utf-8",
    )

    archive_path = ARCHIVE_DIR / path.name
    if archive_path.exists():
        archive_path = ARCHIVE_DIR / f"{int(time.time())}-{path.name}"
    shutil.copy2(path, archive_path)
    return output_path


def process_csv_only(support_path: Path, job_hint: str = "") -> Path:
    validation_report = validate_csv_against_reference(job_hint, support_path)
    explanation = validation_report.plain_english_explanation()
    created_at = now_app_time()
    output_path = OUTPUT_DIR / output_name(support_path.name)
    output_path.write_text(
        "\n".join(
            [
                "Jenkins AI Failure Explanation",
                "==============================",
                "",
                "Jenkins log file: None",
                f"Uploaded input CSV: {support_path.name}",
                f"Selected Jenkins job: {job_hint}",
                f"Detected Jenkins job: {job_hint}",
                "Detected Jenkins status: Not run",
                f"Created at: {created_at}",
                "",
                "Plain-English Explanation",
                "-------------------------",
                explanation,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return output_path


def render_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #171a1f;
      --muted: #5d6673;
      --border: #d9dee7;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --accent-soft: #e6f6f3;
      --danger: #9f1239;
      --field: #fbfcfe;
      --shadow: 0 14px 40px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.12), transparent 320px),
        linear-gradient(180deg, #edf6f4 0, rgba(237, 246, 244, 0) 280px),
        var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    header {{
      border-bottom: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    .wrap {{
      width: min(1100px, calc(100vw - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      min-height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }}
    h1 {{ margin: 0; font-size: 21px; letter-spacing: 0; }}
    nav a {{
      color: var(--text);
      text-decoration: none;
      margin-left: 18px;
      font-weight: 600;
      font-size: 14px;
    }}
    nav a:hover {{ color: var(--accent); }}
    main {{ padding: 30px 0 48px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 26px;
      margin-bottom: 18px;
      box-shadow: var(--shadow);
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) 320px;
      gap: 22px;
      align-items: start;
    }}
    .hero h2 {{
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.12;
    }}
    .hero p {{ margin: 0 0 20px; max-width: 720px; }}
    .upload-form {{
      display: grid;
      gap: 14px;
      min-width: 0;
    }}
    .field {{
      min-width: 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #f8fafc;
      padding: 15px;
    }}
    .field label {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      font-weight: 750;
      font-size: 14px;
    }}
    .hint {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
    }}
    .actions {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      padding-top: 2px;
    }}
    .status-card {{
      background: #10201e;
      border-radius: 8px;
      color: #e7f8f4;
      padding: 20px;
    }}
    .status-card h3 {{
      margin: 0 0 14px;
      font-size: 15px;
      color: #9de2d8;
    }}
    .stat {{
      border-top: 1px solid rgba(255, 255, 255, 0.14);
      padding: 12px 0;
    }}
    .stat:first-of-type {{ border-top: 0; padding-top: 0; }}
    .stat strong {{ display: block; font-size: 13px; color: #9de2d8; }}
    .stat span {{ display: block; margin-top: 3px; font-weight: 700; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }}
    .tile {{
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }}
    .tile strong {{ display: block; margin-bottom: 6px; }}
    .form-row {{ margin-top: 0; }}
    .select-wrap {{
      position: relative;
      margin-top: 10px;
    }}
    .select-wrap::after {{
      content: "";
      position: absolute;
      right: 15px;
      top: 50%;
      width: 9px;
      height: 9px;
      border-right: 2px solid #475569;
      border-bottom: 2px solid #475569;
      transform: translateY(-65%) rotate(45deg);
      pointer-events: none;
    }}
    select {{
      appearance: none;
      -webkit-appearance: none;
      display: block;
      width: 100%;
      min-width: 0;
      height: 48px;
      border: 1px solid #c8d0dc;
      border-radius: 8px;
      padding: 0 42px 0 14px;
      background: #ffffff;
      color: var(--text);
      font: inherit;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    select option {{
      font-size: 13px;
      font-weight: 650;
    }}
    select:focus {{
      outline: 3px solid rgba(15, 118, 110, 0.16);
      border-color: var(--accent);
      box-shadow: 0 0 0 1px var(--accent);
    }}
    input[type=text] {{
      display: block;
      width: 100%;
      min-width: 0;
      height: 48px;
      border: 1px solid #c8d0dc;
      border-radius: 8px;
      padding: 0 14px;
      background: var(--field);
      color: var(--text);
      font: inherit;
    }}
    .muted {{ color: var(--muted); }}
    input[type=file] {{
      display: block;
      width: 100%;
      min-width: 0;
      border: 1px dashed #aab3c2;
      border-radius: 8px;
      padding: 14px;
      background: var(--field);
      color: var(--text);
      font: inherit;
    }}
    input[type=file]:focus, input[type=text]:focus {{
      outline: 3px solid rgba(15, 118, 110, 0.16);
      border-color: var(--accent);
    }}
    button {{
      margin-top: 0;
      background: var(--accent);
      color: white;
      border: 0;
      border-radius: 7px;
      min-height: 46px;
      padding: 0 18px;
      font-weight: 700;
      cursor: pointer;
      font: inherit;
    }}
    button:hover {{ background: var(--accent-dark); }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{ background: #eef2f7; font-size: 13px; }}
    tr:last-child td {{ border-bottom: 0; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #0f172a;
      color: #e5e7eb;
      padding: 18px;
      border-radius: 8px;
      overflow: auto;
      font-size: 14px;
    }}
    .ok {{
      border-color: #b9e2c9;
      background: var(--accent-soft);
    }}
    .error {{
      border-color: #fecdd3;
      background: #fff1f2;
      color: var(--danger);
    }}
    a {{ color: #14532d; font-weight: 700; }}
    @media (max-width: 760px) {{
      .topbar {{ align-items: flex-start; flex-direction: column; padding: 14px 0; }}
      nav a {{ margin: 0 18px 0 0; }}
      .hero {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .hero h2 {{ font-size: 25px; }}
      .panel {{ padding: 18px; }}
      .field label {{ align-items: flex-start; flex-direction: column; gap: 3px; }}
      .hint {{ white-space: normal; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>Jenkins Log AI Explainer</h1>
      <nav>
        <a href="/">Upload</a>
        <a href="/results">Results</a>
      </nav>
    </div>
  </header>
  <main class="wrap">
    {body}
  </main>
</body>
</html>""".encode("utf-8")


def result_files() -> list[Path]:
    return sorted(OUTPUT_DIR.glob("*.txt"), key=lambda path: path.stat().st_mtime, reverse=True)


def output_field(path: Path, label: str) -> str:
    pattern = re.compile(rf"^{re.escape(label)}:\s*(.*)$", re.MULTILINE)
    match = pattern.search(read_text(path))
    return match.group(1).strip() if match else "Unknown"


class Handler(BaseHTTPRequestHandler):
    def send_html(self, title: str, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = render_page(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.home()
        elif parsed.path == "/results":
            self.results()
        elif parsed.path.startswith("/result/"):
            self.result_detail(unquote(parsed.path.removeprefix("/result/")))
        else:
            self.send_html("Not found", '<div class="panel error">Page not found.</div>', HTTPStatus.NOT_FOUND)

    def home(self) -> None:
        jobs = reference_jobs()
        if jobs:
            options = "".join(f'<option value="{html.escape(job)}">{html.escape(job)}</option>' for job in jobs)
            job_control = f"""
              <label for="jobkey"><span>Jenkins job</span><span class="hint">Stored reference format</span></label>
              <div class="select-wrap">
                <select id="jobkey" name="jobkey" required>
                  {options}
                </select>
              </div>
            """
            submit_button = '<button type="submit">Upload And Explain</button>'
        else:
            job_control = '<div class="panel error">No reference formats found. Add a correct CSV under <code>/app/reference_formats/&lt;job_name&gt;/correct_input.csv</code>.</div>'
            submit_button = '<button type="submit" disabled>Upload And Explain</button>'

        body = f"""
        <section class="panel hero">
          <div>
            <h2>Explain Jenkins failures in plain English</h2>
            <p class="muted">Select the Jenkins job and upload the failed console log, the input CSV, or both. CSV-only uploads are useful for testing the format before running Jenkins.</p>
            <form class="upload-form" action="/upload" method="post" enctype="multipart/form-data">
              <div class="field">
                {job_control}
              </div>
              <div class="field">
                <label for="logfile"><span>Jenkins failed console log</span><span class="hint">Optional .txt, .log, .out</span></label>
                <input id="logfile" name="logfile" type="file" accept=".txt,.log,.out">
              </div>
              <div class="field">
                <label for="supportfile"><span>Jenkins input CSV</span><span class="hint">Optional .csv</span></label>
                <input id="supportfile" name="supportfile" type="file" accept=".csv">
              </div>
              <div class="actions">
                {submit_button}
                <span class="muted">Outputs are saved in Docker.</span>
              </div>
            </form>
          </div>
          <aside class="status-card">
            <h3>Current Setup</h3>
            <div class="stat"><strong>Website</strong><span>Docker container</span></div>
            <div class="stat"><strong>AI model</strong><span>Local Mac Ollama</span></div>
            <div class="stat"><strong>Storage</strong><span>/data inside Docker</span></div>
          </aside>
        </section>
        <section class="grid">
          <div class="tile">
            <strong>Input logs</strong>
            <code>/data/input</code>
          </div>
          <div class="tile">
            <strong>Input CSVs</strong>
            <code>/data/support</code>
          </div>
          <div class="tile">
            <strong>AI output</strong>
            <code>/data/output</code>
          </div>
          <div class="tile">
            <strong>Archive</strong>
            <code>/data/archive</code>
          </div>
        </section>
        <section class="panel">
          <h2>Reference Formats</h2>
          <p class="muted">Available jobs: {html.escape(', '.join(jobs) if jobs else 'None')}</p>
        </section>
        """
        self.send_html("Upload", body)

    def results(self) -> None:
        rows = []
        for path in result_files():
            stat = path.stat()
            detected_job_name = output_field(path, "Selected Jenkins job")
            if detected_job_name == "Unknown":
                detected_job_name = output_field(path, "Detected Jenkins job")
            rows.append(
                "<tr>"
                f"<td><a href=\"/result/{quote(path.name)}\">{html.escape(path.name)}</a></td>"
                f"<td>{html.escape(detected_job_name)}</td>"
                f"<td>{timestamp_app_time(stat.st_mtime)}</td>"
                f"<td>{stat.st_size} bytes</td>"
                "</tr>"
            )
        empty_row = '<tr><td colspan="4" class="muted">No outputs yet.</td></tr>'
        table = (
            "<table><thead><tr><th>Output File</th><th>Jenkins Job</th><th>Created</th><th>Size</th></tr></thead>"
            f"<tbody>{''.join(rows) or empty_row}</tbody></table>"
        )
        self.send_html("Results", f'<section class="panel"><h2>Results</h2>{table}</section>')

    def result_detail(self, name: str) -> None:
        path = OUTPUT_DIR / safe_filename(name)
        if not path.exists() or path.parent != OUTPUT_DIR:
            self.send_html("Not found", '<div class="panel error">Result not found.</div>', HTTPStatus.NOT_FOUND)
            return
        content = html.escape(read_text(path))
        self.send_html("Result", f'<section class="panel"><h2>{html.escape(path.name)}</h2><pre>{content}</pre></section>')

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/upload":
            self.send_html("Not found", '<div class="panel error">Page not found.</div>', HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_html("Upload error", '<div class="panel error">File is empty or too large.</div>', HTTPStatus.BAD_REQUEST)
            return

        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + body
        )

        part = None
        support_part = None
        job_key = ""
        for candidate in message.iter_parts():
            field_name = candidate.get_param("name", header="content-disposition")
            if field_name == "logfile" and candidate.get_filename():
                part = candidate
            elif field_name == "supportfile" and candidate.get_filename():
                support_part = candidate
            elif field_name == "jobkey":
                job_key = (candidate.get_content() or "").strip()

        if part is None and support_part is None:
            self.send_html("Upload error", '<div class="panel error">Upload a Jenkins log, an input CSV, or both.</div>', HTTPStatus.BAD_REQUEST)
            return

        if not valid_job_key(job_key):
            self.send_html("Upload error", '<div class="panel error">Unknown Jenkins job selected.</div>', HTTPStatus.BAD_REQUEST)
            return

        if support_part is not None:
            support_name = safe_filename(support_part.get_filename() or "jenkins-input.csv")
            if not support_name.lower().endswith(".csv"):
                self.send_html("Upload error", '<div class="panel error">Jenkins input file must be a .csv file.</div>', HTTPStatus.BAD_REQUEST)
                return

        input_path = save_upload(part, INPUT_DIR, "jenkins-upload.txt") if part is not None else None
        support_path = save_upload(support_part, SUPPORT_DIR, "jenkins-input.csv") if support_part is not None else None

        try:
            output_path = process_log(input_path, job_key, support_path) if input_path is not None else process_csv_only(support_path, job_key)
        except Exception as error:
            self.send_html(
                "Upload error",
                f'<div class="panel error">Could not process upload: {html.escape(str(error))}</div>',
                HTTPStatus.BAD_REQUEST,
            )
            return
        body_html = (
            '<section class="panel ok">'
            "<h2>Explanation Created</h2>"
            f"<p><strong>Jenkins job:</strong> {html.escape(job_key)}</p>"
            f"<p><strong>Jenkins log:</strong> {html.escape(input_path.name if input_path else 'Not uploaded')}</p>"
            f"<p><strong>Input CSV:</strong> {html.escape(support_path.name if support_path else 'Not uploaded')}</p>"
            f"<p><strong>Output:</strong> <a href=\"/result/{quote(output_path.name)}\">{html.escape(output_path.name)}</a></p>"
            "</section>"
        )
        self.send_html("Created", body_html)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    ensure_dirs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Jenkins AI uploader running at http://{HOST}:{PORT}")
    server.serve_forever()
