FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for some optional parsers (unstructured / psycopg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Pre-build the sample analytical DB so the service is demoable out-of-the-box
RUN python scripts/generate_sample.py

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
