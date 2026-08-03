FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLAUDE_SEO_WEB_DATA=/data \
    CLAUDE_SEO_WEB_AUTO_WORKER=0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY web/requirements.txt /tmp/web-requirements.txt
COPY requirements.txt /tmp/claude-seo-requirements.txt
RUN pip install --no-cache-dir -r /tmp/web-requirements.txt

COPY . /app

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "web.backend:app", "--host", "0.0.0.0", "--port", "8001"]
