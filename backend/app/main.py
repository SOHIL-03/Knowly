"""Knowly API: the single Cloud Run service for the live path (Architecture v3 section 2)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.diagnostics import emulator_status, log_startup
from app.deps import correlation_middleware, services
from app.errors import install_error_handlers
from app.logs import configure_logging
from app.routes import briefings, checkpoint, internal, library, user
from app.services import Services


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    log_startup(settings)
    app = FastAPI(title="Knowly API", version="3.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    install_error_handlers(app)
    app.middleware("http")(correlation_middleware)

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and int(length) > settings.max_request_bytes:
            return JSONResponse(status_code=413, content={"error": {"code": "too_large", "message": "Request is too large"}})
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["x-correlation-id"],
    )

    for r in (user.router, briefings.router, checkpoint.router, library.router):
        app.include_router(r, prefix="/api")
    app.include_router(internal.router)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": app.version}

    @app.get("/api/ready")
    def ready():
        checks: dict[str, str] = {}
        try:
            svc: Services = services()
        except Exception as exc:
            # e.g. no Google credentials or emulator settings for Storage/Firestore
            checks["services"] = f"error: {type(exc).__name__}: {str(exc)[:200]}"
            return JSONResponse(status_code=503, content={"ready": False, "checks": checks})
        try:
            svc.db.collection("senders").limit(1).get()
            checks["firestore"] = "ok"
        except Exception as exc:
            checks["firestore"] = f"error: {type(exc).__name__}"
        s = svc.settings
        from app.ai.gemini_keys import load_keys

        key_names = [k.name for k in load_keys(s)]  # names only, never values
        checks["openai"] = f"{s.openai_model}" if s.openai_api_key.strip() else "not configured (needs OPENAI_API_KEY)"
        checks["gemini"] = "vertex" if s.gemini_use_vertex else (f"{len(key_names)} key(s): {', '.join(key_names)}" if key_names else "not configured")
        checks["tts"] = s.gemini_tts_model if key_names else "not configured (needs GEMINI_API_KEY_1)"
        checks["dispatch"] = s.dispatch_mode
        for var, status in emulator_status().items():
            checks[var.removesuffix("_HOST").lower()] = status
        ok = checks["firestore"] == "ok"
        return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})

    return app


app = create_app()
