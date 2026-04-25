FROM python:3.12-slim

# curl is needed for HEALTHCHECK only
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps as a separate layer so rebuilds after source changes are fast.
# chromadb ships pre-built wheels for linux/amd64 and linux/arm64 — no
# build-essential needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
# messages.py is present in v1.5.0+ (PR #4); safe to glob so this works on
# main too
COPY *.py ./

# Palace directory — mount your palace here at runtime.
# The palace is never baked into the image; it is always external state.
VOLUME ["/palace"]

ENV PALACE_PATH=/palace \
    PALACE_HOST=0.0.0.0 \
    PALACE_PORT=8085

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${PALACE_PORT}/health || exit 1

# --manual bypasses the INVOCATION_ID guard that prevents accidental non-systemd starts.
# The PR branch (pre-v1.5.0) does not have this flag yet; remove it if the build fails.
ENTRYPOINT ["python", "main.py", "--manual"]
