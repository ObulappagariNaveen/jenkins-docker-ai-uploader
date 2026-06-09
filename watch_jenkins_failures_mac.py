#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from html import unescape
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin

import app


DEFAULT_JENKINS_BASE_URL = "https://jenkins.prd.valmo.in/job/support/job/log10/job/Regular_tasks"
DEFAULT_CONTAINER = "jenkins-ai-web-local-ollama"
STATE_PATH = Path("jenkins-watcher-mac-state.json")


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def require_credentials() -> tuple[str, str]:
    user = env_value("JENKINS_USER")
    token = env_value("JENKINS_API_TOKEN")
    if not user or not token:
        raise RuntimeError("Set JENKINS_USER and JENKINS_API_TOKEN before starting the watcher.")
    return user, token


def curl_text(url: str, *, user: str, token: str, timeout: int) -> str:
    with tempfile.NamedTemporaryFile("w", delete=False) as config:
        config.write("user = \"" + user.replace('"', '\\"') + ":" + token.replace('"', '\\"') + "\"\n")
        config.write(f"connect-timeout = {timeout}\n")
        config.write(f"max-time = {timeout}\n")
        config_path = Path(config.name)
    try:
        result = subprocess.run(
            ["curl", "--http1.1", "--fail", "--silent", "--show-error", "--config", str(config_path), url],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"curl failed with exit code {result.returncode}")
    return result.stdout


def docker_run(args: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"docker failed with exit code {result.returncode}")
    return result.stdout.strip()


def reference_jobs() -> list[str]:
    root = Path("reference_formats")
    return sorted(
        path.name for path in root.iterdir() if path.is_dir() and (path / "correct_input.csv").exists()
    )


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
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def already_processed(state: dict[str, object], job_name: str, build_number: int) -> bool:
    processed = state.get("processed", {})
    return isinstance(processed, dict) and f"{job_name}#{build_number}" in processed


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
    user: str,
    token: str,
    timeout: int,
) -> tuple[str, str] | None:
    parameters_url = f"{build_url.rstrip('/')}/parameters/"
    parameters_html = curl_text(parameters_url, user=user, token=token, timeout=timeout)
    links = input_parameter_links(parameters_html, parameters_url)
    if not links:
        return None
    file_name, download_url = links[0]
    return file_name, curl_text(download_url, user=user, token=token, timeout=timeout)


def input_csv_has_validation_issues(job_name: str, input_csv: tuple[str, str] | None) -> bool:
    if input_csv is None:
        return False

    _, csv_text = input_csv
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as handle:
            handle.write(csv_text)
            temp_path = Path(handle.name)
        report = app.validate_csv_against_reference(job_name, temp_path)
        return report.has_issues
    except Exception:
        # If local validation cannot read the file, let the normal explanation path show the real error.
        return True
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def create_explanation_in_container(
    container: str,
    job_name: str,
    build_number: int,
    log_text: str,
    input_csv: tuple[str, str] | None = None,
) -> str:
    log_name = safe_log_name(job_name, build_number)
    docker_path = f"/data/input/{log_name}"
    docker_run(["exec", "-i", container, "sh", "-c", f"cat > {docker_path}"], input_text=log_text)

    support_path = None
    if input_csv is not None:
        file_name, csv_text = input_csv
        support_name = safe_support_name(job_name, build_number, file_name)
        support_path = f"/data/support/{support_name}"
        docker_run(["exec", "-i", container, "sh", "-c", f"cat > {support_path}"], input_text=csv_text)

    support_arg = "None" if support_path is None else f"Path({support_path!r})"
    command = (
        "import app; "
        "from pathlib import Path; "
        "app.ensure_dirs(); "
        f"print(app.process_log(Path({docker_path!r}), {job_name!r}, {support_arg}).name)"
    )
    return docker_run(["exec", container, "python", "-c", command])


def check_job(
    *,
    base_url: str,
    user: str,
    token: str,
    timeout: int,
    state: dict[str, object],
    job_name: str,
    container: str,
    dry_run: bool,
    force: bool,
) -> str:
    api_url = f"{job_url(base_url, job_name)}/lastBuild/api/json"
    data = json.loads(curl_text(api_url, user=user, token=token, timeout=timeout))
    build_number = int(data.get("number") or 0)
    if not build_number:
        return f"{job_name}: no builds found"

    if data.get("building") is True:
        return f"{job_name} #{build_number}: still building"

    result = str(data.get("result") or "UNKNOWN")
    build_url = str(data.get("url") or f"{job_url(base_url, job_name)}/{build_number}/")
    if not force and already_processed(state, job_name, build_number):
        return f"{job_name} #{build_number}: {result} already processed"

    console_url = f"{job_url(base_url, job_name)}/{build_number}/consoleText"
    log_text = curl_text(console_url, user=user, token=token, timeout=timeout)
    input_csv = fetch_input_parameter_csv(build_url=build_url, user=user, token=token, timeout=timeout)
    has_log_evidence = log_has_failure_evidence(log_text)
    has_csv_issues = input_csv_has_validation_issues(job_name, input_csv)

    if result != "FAILURE" and not has_log_evidence and not has_csv_issues:
        stored_result = f"{result}_VALID_INPUT" if input_csv is not None else result
        mark_processed(state, job_name, build_number, stored_result)
        return f"{job_name} #{build_number}: {result}, input CSV valid and no failure evidence, skipped"

    if dry_run:
        if result == "FAILURE":
            reason = "FAILURE"
        elif has_csv_issues:
            reason = f"{result} with CSV validation issue"
        else:
            reason = f"{result} with failure evidence"
        input_message = f", input CSV: {input_csv[0]}" if input_csv is not None else ""
        return f"{job_name} #{build_number}: {reason} would create explanation{input_message}"

    output_name = create_explanation_in_container(container, job_name, build_number, log_text, input_csv)
    stored_result = result if result == "FAILURE" else f"{result}_WITH_INPUT_OR_FAILURE_EVIDENCE"
    mark_processed(state, job_name, build_number, stored_result, output_name)
    return f"{job_name} #{build_number}: {stored_result} explained -> {output_name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mac-side Jenkins watcher using curl --http1.1.")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between checks. Default: 30.")
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not create explanations.")
    parser.add_argument("--force", action="store_true", help="Re-check latest build even if state says it was processed.")
    parser.add_argument("--jobs", nargs="*", help="Optional job names. Default: all reference format jobs.")
    parser.add_argument("--timeout", type=int, default=45, help="Jenkins request timeout seconds. Default: 45.")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="Docker container name.")
    parser.add_argument(
        "--base-url",
        default=env_value("JENKINS_BASE_URL", DEFAULT_JENKINS_BASE_URL),
        help="Jenkins folder URL that contains the jobs.",
    )
    return parser.parse_args()


def run_once(args: argparse.Namespace, user: str, token: str, state: dict[str, object]) -> None:
    jobs = args.jobs or reference_jobs()
    for job_name in jobs:
        try:
            message = check_job(
                base_url=args.base_url,
                user=user,
                token=token,
                timeout=args.timeout,
                state=state,
                job_name=job_name,
                container=args.container,
                dry_run=args.dry_run,
                force=args.force,
            )
        except Exception as error:
            message = f"{job_name}: {error}"
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)
    save_state(state)


def main() -> int:
    args = parse_args()
    if args.interval < 10:
        print("Use --interval 10 or higher to avoid hitting Jenkins too often.", file=sys.stderr)
        return 2

    user, token = require_credentials()
    state = load_state()
    while True:
        run_once(args, user, token, state)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
