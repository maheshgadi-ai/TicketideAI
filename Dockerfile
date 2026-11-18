FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY evals/datasets ./evals/datasets
RUN pip install --no-cache-dir .

# The sandbox refuses to run as root, because Linux exempts root from its process limit.
RUN useradd --create-home --uid 10001 ticketide \
    && mkdir -p /app/data \
    && chown -R ticketide:ticketide /app/data
USER ticketide

EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request as r; r.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"]

# The container listens on its own network; docker-compose.yml publishes it to localhost only.
CMD ["ticketide", "app", "--host", "0.0.0.0"]
