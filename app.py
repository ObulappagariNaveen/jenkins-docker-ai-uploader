#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


HOST = "0.0.0.0"
PORT = 8080
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
ARCHIVE_DIR = DATA_DIR / "archive"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def ensure_dirs() -> None:
    for folder in (INPUT_DIR, OUTPUT_DIR, ARCHIVE_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def safe_filename(name: str) -> str:
    name = Path(name).name.strip() or "jenkins-log.txt"
    name = re.sub(r"[^A-Za-z0-9_.#() -]+", "_", name)
    if not name.lower().endswith((".txt", ".log", ".out")):
        name += ".txt"
    return name


def output_name(input_name: str) -> str:
    stem = Path(input_name).stem.replace(" ", "_").replace("(", "").replace(")", "")
    stem = re.sub(r"[^A-Za-z0-9_.#-]+", "_", stem)
    return f"ollama_failure_reason_{stem}.txt"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def status_line(log_text: str) -> str:
    match = re.search(r"Finished:\s+(\w+)", log_text)
    return match.group(1) if match else "Unknown"


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
        "syntaxerror",
        "unicodeerror",
        "unicodedecodeerror",
        "permission denied",
        "no such file",
        "not found",
        "timed out",
        "timeout",
        "finished:",
    )

    selected_indexes: set[int] = set()
    for index, line in enumerate(lines):
        lowered = line.lower()
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


def build_prompt(file_name: str, log_text: str) -> str:
    focused_log_text = relevant_log_context(log_text)

    return f"""You are a Jenkins failure explainer for support engineers.

Read this focused Jenkins console log context and explain the real failure in simple plain text.

Rules:
- Focus on the actual error, not normal setup lines like git checkout or package already installed.
- If the build succeeded, say no failure was detected.
- Do not invent evidence.
- Give a practical possible fix.
- Keep the language simple for a new colleague.
- If there is not enough evidence, say exactly what is missing.

Always use this format:
Job:
Jenkins status:
Where it failed:
Failure type:
Simple reason:
Root cause:
Evidence from log:
Possible fix:
Confidence:

File name:
{file_name}

Detected Jenkins status:
{status_line(log_text)}

Focused Jenkins console log context:
{focused_log_text}
"""


def ask_ollama(file_name: str, log_text: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_prompt(file_name, log_text),
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


def process_log(path: Path) -> Path:
    log_text = read_text(path)
    try:
        explanation = ask_ollama(path.name, log_text)
    except Exception as error:
        explanation = (
            "Could not get Ollama explanation.\n\n"
            f"Reason: {error}\n\n"
            "Check that Ollama is running and the selected model is downloaded."
        )

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output_path = OUTPUT_DIR / output_name(path.name)
    output_path.write_text(
        "\n".join(
            [
                "Jenkins AI Failure Explanation",
                "==============================",
                "",
                f"Source file: {path.name}",
                f"Detected Jenkins status: {status_line(log_text)}",
                f"Created at: {created_at}",
                "",
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
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #171a1f;
      --muted: #5d6673;
      --border: #d9dee7;
      --accent: #166534;
      --accent-soft: #e9f6ee;
      --danger: #9f1239;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    header {{
      border-bottom: 1px solid var(--border);
      background: var(--panel);
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
    main {{ padding: 28px 0 44px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 22px;
      margin-bottom: 18px;
    }}
    .muted {{ color: var(--muted); }}
    label {{ display: block; font-weight: 700; margin-bottom: 10px; }}
    input[type=file] {{
      display: block;
      width: 100%;
      border: 1px dashed #aab3c2;
      border-radius: 8px;
      padding: 18px;
      background: #fbfcfe;
    }}
    button {{
      margin-top: 16px;
      background: var(--accent);
      color: white;
      border: 0;
      border-radius: 7px;
      padding: 11px 16px;
      font-weight: 700;
      cursor: pointer;
    }}
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
        body = """
        <section class="panel">
          <h2>Upload Jenkins Console Log</h2>
          <p class="muted">Upload a Jenkins <code>consoleText</code> file. The original log and AI explanation are stored inside Docker volume storage.</p>
          <form action="/upload" method="post" enctype="multipart/form-data">
            <label for="logfile">Jenkins log file</label>
            <input id="logfile" name="logfile" type="file" accept=".txt,.log,.out" required>
            <button type="submit">Upload And Explain</button>
          </form>
        </section>
        <section class="panel">
          <h2>Storage</h2>
          <p><strong>Input logs:</strong> <code>/data/input</code></p>
          <p><strong>AI output:</strong> <code>/data/output</code></p>
          <p><strong>Archive:</strong> <code>/data/archive</code></p>
        </section>
        """
        self.send_html("Upload", body)

    def results(self) -> None:
        rows = []
        for path in result_files():
            stat = path.stat()
            rows.append(
                "<tr>"
                f"<td><a href=\"/result/{quote(path.name)}\">{html.escape(path.name)}</a></td>"
                f"<td>{datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}</td>"
                f"<td>{stat.st_size} bytes</td>"
                "</tr>"
            )
        table = (
            "<table><thead><tr><th>Output File</th><th>Created</th><th>Size</th></tr></thead>"
            f"<tbody>{''.join(rows) or '<tr><td colspan=\"3\" class=\"muted\">No outputs yet.</td></tr>'}</tbody></table>"
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
        for candidate in message.iter_parts():
            if candidate.get_param("name", header="content-disposition") == "logfile":
                part = candidate
                break

        if part is None:
            self.send_html("Upload error", '<div class="panel error">No file field found.</div>', HTTPStatus.BAD_REQUEST)
            return

        filename = safe_filename(part.get_filename() or "jenkins-log.txt")
        input_path = INPUT_DIR / filename
        if input_path.exists():
            input_path = INPUT_DIR / f"{int(time.time())}-{filename}"
        input_path.write_bytes(part.get_payload(decode=True) or b"")

        output_path = process_log(input_path)
        body_html = (
            '<section class="panel ok">'
            "<h2>Explanation Created</h2>"
            f"<p><strong>Input:</strong> {html.escape(input_path.name)}</p>"
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
