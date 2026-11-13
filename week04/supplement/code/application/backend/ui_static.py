"""Serve the UI entry document without retaining an old asset manifest."""
from fastapi.staticfiles import StaticFiles


class UiStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Asset filenames contain a build hash and may be cached normally. The
        # HTML chooses those filenames, so retaining it can pin an older UI.
        if path in (".", "index.html") or response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response
