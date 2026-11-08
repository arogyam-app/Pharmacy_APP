"""FastAPI application factory.

Pure JSON API — this app never renders HTML. In development the Vue SPA in
../frontend is served by Vite on its own origin and talks to this over CORS.

In the native Windows deployment `SERVE_SPA=true` additionally hands over the
files Vite has already built, so the page and the API share one origin and one
port. That is a delivery arrangement, not templating: nothing here generates
markup, and every route still answers in JSON. See `core.config.serve_spa` for
why a second web server was not worth installing on a machine without Docker.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="Pharmacy POS, inventory and reporting API. See docs/API.md.",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    if settings.serve_spa:
        _mount_spa(app)
    else:
        @app.get("/", include_in_schema=False)
        def root() -> dict:
            return {
                "app": settings.app_name,
                "version": settings.version,
                "docs": "/docs",
                "api": settings.api_v1_prefix,
            }

    return app


def _mount_spa(app: FastAPI) -> None:
    """Serve the built SPA under the API, without shadowing it.

    Registered *after* the API router and after FastAPI's own `/docs` and
    `/openapi.json`, so those match first — routes are tried in the order they
    were added. Everything left over is a Vue Router path and gets
    `index.html`, which is what makes a deep link like /medicine/inventory
    survive a page refresh instead of 404ing.
    """
    dist = settings.spa_dist_path
    if not dist.is_dir():
        # A missing build is a deployment mistake, not a reason to refuse to
        # start: the API is still perfectly usable and the log says why.
        print(f"[spa] SERVE_SPA is on but {dist} does not exist — run `npm run build`")
        return

    assets = dist / "assets"
    if assets.is_dir():
        # Vite writes a content hash into every filename here, so these are
        # immutable and StaticFiles' own caching headers are safe.
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = dist / "index.html"

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str) -> FileResponse:
        candidate = (dist / spa_path).resolve()
        # `spa_path` comes off the URL, so "../../etc/passwd" is a request the
        # server will be sent eventually. Only files genuinely inside dist.
        inside = candidate.is_relative_to(dist) and candidate.is_file()
        if spa_path and inside:
            return FileResponse(candidate)
        # index.html names the hashed bundles, so a cached copy would keep
        # loading yesterday's app after every rebuild.
        return FileResponse(index, headers={"Cache-Control": "no-store, must-revalidate"})


app = create_app()
