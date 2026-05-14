Jenkins Docker AI Uploader
==========================

Purpose
-------
This is a Docker-only website for Jenkins log explanation.

You upload a downloaded Jenkins consoleText log in the browser.
Docker stores the raw log.
Ollama explains the failure.
Docker stores the plain-English output.


What It Uses
------------
Container 1:
  ollama

Container 2:
  web upload app

Docker volumes:
  ollama-models       -> stores Ollama model files
  jenkins-ai-data     -> stores uploaded logs, outputs, and archive


Storage Inside Docker
---------------------
Uploaded logs:
  /data/input

AI explanations:
  /data/output

Archived raw logs:
  /data/archive


Start
-----
cd /Users/obulappagari.naveen/Documents/Codex/2026-05-10/files-mentioned-by-the-user-jenkins/jenkins-docker-ai-uploader

docker compose up -d --build


Download Ollama Model
---------------------
Run this once after starting Docker:

docker exec jenkins-ai-ollama ollama pull llama3.2:3b


Open Website
------------
http://localhost:8080


Daily Use
---------
1. Open http://localhost:8080
2. Upload downloaded Jenkins consoleText .txt/.log/.out file
3. Read the plain-English output in browser
4. Output is stored in Docker volume under /data/output


Check Containers
----------------
docker compose ps


View Web Logs
-------------
docker compose logs -f web


View Ollama Logs
----------------
docker compose logs -f ollama


Check Stored Files Inside Docker
--------------------------------
docker exec jenkins-ai-web find /data -maxdepth 2 -type f -print


Copy All Docker-Stored Data To Mac
----------------------------------
mkdir -p /Users/obulappagari.naveen/Documents/Jenkins-ai-docker-export

docker cp jenkins-ai-web:/data/. /Users/obulappagari.naveen/Documents/Jenkins-ai-docker-export/


Stop
----
docker compose down


Important
---------
docker compose down does not delete stored data.

Do not run this unless you intentionally want to delete stored Docker volume data:

docker compose down -v

