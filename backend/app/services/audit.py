"""Audit trail writer (CLAUDE.md rule 6: every mutation records a row)."""

from uuid import UUID

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.enums import AuditAction
from app.models.user import User

# Never let a secret reach the audit table.
REDACTED_FIELDS = {"password", "password_hash", "new_password", "current_password", "token_hash"}


def _default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def snapshot(obj: Any, fields: list[str] | None = None) -> dict:
    """Plain dict of a model row, suitable for before/after JSON."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        data = dict(obj)
    else:
        columns = [c.key for c in inspect(obj).mapper.column_attrs]
        data = {key: getattr(obj, key) for key in (fields or columns)}
    return {k: ("***" if k in REDACTED_FIELDS else v) for k, v in data.items()}


def record(
    db: Session,
    *,
    action: AuditAction,
    entity_type: str,
    entity_id: UUID | None = None,
    user: User | None = None,
    store_id: UUID | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    request: Request | None = None,
) -> AuditLog:
    """Add an audit row to the session. The caller commits — so the audit row
    lands in the same transaction as the change it describes, or not at all."""
    entry = AuditLog(
        store_id=store_id if store_id is not None else (user.store_id if user else None),
        user_id=user.id if user else None,
        action=action.value,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=json.dumps(snapshot(before), default=_default) if before is not None else None,
        after_json=json.dumps(snapshot(after), default=_default) if after is not None else None,
        reason=reason,
    )
    if request is not None:
        entry.ip_address = _client_ip(request)
        entry.user_agent = (request.headers.get("user-agent") or "")[:255] or None

    db.add(entry)
    return entry


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None
