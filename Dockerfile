# syntax=docker/dockerfile:1
# Playwright's official image: Ubuntu 24.04, Python 3.12, Chromium preinstalled.
# The tag must match the playwright version pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    STM_DATA_DIR=/data \
    STM_DOWNLOAD_DIR=/downloads \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    HOME=/tmp/home

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --break-system-packages -r requirements.txt

COPY main.py pyproject.toml ./
COPY soundtrack_manager ./soundtrack_manager
COPY examples ./examples

# Persistent locations. Mount host directories here (see compose.yaml).
RUN mkdir -p /data /downloads && chown pwuser:pwuser /data /downloads
VOLUME ["/data", "/downloads"]

# Non-root user that ships with the Playwright image (uid/gid 1000).
# compose.yaml can override it with PUID/PGID (e.g. 99:100 on Unraid).
USER pwuser

EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python3", "main.py"]
CMD ["serve"]
