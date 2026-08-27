FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN addgroup --system analytics-mcp \
    && adduser --system --ingroup analytics-mcp analytics-mcp

COPY pyproject.toml README.md LICENSE ./
COPY analytics_mcp ./analytics_mcp
RUN pip install --no-cache-dir .

USER analytics-mcp

EXPOSE 8080

CMD ["analytics-mcp-http", "--host", "0.0.0.0"]
