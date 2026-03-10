FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Railway sets PORT env var, but we default to 8004
ENV CHIRAN_PORT=8004

EXPOSE 8004

# Health check for Railway
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8004/health')" || exit 1

CMD ["python", "main.py"]
