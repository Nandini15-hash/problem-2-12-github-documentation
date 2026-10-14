# Webhook / MCP service image. Runtime needs only the Python stdlib + git (+ the claude CLI for live reasoning).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm \
 && npm install -g @anthropic-ai/claude-code && rm -rf /var/lib/apt/lists/*
RUN useradd -m docsync
WORKDIR /app
COPY src/ src/
COPY cassettes/ cassettes/
USER docsync
# secrets via env only: CLAUDE_KEY, DOCSYNC_WEBHOOK_SECRET, GITHUB_TOKEN
EXPOSE 8787
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz')"
CMD ["python", "-m", "src.docsync.cli", "serve", "--repo", "/repos/target", "--port", "8787"]
