# The whole service in one image: customer portal, admin panel with the scheduler, and the Chrome the bot uses
# to write PDF resumes. docker-compose.yml runs it twice (customer site, admin site) on one shared data volume.
# Build from the jobbot folder:   docker build -f deploy/Dockerfile -t jobbot .
# Not yet built on a real server: Docker is not installed on the PC this was written on.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chrome

COPY . .

# Everything that must be kept (the database, each customer's files, backups, keys) goes to /data,
# which docker-compose.yml puts on a named volume.
ENV JOBBOT_DATA=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    JOBBOT_TRUSTED_PROXY=*
RUN mkdir -p /data && chown -R pwuser:pwuser /data /app
USER pwuser
VOLUME /data
EXPOSE 8765 8766

# 0.0.0.0 inside the container only: docker-compose.yml publishes nothing but the HTTPS proxy.
CMD ["python", "-m", "jobbot", "serve", "--host", "0.0.0.0", "--site", "customer", "--port", "8765"]
