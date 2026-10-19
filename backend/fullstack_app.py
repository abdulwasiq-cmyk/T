"""Serve the built Vite frontend from the same FastAPI process.

This module imports the existing API application, then adds a final SPA
fallback route. API routes remain unchanged and continue to take precedence.
"""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from main import app
from catalogues.poster_service import router as poster_router
from nexstream import router as nexstream_router

# FastAPI keeps included routers lazy, so remove this API-only root route from
# its owner before the router is expanded into the full-stack application.
poster_router.routes[:] = [
    route
    for route in poster_router.routes
    if not (getattr(route, "path", None) == "/" and getattr(route, "name", None) == "root")
]

app.include_router(nexstream_router)

# main.py already exposes "/" as the API health endpoint. In the full-stack
# container that route would win before the SPA route below, so replace only
# that root route while leaving every other API route untouched.
app.routes[:] = [
    route
    for route in app.routes
    if not (getattr(route, "path", None) == "/" and getattr(route, "name", None) == "root")
]

FRONTEND_DIR = Path("/app/frontend-dist").resolve()
INDEX_FILE = FRONTEND_DIR / "index.html"

# The HTML entrypoint references fingerprinted Vite assets. Never let a browser
# or intermediary keep an old index.html after a deployment, or it can keep
# loading an old JavaScript bundle even when the container has been updated.
HTML_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _frontend_file(path: str) -> Path | None:
    candidate = (FRONTEND_DIR / path).resolve()
    try:
        candidate.relative_to(FRONTEND_DIR)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


@app.get("/", include_in_schema=False)
async def frontend_index():
    if not INDEX_FILE.is_file():
        raise HTTPException(503, "Frontend build is not available")
    return FileResponse(INDEX_FILE, headers=HTML_NO_CACHE_HEADERS)


@app.get("/{path:path}", include_in_schema=False)
async def frontend_spa_fallback(path: str):
    # Never turn an unknown API endpoint into an HTML response.
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "API endpoint not found")

    file_path = _frontend_file(path)
    if file_path is not None:
        return FileResponse(file_path)

    if not INDEX_FILE.is_file():
        raise HTTPException(503, "Frontend build is not available")

    # React Router/client-side routes are served by the Vite index.
    return FileResponse(INDEX_FILE, headers=HTML_NO_CACHE_HEADERS)
