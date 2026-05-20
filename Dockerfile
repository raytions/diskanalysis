FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Data volume for SQLite DB
VOLUME ["/data"]

ENV DB_PATH=/data/diskanalysis.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host $APP_HOST --port $APP_PORT"]
