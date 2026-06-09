#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


DEFAULT_JENKINS_BASE_URL = "https://jenkins.prd.valmo.in/job/support/job/log10/job/Regular_tasks"


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def curl_text(url: str, *, user: str, token: str, timeout: int = 45) -> str:
    with tempfile.NamedTemporaryFile("w", delete=False) as config:
        config.write("user = \"" + user.replace('"', '\\"') + ":" + token.replace('"', '\\"') + "\"\n")
        config.write(f"connect-timeout = {timeout}\n")
        config.write(f"max-time = {timeout}\n")
        config_path = Path(config.name)
    try:
        result = subprocess.run(
            ["curl", "--http1.1", "--globoff", "--fail", "--silent", "--show-error", "--config", str(config_path), url],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        config_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"curl failed with exit code {result.returncode}")
    return result.stdout


def local_reference_jobs() -> list[str]:
    root = Path("reference_formats")
    return sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "correct_input.csv").exists())


def jenkins_jobs(base_url: str, user: str, token: str) -> list[str]:
    url = f"{base_url.rstrip('/')}/api/json?tree=jobs[name]"
    data = json.loads(curl_text(url, user=user, token=token))
    return sorted(job["name"] for job in data.get("jobs", []) if "name" in job)


def main() -> int:
    user = env_value("JENKINS_USER")
    token = env_value("JENKINS_API_TOKEN")
    base_url = env_value("JENKINS_BASE_URL", DEFAULT_JENKINS_BASE_URL)
    if not user or not token:
        print("Set JENKINS_USER and JENKINS_API_TOKEN first.")
        return 2

    local_jobs = local_reference_jobs()
    actual_jobs = jenkins_jobs(base_url, user, token)
    actual_set = set(actual_jobs)
    local_set = set(local_jobs)

    print(f"Local website jobs: {len(local_jobs)}")
    print(f"Actual Jenkins jobs under Regular_tasks: {len(actual_jobs)}")
    print()

    mismatched = sorted(local_set - actual_set)
    if not mismatched:
        print("Exact match check: OK. Every website job name exists in Jenkins.")
    else:
        print("Website job names not found exactly in Jenkins:")
        actual_by_lower = {job.lower(): job for job in actual_jobs}
        for job in mismatched:
            similar = actual_by_lower.get(job.lower())
            if similar:
                print(f"- {job}  -> Jenkins has {similar!r} with different case")
            else:
                print(f"- {job}")

    extra = sorted(actual_set - local_set)
    if extra:
        print()
        print("Jenkins jobs not added to this website:")
        for job in extra:
            print(f"- {job}")

    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
