# AR Collections Nova Sonic 2.5 Agent container image.
#
# MULTI-STAGE build:
#   stage 1 (web-build) - node image that compiles the React/Vite/shadcn
#                         dashboard in frontend/ into a static bundle. Vite is
#                         configured (base: "/static/") to emit the bundle into
#                         web/ (index.html + assets/) so FastAPI serves it with
#                         no backend change.
#   stage 2 (runtime)   - python:3.12-slim that installs the app and copies the
#                         built web/ bundle from stage 1.
#
# npm runs at IMAGE-BUILD time (CodeBuild has network); the local sandbox never
# needs node and local `docker build` is not used.

# ---- stage 1: build the frontend bundle ------------------------------------
FROM node:22-slim AS web-build
WORKDIR /build

# Install deps first (better layer caching) using the committed lockfile.
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci

# Copy the frontend source and build. vite.config.ts outputs to ../web, so the
# emitted bundle lands at /build/web (index.html + assets/).
COPY frontend ./frontend
RUN cd frontend && npm run build

# ---- stage 2: python runtime -----------------------------------------------
# Pinned to Python 3.12 (NOT 3.13): the audio bridge uses the stdlib `audioop`
# module (mu-law <-> PCM transcode), which was removed in 3.13. The aws-sdk-
# bedrock-runtime / strands bidi stack is also validated on 3.12.
FROM python:3.12-slim

# Embed the reviewed source revision in the image and /api/config.
ARG BUILD_REVISION=unknown
ENV BUILD_REVISION=${BUILD_REVISION}
LABEL org.opencontainers.image.revision=${BUILD_REVISION}

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

# Copy the compiled dashboard bundle produced by stage 1 into the directory
# FastAPI serves (GET / -> web/index.html; /static -> web/).
COPY --from=web-build /build/web ./web

RUN python -m pip install --upgrade pip \
    && python -m pip install .

# scripts/ (operator CLI) is copied after install so changes don't bust the
# dependency layer.
COPY scripts ./scripts

# Drop root for runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# uvicorn serves REST + the /media WebSocket. 0.0.0.0 so the ALB can reach it.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
