FROM python:3.12-slim

WORKDIR /app

RUN useradd --system --uid 10001 --no-create-home app

COPY app.py watch_jenkins_failures.py docker-entrypoint.sh ./
COPY reference_formats ./reference_formats

RUN chmod 755 /app/docker-entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "app.py"]
