# AR Collections Nova Sonic 2.5 Agent container image.
#
# Pinned to Python 3.12 (NOT 3.13): the audio bridge uses the stdlib `audioop`
# module (mu-law <-> PCM transcode), which was removed in 3.13. The aws-sdk-
# bedrock-runtime / strands bidi stack is also validated on 3.12.
FROM python:3.12-slim

# No .pyc files, unbuffered stdout so JSON-line logs reach CloudWatch promptly.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_PORT=8080

WORKDIR /app

# Install dependencies first (better layer caching). Copy only the project
# metadata, install the package, then copy the source.
COPY pyproject.toml README.md ./
COPY app ./app
COPY web ./web

RUN python -m pip install --upgrade pip \
    && python -m pip install .

# scripts/ (operator CLI) is copied after install so changes don't bust the
# dependency layer. web/ is copied above so GET / and /static/* serve the
# dashboard inside the container.
COPY scripts ./scripts

# Drop root for runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# uvicorn serves REST + the /media WebSocket. 0.0.0.0 so the ALB can reach it.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
