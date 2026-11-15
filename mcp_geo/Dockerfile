# Plain Python base. Pinned: an unpinned mcp install pulls 2.x (FastMCP renamed).
FROM python:3.13-slim

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8790 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN pip install "mcp==1.30.0" "httpx==0.28.1"
COPY *.py /app/

EXPOSE 8790
CMD ["python", "server.py"]
