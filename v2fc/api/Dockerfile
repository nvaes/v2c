# Orchestrateur V2FC — API FastAPI. Ne contient pas le VDDK (pas nécessaire
# ici : c'est le Data Mover qui le charge, pas l'API), donc pas de contrainte
# --platform linux/amd64 particulière au-delà de la cohérence avec le reste
# de la stack.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/work

WORKDIR /work
COPY api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY datamover/ /work/datamover/
COPY relay/     /work/relay/
COPY api/       /work/api/

EXPOSE 8000
CMD ["/bin/bash", "-c", "cd /work/api && alembic upgrade head && cd /work && uvicorn api.main:app --host 0.0.0.0 --port 8000"]
