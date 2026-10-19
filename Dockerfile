# syntax=docker/dockerfile:1.6
# Targets:
#   app     API, scheduler, Telegram bot (Tectonic + Typst for resumes)
#   worker  app + Playwright Chromium for the application engine
#   test    worker + pytest
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/opt/cache PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
# Tectonic and Typst are static musl builds: no shared libraries needed.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /srv jobbot && mkdir -p /opt/cache /srv && chown jobbot /opt/cache /srv
ARG TECTONIC_VERSION=0.15.0
ARG TYPST_VERSION=0.13.1
RUN curl -fsSL "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
        | tar xz -C /usr/local/bin tectonic \
    && curl -fsSL "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-x86_64-unknown-linux-musl.tar.xz" \
        | tar xJ --strip-components=1 -C /usr/local/bin typst-x86_64-unknown-linux-musl/typst \
    && tectonic --version && typst --version
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=jobbot . .
USER jobbot
# Pre-warm Tectonic's TeX bundle so resume compiles never need the network at
# run time, and fail the build now if the template doesn't compile.
RUN RESUME_ENGINE=tectonic DATABASE_URL=postgresql+psycopg://x/x ENCRYPTION_KEY=build \
    python -m app.resume.cli --vault examples/vault.example.json --jd examples/jd.example.txt \
        --title "Software Development Engineer I" --company "Northwind" --out /tmp/warm.pdf \
    && rm -f /tmp/warm.pdf
EXPOSE 8000
CMD ["python", "-m", "app.cli", "api"]

FROM app AS worker
USER root
RUN python -m playwright install --with-deps chromium && chmod -R a+rX /opt/pw-browsers
USER jobbot
CMD ["python", "-m", "app.cli", "apply-worker"]

FROM worker AS test
USER root
RUN pip install pytest
USER jobbot
CMD ["pytest", "-q"]
