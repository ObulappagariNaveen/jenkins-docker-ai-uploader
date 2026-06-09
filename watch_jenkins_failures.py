#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from html import unescape
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin

import app


DEFAULT_JENKINS_BASE_URL = "https://jenkins.prd.valmo.in/job/support/job/log10/job/Regular_tasks"
STATE_PATH = app.DATA_DIR / "watcher_state.json"


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def auth_header() -> dict[str, str]:
    user = env_value("JENKINS_USER")
    token = env_value("JENKINS_API_TOKEN")
    if not user or not token:
        raise RuntimeError("Set JENKINS_USER and JENKINS_API_TOKEN before starting the watcher.")
    encoded = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def request_text(url: str, headers: dict[str, str], timeout: int) -> str:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def request_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, object]:
    return json.loads(request_text(url, headers, timeout))


def job_url(base_url: str, job_name: str) -> str:
    return f"{base_url.rstrip('/')}/job/{quote(job_name, safe='')}"


def load_state() -> dict[str, object]:
    if not STATE_PATH.exists():
        return {"processed": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": {}}


def save_state(state: dict[str, object]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def safe_log_name(job_name: str, build_number: int) -> str:
    safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_name)
    return f"jenkins-auto-{safe_job}-#{build_number}.txt"


def safe_support_name(job_name: str, build_number: int, file_name: str) -> str:
    safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_name)
    safe_file = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file_name).name or "input.csv")
    return f"jenkins-auto-{safe_job}-#{build_number}-{safe_file}"


def input_parameter_links(parameters_html: str, parameters_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for match in re.finditer(r'href=["\']([^"\']*parameter/[^"\']+\.csv(?:/[^"\']*)?)["\']', parameters_html, re.I):
        href = unescape(match.group(1))
        if "*view*" in href:
            continue
        file_name = Path(href.rstrip("/").split("/")[-1]).name
        links.append((file_name, urljoin(parameters_url, href)))
    return links


def fetch_input_parameter_csv(
    *,
    build_url: str,
    headers: dict[str, str],
    timeout: int,
) -> tuple[str, str] | None:
    parameters_url = f"{build_url.rstrip('/')}/parameters/"
    try:
        parameters_html = request_text(parameters_url, headers, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    links = input_parameter_links(parameters_html, parameters_url)
    if not links:
        return None
    file_name, download_url = links[0]
    return file_name, request_text(download_url, headers, timeout)


def save_input_csv(job_name: str, build_number: int, input_csv: tuple[str, str] | None) -> Path | None:
    if input_csv is None:
        return None
    file_name, csv_text = input_csv
    support_path = app.SUPPORT_DIR / safe_support_name(job_name, build_number, file_name)
    support_path.write_text(csv_text, encoding="utf-8")
    return support_path


def input_csv_has_validation_issues(job_name: str, input_csv: tuple[str, str] | None) -> bool:
    if input_csv is None:
        return False
    _, csv_text = input_csv
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as handle:
            handle.write(csv_text)
            temp_path = Path(handle.name)
        return app.validate_csv_against_reference(job_name, temp_path).has_issues
    except Exception:
        return True
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def mark_processed(state: dict[str, object], job_name: str, build_number: int, result: str, output_name: str = "") -> None:
    processed = state.setdefault("processed", {})
    if not isinstance(processed, dict):
        processed = {}
        state["processed"] = processed
    processed[f"{job_name}#{build_number}"] = {
        "job": job_name,
        "build": build_number,
        "result": result,
        "output": output_name,
        "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def already_processed(state: dict[str, object], job_name: str, build_number: int) -> bool:
    processed = state.get("processed", {})
    return isinstance(processed, dict) and f"{job_name}#{build_number}" in processed


def log_has_failure_evidence(log_text: str) -> bool:
    patterns = (
        r"\[FAILED\]",
        r"(?im)^Traceback",
        r"(?i)\bException\b",
        r"(?i)\bKeyError\b",
        r"(?i)\bERROR\b",
        r"(?i)\bInvalid\b",
        r"(?i)Missing required column",
        r"(?i)\bnot found\b",
        r"(?i)\balready exists\b",
        r"(?i)\balready present\b",
    )
    return any(re.search(pattern, log_text) for pattern in patterns)


def explain_failed_build(
    *,
    base_url: str,
    headers: dict[str, str],
    timeout: int,
    state: dict[str, object],
    job_name: str,
    build_number: int,
    result: str = "FAILURE",
    build_url: str = "",
) -> str:
    console_url = f"{job_url(base_url, job_name)}/{build_number}/consoleText"
    log_text = request_text(console_url, headers, timeout)
    app.ensure_dirs()
    input_csv = fetch_input_parameter_csv(build_url=build_url or f"{job_url(base_url, job_name)}/{build_number}/", headers=headers, timeout=timeout)
    log_path = app.INPUT_DIR / safe_log_name(job_name, build_number)
    log_path.write_text(log_text, encoding="utf-8")
    support_path = save_input_csv(job_name, build_number, input_csv)
    output_path = app.process_log(log_path, job_name, support_path)
    mark_processed(state, job_name, build_number, result, output_path.name)
    return output_path.name


def check_job(
    *,
    base_url: str,
    headers: dict[str, str],
    timeout: int,
    state: dict[str, object],
    job_name: str,
    dry_run: bool,
    force: bool,
) -> str:
    api_url = f"{job_url(base_url, job_name)}/lastBuild/api/json?tree=number,building,result,url"
    data = request_json(api_url, headers, timeout)
    build_number = int(data.get("number") or 0)
    if not build_number:
        return f"{job_name}: no builds found"

    if data.get("building") is True:
        return f"{job_name} #{build_number}: still building"

    result = str(data.get("result") or "UNKNOWN")
    build_url = str(data.get("url") or f"{job_url(base_url, job_name)}/{build_number}/")
    if not force and already_processed(state, job_name, build_number):
        return f"{job_name} #{build_number}: {result} already processed"

    if dry_run:
        return f"{job_name} #{build_number}: {result} would inspect consoleText"

    if result != "FAILURE":
        console_url = f"{job_url(base_url, job_name)}/{build_number}/consoleText"
        log_text = request_text(console_url, headers, timeout)
        input_csv = fetch_input_parameter_csv(build_url=build_url, headers=headers, timeout=timeout)
        has_log_evidence = log_has_failure_evidence(log_text)
        has_csv_issues = input_csv_has_validation_issues(job_name, input_csv)
        if not has_log_evidence and not has_csv_issues:
            stored_result = f"{result}_VALID_INPUT" if input_csv is not None else result
            mark_processed(state, job_name, build_number, stored_result)
            return f"{job_name} #{build_number}: {result}, input CSV valid and no failure evidence, skipped"

        app.ensure_dirs()
        log_path = app.INPUT_DIR / safe_log_name(job_name, build_number)
        log_path.write_text(log_text, encoding="utf-8")
        support_path = save_input_csv(job_name, build_number, input_csv)
        output_path = app.process_log(log_path, job_name, support_path)
        stored_result = f"{result}_WITH_INPUT_OR_FAILURE_EVIDENCE"
        mark_processed(state, job_name, build_number, stored_result, output_path.name)
        return f"{job_name} #{build_number}: {stored_result} explained -> {output_path.name}"

    output_name = explain_failed_build(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        state=state,
        job_name=job_name,
        build_number=build_number,
        result=result,
        build_url=build_url,
    )
    return f"{job_name} #{build_number}: FAILURE explained -> {output_name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch Jenkins jobs and explain completed failed builds.")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between checks. Default: 30.")
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not download or explain builds.")
    parser.add_argument("--force", action="store_true", help="Re-check latest build even if state says it was processed.")
    parser.add_argument("--jobs", nargs="*", help="Optional job names. Default: all reference format jobs.")
    parser.add_argument("--timeout", type=int, default=30, help="Jenkins request timeout seconds. Default: 30.")
    parser.add_argument(
        "--base-url",
        default=env_value("JENKINS_BASE_URL", DEFAULT_JENKINS_BASE_URL),
        help="Jenkins folder URL that contains the jobs.",
    )
    return parser.parse_args()


def run_once(args: argparse.Namespace, headers: dict[str, str], state: dict[str, object]) -> None:
    jobs = args.jobs or app.reference_jobs()
    for job_name in jobs:
        try:
            message = check_job(
                base_url=args.base_url,
                headers=headers,
                timeout=args.timeout,
                state=state,
                job_name=job_name,
                dry_run=args.dry_run,
                force=args.force,
            )
        except urllib.error.HTTPError as error:
            message = f"{job_name}: Jenkins HTTP {error.code}"
        except Exception as error:
            message = f"{job_name}: {error}"
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
    save_state(state)


def main() -> int:
    args = parse_args()
    if args.interval < 10:
        print("Use --interval 10 or higher to avoid hitting Jenkins too often.", file=sys.stderr)
        return 2

    headers = auth_header()
    app.ensure_dirs()
    state = load_state()

    while True:
        run_once(args, headers, state)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
