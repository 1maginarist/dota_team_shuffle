FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# База SQLite лежит здесь — монтируется наружу, чтобы переживать пересборки
VOLUME /app/data

CMD ["python", "-m", "bot"]
