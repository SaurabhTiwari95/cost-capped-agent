# --- build stage: install dependencies only -------------------------------------------------
FROM python:3.15-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- runtime stage: small image, non-root user ------------------------------------------------
FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_DB_PATH=/data/agent.db
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir /data && chown app /data
WORKDIR /srv
COPY --from=build /install /usr/local
COPY costagent ./costagent
COPY sample_corpus ./sample_corpus
USER app
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "costagent"]
CMD ["--help"]
