Jenkins Docker Website With Local Ollama
=======================================

Purpose
-------
This option keeps the website and Jenkins log storage inside Docker.
It uses the Ollama app already running on your Mac for the AI explanation.

This is usually faster on a Mac than running Ollama fully inside Docker.


What Runs Where
---------------
Docker:
  website
  uploaded logs
  generated output files
  archived raw logs

Mac:
  Ollama app
  jenkins-failure-explainer model


Start Ollama On Mac
-------------------
Open the Ollama app.

Check model from Terminal:

ollama list

You should see:

jenkins-failure-explainer


Start Website
-------------
cd /Users/obulappagari.naveen/Documents/Codex/2026-05-10/files-mentioned-by-the-user-jenkins/jenkins-docker-ai-uploader

docker compose -f docker-compose.local-ollama.yml up -d --build


Open Website
------------
http://localhost:8081


Daily Use
---------
1. Download Jenkins consoleText log as .txt.
2. Open http://localhost:8081.
3. Upload the log.
4. Click the generated output link.
5. The explanation is stored inside Docker at /data/output.


Check Stored Files Inside Docker
--------------------------------
docker exec jenkins-ai-web-local-ollama find /data -maxdepth 2 -type f -print


Copy Docker Data To Mac
-----------------------
mkdir -p /Users/obulappagari.naveen/Documents/Jenkins-ai-docker-local-ollama-export

docker cp jenkins-ai-web-local-ollama:/data/. /Users/obulappagari.naveen/Documents/Jenkins-ai-docker-local-ollama-export/


Stop
----
docker compose -f docker-compose.local-ollama.yml down


Important
---------
This does not change or delete the old local-folder scripts.
This does not change or delete the old Docker-only setup.

Do not run this unless you intentionally want to delete this website data:

docker compose -f docker-compose.local-ollama.yml down -v
