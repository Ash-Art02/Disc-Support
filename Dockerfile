FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .env.example ./
# Cloud hosts inject DISCORD_TOKEN + PORT as env vars - do NOT bake .env into image
CMD ["python", "bot.py"]
