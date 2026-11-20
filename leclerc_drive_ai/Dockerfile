ARG BUILD_FROM=python:3.12-slim-bookworm
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_NODEJS_PATH=/usr/bin/node

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    nodejs \
    ca-certificates \
    fonts-liberation \
    fonts-freefont-ttf \
    curl \
    jq \
    zstd \
    procps \
    pciutils \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Installation officielle d'Ollama (détection auto amd64/arm64)
RUN curl -fsSL https://ollama.com/install.sh | sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod a+x run.sh

EXPOSE 8099

CMD [ "./run.sh" ]
