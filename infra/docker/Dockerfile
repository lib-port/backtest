FROM python:3.11-slim

# Create non-root user
RUN useradd -m -u 10001 appuser

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY data /app/data

RUN pip install -U pip && pip install -e ".[dev,postgres]"

USER 10001:10001

ENTRYPOINT ["python", "-m", "quant_backtester.cli"]
