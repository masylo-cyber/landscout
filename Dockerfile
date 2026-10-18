FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Warsaw

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgeos-c1v5 libproj25 tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY landscout ./landscout
RUN pip install -e .

COPY config ./config

CMD ["landscout", "serve"]
