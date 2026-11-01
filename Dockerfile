FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY carousel ./carousel
COPY fonts ./fonts
COPY workspace/settings.example.json ./workspace/settings.example.json
ENV PYTHONUNBUFFERED=1
# Данные ученика (/app/workspace) подключаются из Cloud Storage при установке.
CMD ["python", "-m", "carousel.server"]
