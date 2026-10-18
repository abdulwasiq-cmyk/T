# Build directly from the uploaded new-test ZIP.
# The ZIP can contain a top-level folder (e.g. new-test-main/); normalize it
# into /src so Docker does not depend on the archive's folder name.
FROM alpine:3.21 AS source
RUN apk add --no-cache unzip
WORKDIR /src
COPY new-test-main.zip /tmp/project.zip
RUN mkdir -p /tmp/unpacked \
 && unzip -q /tmp/project.zip -d /tmp/unpacked \
 && ROOT="$(find /tmp/unpacked -type f -path '*/frontend/package.json' -print -quit | sed 's#/frontend/package.json$##')" \
 && test -n "$ROOT" \
 && cp -a "$ROOT"/. /src/ \
 && test -f /src/backend/fullstack_app.py \
 && test -f /src/Dockerfile

FROM node:22-alpine AS frontend-build
WORKDIR /src
COPY --from=source /src/frontend/package.json ./frontend/package.json
WORKDIR /src/frontend
RUN npm install
COPY --from=source /src/frontend/ ./
COPY --from=source /src/search_limits.json /src/search_limits.json
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends aria2 ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=source /src/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=source /src/backend/ ./
COPY --from=source /src/search_limits.json ./search_limits.json
COPY --from=frontend-build /src/frontend/dist ./frontend-dist
RUN python build_catalogue_pages.py /app/cache-seed  && mkdir -p /app/data

EXPOSE 10000
VOLUME ["/app/data"]
CMD ["sh", "-c", "uvicorn fullstack_app:app --host 0.0.0.0 --port ${PORT:-10000}"]
