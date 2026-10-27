# Jenkins Log AI Explainer Website

A small Docker website that explains Jenkins job failures in simple plain English.

- Docker runs the website and stores uploaded logs, input CSVs and generated explanations.
- Ollama runs the AI model. The recommended setup uses the Ollama app on the Mac.
- Jenkins itself is not changed.

For how the CSV checks and failure rules work, see `CURRENT_WORKFLOW.md`.

## What The Website Does

1. Pick the Jenkins job from the dropdown.
2. Upload the failed Jenkins `consoleText` log (`.txt`, `.log`, `.out`), the input CSV, or both.
3. The website checks the CSV against `reference_formats/<job>/correct_input.csv`.
4. Known failures are explained by built-in rules. Anything else is sent to Ollama.
5. The explanation is saved inside Docker and shown in the browser.

## Files In This Repository

- `app.py` - Python website, CSV checks and Ollama logic.
- `reference_formats/` - The correct CSV format for each supported Jenkins job.
- `watch_jenkins_failures.py` - Fetches failed builds from Jenkins and explains them (used by the Jenkins pipeline).
- `watch_jenkins_failures_mac.py` - The same watcher for running on the Mac against the Docker website.
- `jenkins_ai_failure_explainer_pipeline.groovy` - Jenkins pipeline that runs the watcher for the upstream job.
- `check_jenkins_job_names.py` - Checks that the reference job names exist in Jenkins.
- `Dockerfile`, `docker-entrypoint.sh` - Builds the website image.
- `docker-compose.local-ollama.yml` - Recommended: Docker website plus the Mac's Ollama app.
- `docker-compose.yml` - Website and Ollama both in Docker. Works, but slower on a Mac.
- `Modelfile.jenkins-failure-explainer` - Custom Ollama model instructions.
- `NEW_CHAT_HANDOFF.txt` - Text to paste into a new AI chat about this project.

## Required Software

- Docker Desktop
- Ollama app (for the recommended setup)

## Recommended Setup: Local Mac Ollama

Create the model once:

```bash
cd ~/jenkins-docker-ai-uploader
ollama pull llama3.2:3b
ollama create jenkins-failure-explainer -f Modelfile.jenkins-failure-explainer
ollama list
```

Start the website. The code and reference formats are built into the image, so use `--build` after any change:

```bash
docker compose -f docker-compose.local-ollama.yml up -d --build
```

Open `http://localhost:8081`. Colleagues on the same network can use `http://YOUR-MAC-IP:8081`.

## Docker-Only Setup

```bash
docker compose up -d --build
docker exec jenkins-ai-ollama ollama pull llama3.2:3b
```

Open `http://localhost:8080`.

## Settings

Set these in your shell or in a `.env` file next to the compose file before `docker compose up`.

| Setting | Default | What it does |
|---|---|---|
| `APP_ACCESS_TOKEN` | empty (off) | When set, the browser asks for a login. Any username works; the password is this value. |
| `RETENTION_DAYS` | `0` (off) | When above 0, uploaded logs, CSVs and outputs older than this many days are deleted every hour. |
| `OLLAMA_MODEL` | `jenkins-failure-explainer` (local) / `llama3.2:3b` (Docker-only) | Model used for explanations. |
| `MAX_CONCURRENT_OLLAMA` | `2` | How many uploads can ask Ollama at the same time. Others wait up to `OLLAMA_QUEUE_WAIT_SECONDS` (60). |
| `REQUEST_TIMEOUT_SECONDS` | `60` | Drops a browser connection that stops sending data. |

Example `.env`:

```text
APP_ACCESS_TOKEN=choose-a-shared-password
RETENTION_DAYS=30
```

## Docker Storage

Inside the website container:

```text
/data/input     uploaded Jenkins logs
/data/support   uploaded input CSVs
/data/output    AI explanations
/data/archive   archived copies of logs
```

List stored files:

```bash
docker exec jenkins-ai-web-local-ollama find /data -maxdepth 2 -type f -print
```

Copy everything to the Mac:

```bash
mkdir -p ~/Documents/Jenkins-ai-docker-export
docker cp jenkins-ai-web-local-ollama:/data/. ~/Documents/Jenkins-ai-docker-export/
```

## Stop The Website

```bash
docker compose -f docker-compose.local-ollama.yml down
```

This keeps the stored data. `down -v` deletes the Docker volume with all uploaded logs and outputs, so only run it on purpose.

## Health Check

The container reports healthy when `http://localhost:8081/healthz` answers `ok`. This page never needs a login.

```bash
docker ps --format '{{.Names}} {{.Status}}'
```

## Notes

- This website does not change Jenkins and does not need Jenkins admin access.
- The local Mac Ollama setup is recommended because Docker-only Ollama was slower and timed out during testing.
- The website runs as a non-root user inside the container. On first start it takes ownership of an older `/data` volume.
