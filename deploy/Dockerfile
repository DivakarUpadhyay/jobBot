# The whole service in one image: API, admin page, scheduler, and the Chrome the bot uses to
# write PDF resumes. NOT YET BUILT OR RUN: Docker is not installed on the PC this was written on.
# Build from the jobbot folder:   docker build -f deploy/Dockerfile -t jobbot .
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chrome

COPY . .

# Everything that must be kept (control database, each customer's database and files, keys)
# goes to /data, which docker-compose.yml puts on a named volume.
ENV JOBBOT_DATA=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8
RUN mkdir -p /data && chown -R pwuser:pwuser /data /app
USER pwuser
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)"

# 0.0.0.0 inside the container only: docker-compose.yml publishes nothing but the HTTPS proxy.
CMD ["python", "-m", "jobbot", "serve", "--host", "0.0.0.0", "--port", "8765"]
