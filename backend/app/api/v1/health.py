"""Liveness/readiness probe — also the SPA's connectivity check."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:  # pragma: no cover - only on a broken DB
        db_status = f"error: {exc.__class__.__name__}"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "db": db_status,
        "app": settings.app_name,
        "version": settings.version,
        "environment": settings.environment,
    }
