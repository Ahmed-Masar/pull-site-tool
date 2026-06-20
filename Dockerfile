FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends httrack \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pull_site.py app.py ./

ENV SITES_DIR=/app/sites
ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
