FROM python:3.11-slim

WORKDIR /app

# Install curl for auto-registration health checks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY runtimes/restate/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy core domain, services, and runtime layers
COPY core /app/core
COPY services /app/services
COPY runtimes /app/runtimes

ENV PYTHONPATH=/app
ENV PORT=9080

RUN chmod +x /app/runtimes/restate/entrypoint.sh

EXPOSE 9080

ENTRYPOINT ["/bin/sh", "/app/runtimes/restate/entrypoint.sh"]
