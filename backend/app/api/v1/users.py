"""User administration — admin only."""

from uuid import UUID

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, or_

from app.core.deps import AdminUser, DbSession
from app.core.exceptions import ConflictError, NotFoundError, PermissionError_
from app.core.permissions import CAPABILITIES, GROUP_LABELS, GROUP_ORDER
from app.core.security import generate_temp_password, hash_password
from app.models.enums import AuditAction, UserRole
from app.models.user import RefreshToken, User
from app.schemas.common import Page, PageParams
from app.schemas.user import (
    PermissionCatalogEntry,
    PasswordResetResult,
    UserAccessOut,
    UserAccessUpdate,
    UserCreate,
    UserCreated,
    UserOut,
    UserUpdate,
)
from app.services import access, audit

router = APIRouter()

AUDIT_FIELDS = ["id", "full_name", "email", "phone", "role", "is_active", "must_change_password"]


@router.get("", response_model=Page[UserOut])
def list_users(
    admin: AdminUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = Query(None, description="Matches name or email"),
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> Page[UserOut]:
    query = db.query(User).filter(User.store_id == admin.store_id)
    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(func.lower(User.full_name).like(needle), func.lower(User.email).like(needle))
        )
    if role is not None:
        query = query.filter(User.role == role.value)
    if is_active is not None:
        query = query.filter(User.is_active.is_(is_active))

    total = query.count()
    rows = (
        query.order_by(User.full_name).offset(params.offset).limit(params.page_size).all()
    )
    return Page.build([UserOut.model_validate(r) for r in rows], total, params)


@router.post("", response_model=UserCreated, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate, request: Request, admin: AdminUser, db: DbSession
) -> UserCreated:
    email = payload.email.strip().lower()
    if db.query(User).filter(func.lower(User.email) == email).count():
        raise ConflictError("That email is already registered.", code="EMAIL_ALREADY_EXISTS",
                            field="email")

    generated = payload.password is None
    raw_password = payload.password or generate_temp_password()

    user = User(
        store_id=admin.store_id,
        full_name=payload.full_name.strip(),
        email=email,
        phone=payload.phone,
        role=payload.role.value,
        password_hash=hash_password(raw_password),
        # A password the admin knows must be replaced on first sign-in.
        must_change_password=True,
        created_by_id=admin.id,
        updated_by_id=admin.id,
    )
    db.add(user)
    db.flush()

    audit.record(
        db, action=AuditAction.CREATE, entity_type="User", entity_id=user.id, user=admin,
        after=audit.snapshot(user, AUDIT_FIELDS), request=request,
    )
    db.commit()
    db.refresh(user)

    return UserCreated(
        user=UserOut.model_validate(user),
        temporary_password=raw_password if generated else None,
    )


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: UUID, admin: AdminUser, db: DbSession) -> UserOut:
    return UserOut.model_validate(_get(db, user_id, admin.store_id))


@router.get("/permissions/catalogue", response_model=list[PermissionCatalogEntry])
def permission_catalogue(admin: AdminUser) -> list[PermissionCatalogEntry]:
    """Every capability an admin can tick, grouped the way the sidebar is.

    Served rather than hard-coded in the SPA so the two cannot drift: a
    capability added on the server appears on the screen without a frontend
    release, already in the right group and with the right label.
    """
    return [
        PermissionCatalogEntry(
            key=c.key,
            label=c.label,
            group=c.group,
            group_label=GROUP_LABELS.get(c.group, c.group),
            delegable=c.delegable,
            locked=c.locked,
            default_roles=list(c.roles),
        )
        for group, _ in GROUP_ORDER
        for c in CAPABILITIES
        if c.group == group
    ]


@router.get("/{user_id}/permissions", response_model=UserAccessOut)
def get_user_permissions(user_id: UUID, admin: AdminUser, db: DbSession) -> UserAccessOut:
    return UserAccessOut(**access.access_summary(db, _get(db, user_id, admin.store_id)))


@router.put("/{user_id}/permissions", response_model=UserAccessOut)
def set_user_permissions(
    user_id: UUID,
    payload: UserAccessUpdate,
    request: Request,
    admin: AdminUser,
    db: DbSession,
) -> UserAccessOut:
    """Set exactly what this person can do.

    Sessions are left alone deliberately: the gates resolve access per
    request, so a revoke takes effect on the target's very next call without
    forcing them to sign in again mid-sale. Their menu catches up when the
    SPA next reads `/auth/me`.
    """
    target = _get(db, user_id, admin.store_id)
    summary = access.set_permissions(
        db, target=target, permissions=payload.permissions, actor=admin,
        note=payload.note, request=request,
    )
    return UserAccessOut(**summary)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: UUID, payload: UserUpdate, request: Request, admin: AdminUser, db: DbSession
) -> UserOut:
    user = _get(db, user_id, admin.store_id)
    before = audit.snapshot(user, AUDIT_FIELDS)
    changes = payload.model_dump(exclude_unset=True)

    if user.id == admin.id:
        # Guard against an admin locking themselves out of their own store.
        if changes.get("is_active") is False:
            raise ConflictError("You cannot deactivate your own account.", field="is_active")
        if "role" in changes and changes["role"] != UserRole.ADMIN:
            _assert_not_last_admin(db, admin.store_id, user)

    for field, value in changes.items():
        setattr(user, field, value.value if isinstance(value, UserRole) else value)
    user.updated_by_id = admin.id

    if changes.get("is_active") is False or "role" in changes:
        _revoke_sessions(db, user.id)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="User", entity_id=user.id, user=admin,
        before=before, after=audit.snapshot(user, AUDIT_FIELDS), request=request,
    )
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/{user_id}/reset-password", response_model=PasswordResetResult)
def reset_password(
    user_id: UUID, request: Request, admin: AdminUser, db: DbSession
) -> PasswordResetResult:
    user = _get(db, user_id, admin.store_id)
    temp_password = generate_temp_password()
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    user.updated_by_id = admin.id
    _revoke_sessions(db, user.id)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="User", entity_id=user.id, user=admin,
        after={"password_reset": True}, request=request,
    )
    db.commit()
    return PasswordResetResult(user_id=user.id, temporary_password=temp_password)


@router.post("/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(
    user_id: UUID, request: Request, admin: AdminUser, db: DbSession
) -> UserOut:
    user = _get(db, user_id, admin.store_id)
    if user.id == admin.id:
        raise ConflictError("You cannot deactivate your own account.")
    _assert_not_last_admin(db, admin.store_id, user)

    before = audit.snapshot(user, AUDIT_FIELDS)
    user.is_active = False
    user.updated_by_id = admin.id
    _revoke_sessions(db, user.id)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="User", entity_id=user.id, user=admin,
        before=before, after=audit.snapshot(user, AUDIT_FIELDS), reason="Deactivated",
        request=request,
    )
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


# --- helpers -------------------------------------------------------------


def _get(db, user_id: UUID, store_id: UUID) -> User:
    user = db.get(User, user_id)
    if user is None or user.store_id != store_id:
        raise NotFoundError("User not found.")
    return user


def _assert_not_last_admin(db, store_id: UUID, user: User) -> None:
    if user.role != UserRole.ADMIN.value:
        return
    remaining = (
        db.query(User)
        .filter(
            User.store_id == store_id,
            User.role == UserRole.ADMIN.value,
            User.is_active.is_(True),
            User.id != user.id,
        )
        .count()
    )
    if remaining == 0:
        raise PermissionError_(
            "This is the only active administrator; the store would be left unmanageable.",
            code="LAST_ADMIN",
            status_code=409,
        )


def _revoke_sessions(db, user_id: UUID) -> None:
    now = datetime.now(timezone.utc)
    for token in db.query(RefreshToken).filter(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
    ):
        token.revoked_at = now
