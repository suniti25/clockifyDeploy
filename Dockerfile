# ------ Builder stage: prepare wheels (no compilers in final image) ------ 
FROM python:3.12-slim AS builder

WORKDIR /app

# Minimal build deps (for psycopg2 )
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ------ Final stage: runtime only ------

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime lib only (for psycopg2-binary)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    python3-pip \
    postgresql-client \
    && pip install --no-cache-dir setuptools wheel \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

COPY . .

# Create non-root user + fix permissions
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/staticfiles \
    && chown -R appuser:appuser /app

RUN usermod -aG www-data appuser

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]

EXPOSE 8000

# CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "leave_management_system.wsgi:application"]