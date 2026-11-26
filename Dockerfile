FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken \
    MODELMUX_DB_PATH=/data/modelmux.db \
    PORT=8000

WORKDIR /app

# Install CPU-only PyTorch first so pip does not pull ~3 GB of CUDA libraries.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-bake tokenizer and MiniLM weights into the image for fast, offline-safe startup.
RUN mkdir -p /app/.cache/huggingface /app/.cache/tiktoken /data \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY app/ ./app/
COPY dashboard/ ./dashboard/
COPY eval/labelled.json ./eval/labelled.json
COPY config.yaml .

# Writable paths for non-root container platforms (e.g. Hugging Face Spaces UID 1000).
RUN chmod -R 777 /data /app/.cache

EXPOSE 8000

# Single worker: RateLimiter, CircuitBreakers, and SQLite WAL live in-process.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
