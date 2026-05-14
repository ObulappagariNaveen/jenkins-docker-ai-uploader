# Jenkins Log AI Explainer Website

This repository contains a small Docker website for explaining Jenkins failure logs in simple plain text.

The current recommended setup is:

- Docker runs the website.
- Docker stores uploaded Jenkins logs and generated output files.
- The Ollama app on the Mac runs the AI model.
- Jenkins itself is not changed, updated, or touched.

## What The Website Does

1. Open the website.
2. Upload a downloaded Jenkins `consoleText` log file, such as `.txt`, `.log`, or `.out`.
3. The website stores the raw log inside Docker.
4. The website sends the important error lines to Ollama.
5. Ollama returns a plain-English failure explanation.
6. The explanation is saved inside Docker and shown in the browser.

## Files In This Repository

- `app.py` - Python website and upload logic.
- `Dockerfile` - Builds the website container.
- `docker-compose.local-ollama.yml` - Recommended setup: Docker website plus local Mac Ollama.
- `docker-compose.yml` - Full Docker setup with Ollama inside Docker. This exists, but on Mac it may be slower.
- `Modelfile.jenkins-failure-explainer` - Custom Ollama model instructions.
- `README_LOCAL_OLLAMA.txt` - Short runbook for the recommended setup.
- `NEW_CHAT_HANDOFF.txt` - Text you can paste into a new Codex or ChatGPT chat.

## Required Software

Install these before running:

- Docker Desktop
- Ollama app

## Create The Ollama Model

Open Terminal from this repository folder:

```bash
cd /Users/obulappagari.naveen/Documents/Codex/2026-05-10/files-mentioned-by-the-user-jenkins/jenkins-docker-ai-uploader
```

Pull the base model:

```bash
ollama pull llama3.2:3b
```

Create the Jenkins explainer model:

```bash
ollama create jenkins-failure-explainer -f Modelfile.jenkins-failure-explainer
```

Check it:

```bash
ollama list
```

You should see:

```text
jenkins-failure-explainer
```

## Start The Website

```bash
cd /Users/obulappagari.naveen/Documents/Codex/2026-05-10/files-mentioned-by-the-user-jenkins/jenkins-docker-ai-uploader
docker compose -f docker-compose.local-ollama.yml up -d --no-build
```

If the image does not exist yet, run:

```bash
docker compose -f docker-compose.local-ollama.yml up -d --build
```

Open:

```text
http://localhost:8081
```

## Daily Use

1. Open a failed Jenkins build.
2. Add `/consoleText` at the end of the build URL.
3. Save the page as a `.txt` file.
4. Open `http://localhost:8081`.
5. Upload the saved Jenkins log file.
6. Open the generated result from the website.

## Docker Storage

Inside the website container, files are stored here:

```text
/data/input
/data/output
/data/archive
```

Check stored files:

```bash
docker exec jenkins-ai-web-local-ollama find /data -maxdepth 2 -type f -print
```

Copy all stored files from Docker to the Mac:

```bash
mkdir -p /Users/obulappagari.naveen/Documents/Jenkins-ai-docker-local-ollama-export
docker cp jenkins-ai-web-local-ollama:/data/. /Users/obulappagari.naveen/Documents/Jenkins-ai-docker-local-ollama-export/
```

## Stop The Website

```bash
docker compose -f docker-compose.local-ollama.yml down
```

This does not delete Docker volume data.

Do not run this unless you intentionally want to delete the uploaded logs and outputs:

```bash
docker compose -f docker-compose.local-ollama.yml down -v
```

## Current Verified Test

This was tested with Jenkins log `#46.txt`.

The system correctly detected the failure as:

```text
CSV/input file encoding issue
```

The evidence was:

```text
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xca
```

## Important Notes

- This website does not change Jenkins.
- This website does not need Jenkins admin access.
- This website does not automatically fetch from Jenkins yet.
- Current flow needs a downloaded Jenkins `consoleText` file.
- The local Mac Ollama setup is recommended because Docker-only Ollama was slower and timed out during testing.
