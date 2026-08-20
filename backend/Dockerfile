# Sextant backend — tina4-python.
#
# No database tooling and no system libraries. This service talks to MongoDB
# over the wire with pymongo; it does not shell out to mongodump, so there is no
# apt-get layer here.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY src/ ./src/

# The audit log must be writable or the app refuses to start — see _preflight in
# app.py. Mount a volume here in anything that is not a demo: a container
# filesystem is exactly as durable as the container, and an audit trail that
# disappears with the pod is not an audit trail.
RUN useradd --system --uid 10001 --create-home sextant \
 && mkdir -p /data /config \
 && chown -R sextant:sextant /data /app

# Tina4 3.13 exits 2 at boot without these, with no message that says why.
ENV TINA4_ALLOW_LEGACY_ENV=true
ENV TINA4_OVERRIDE_CLIENT=true
ENV SEXTANT_CONFIG=/config/sextant.yml
ENV PORT=7145

EXPOSE 7145

# /ready asserts an EFFECT — that the config parses and the audit log is
# WRITABLE — not merely that the process is answering. A console that cannot
# record a change must not take traffic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7145/ready').status==200 else 1)"

USER sextant

CMD ["python", "app.py"]
