FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--workers", "1"]
