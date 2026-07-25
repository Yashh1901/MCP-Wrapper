# ============================================================
# Dockerfile — MCP Database Wrapper Server
# ============================================================
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install system dependencies (drivers for ODBC / PostgreSQL / MySQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    gnupg2 \
    unixodbc \
    unixodbc-dev \
    libpq-dev \
    libmariadb-dev-compat \
    libmariadb-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency files first (for Docker caching)
COPY pyproject.toml README.md ./
COPY mcp_db_wrapper ./mcp_db_wrapper

# Install the application and all dependencies
RUN pip install --no-cache-dir .

# Expose default HTTP/SSE port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command: Start HTTP transport server on 0.0.0.0:8000
CMD ["mcp-db-wrapper", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
