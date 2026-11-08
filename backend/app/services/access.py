"""Resolving what one person can actually do.

The rule is one line — *role policy, unless an admin said otherwise* — and
everything here exists to keep it that way:

    effective = default_permissions(role) with each override applied on top

Nothing caches the result on the user row. A cached copy would drift the
moment a role changed or the policy was edited, and this is a single-store
app where the resolution is one indexed query on a table holding only
exceptions.

Three safety rails, all of which have bitten real systems:

* an admin cannot change **their own** access, so nobody talks themselves out
  of the building;
* `users:manage` cannot be taken from an admin at all, so a store always has
  a way back in without someone editing the database by hand;
* a capability marked non-delegable is never granted to a role that does not
  already hold it, however the request is shaped — that is what keeps cost
  visibility behind the Admin role (CLAUDE.md rule 4).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ConflictError
from app.core.permissions import (
    ALL_PERMISSIONS,
    BY_KEY,
    LOCKED_FOR_ADMIN,
    can_hold,
    default_permissions,
    resolve_permissions,
)
from app.models.enums import AuditAction, UserRole
from app.models.user import User, UserPermissionAccess
from app.services import audit


def overrides_for(db: Session, user_id: UUID) -> dict[str, bool]:
    """{permission: is_granted} — only where an admin departed from the role."""
    return {
        row.permission: row.is_granted
        for row in db.query(UserPermissionAccess).filter(
            UserPermissionAccess.user_id == user_id
        )
    }


def permissions_for_user(db: Session, user: User) -> list[str]:
    """Everything this person can do, defaults and exceptions combined."""
    return resolve_permissions(user.role, overrides_for(db, user.id))


def has(db: Session, user: User, *permissions: str) -> bool:
    """True when the user holds **any** of these. Empty means no restriction."""
    if not permissions:
        return True
    return bool(set(permissions) & set(permissions_for_user(db, user)))


def access_summary(db: Session, user: User) -> dict:
    """Everything the admin screen needs to draw one person's access."""
    defaults = default_permissions(user.role)
    overrides = overrides_for(db, user.id)
    granted = resolve_permissions(user.role, overrides)

    return {
        "user_id": user.id,
        "role": user.role,
        "default_permissions": defaults,
        "permissions": granted,
        "overrides": [
            {"permission": key, "is_granted": value}
            for key, value in sorted(overrides.items())
            if key in BY_KEY
        ],
        # Ticks the screen must not offer: locked for an admin, or not
        # delegable to this role at all.
        "locked_permissions": sorted(
            LOCKED_FOR_ADMIN if user.role == UserRole.ADMIN.value else set()
        ),
        "unavailable_permissions": [
            key for key in ALL_PERMISSIONS if not can_hold(user.role, key)
        ],
    }


def set_permissions(
    db: Session,
    *,
    target: User,
    permissions: list[str],
    actor: User,
    note: str | None = None,
    request: Request | None = None,
) -> dict:
    """Make `target`'s access exactly `permissions`, and record why.

    The caller sends the full desired set rather than a diff: a checkbox
    screen knows what it wants the answer to be, and a diff API would race
    two admins editing the same person into a state neither chose.

    Anything matching the role default stops being an override, so a person
    put back to their defaults ends up with **no rows** — and then follows the
    policy if it ever changes.
    """
    wanted = set(permissions)

    unknown = sorted(wanted - set(ALL_PERMISSIONS))
    if unknown:
        raise AppError(
            f"Unknown permission(s): {', '.join(unknown)}.",
            code="UNKNOWN_PERMISSION",
            field="permissions",
        )

    if target.id == actor.id:
        raise ConflictError(
            "You cannot change your own access. Ask another administrator.",
            code="CANNOT_EDIT_OWN_ACCESS",
            field="permissions",
        )

    forbidden = sorted(key for key in wanted if not can_hold(target.role, key))
    if forbidden:
        raise ConflictError(
            f"A {target.role.replace('_', ' ').lower()} cannot be given: "
            f"{', '.join(BY_KEY[key].label for key in forbidden)}.",
            code="PERMISSION_NOT_DELEGABLE",
            field="permissions",
        )

    if target.role == UserRole.ADMIN.value:
        missing = sorted(LOCKED_FOR_ADMIN - wanted)
        if missing:
            raise ConflictError(
                "An administrator must keep access to user administration.",
                code="PERMISSION_LOCKED",
                field="permissions",
            )

    before = permissions_for_user(db, target)
    defaults = set(default_permissions(target.role))
    existing = {
        row.permission: row
        for row in db.query(UserPermissionAccess).filter(
            UserPermissionAccess.user_id == target.id
        )
    }

    for key in ALL_PERMISSIONS:
        is_wanted = key in wanted
        row = existing.pop(key, None)

        if is_wanted == (key in defaults):
            # Back in step with the role — stop overriding it.
            if row is not None:
                db.delete(row)
            continue

        if row is None:
            db.add(
                UserPermissionAccess(
                    user_id=target.id,
                    permission=key,
                    is_granted=is_wanted,
                    note=note,
                    created_by_id=actor.id,
                    updated_by_id=actor.id,
                )
            )
        elif row.is_granted != is_wanted or note is not None:
            row.is_granted = is_wanted
            row.note = note
            row.updated_by_id = actor.id

    # Rows for capabilities that no longer exist: drop them rather than leave
    # them to be resurrected by a future rename.
    for stale in existing.values():
        db.delete(stale)

    db.flush()
    after = permissions_for_user(db, target)

    audit.record(
        db,
        action=AuditAction.UPDATE,
        entity_type="UserPermissionAccess",
        entity_id=target.id,
        user=actor,
        store_id=target.store_id,
        before={"permissions": before},
        after={"permissions": after},
        reason=note,
        request=request,
    )
    db.commit()
    db.refresh(target)
    return access_summary(db, target)
