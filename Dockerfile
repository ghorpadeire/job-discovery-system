FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2 + Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY . .

EXPOSE 5000

CMD ["gunicorn", "dashboard:app", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "120"]
