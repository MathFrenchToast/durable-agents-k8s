FROM python:3.11-slim

WORKDIR /app

# Install curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application layers
COPY core /app/core
COPY gateway /app/gateway
COPY catalog /app/catalog

ENV PYTHONPATH=/app
ENV CATALOG_PATH=/app/catalog
ENV PORT=8000

EXPOSE 8000

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
