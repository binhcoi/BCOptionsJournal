# No third-party dependencies; nothing to install.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
COPY bcoj ./bcoj
COPY tests ./tests

# The journal lives on a volume so the container stays disposable.
ENV BCOJ_DB=/data/journal.db
VOLUME ["/data"]

# Run as a non-root user; the volume needs to be writable by it.
RUN useradd --create-home --uid 1000 bcoj \
    && mkdir -p /data \
    && chown -R bcoj:bcoj /data /app
USER bcoj

# Prove the P/L engine still agrees with the sheet before anything ships.
RUN python -m unittest discover -s tests -q

EXPOSE 8000

# Inside a container the server must bind all interfaces to be reachable;
# publish the port to loopback on the host (see compose.yml).
ENTRYPOINT ["python", "-m", "bcoj.web"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
