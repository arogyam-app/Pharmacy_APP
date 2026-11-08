"""What a person can do: the capability catalogue, role defaults, and the
per-user exceptions an admin layers on top.

The unit of control is a **capability** — one verb on one thing, `medicines:create`
rather than "the medicines screen". Screens were the unit until the counter's
needs got specific: a sales executive should be able to *add* a medicine
mid-sale without being able to edit the pack size that unit maths depends on,
and to correct a customer's name without being able to remove the customer.
Neither of those is expressible one screen at a time.

Three facts are recorded against each capability and everything else is
derived from them:

* **`roles`** — who holds it before anybody changes anything. This is the
  shop's policy, and editing it moves every user at once.
* **`delegable`** — whether an admin may hand it to a non-admin. Cost
  visibility never is (CLAUDE.md rule 4), and neither is anything that would
  let a non-admin promote themselves.
* **`group`** — which heading it sits under on the Users screen, matching the
  sidebar so the two read the same way.

An override is stored only where an admin has deliberately departed from the
defaults, so a user with none follows the policy — including any later change
to it.
"""

from typing import NamedTuple

from app.models.enums import AppModule, UserRole

ADMIN = UserRole.ADMIN.value
EXEC = UserRole.SALES_EXEC.value
BOTH = (EXEC, ADMIN)
ADMIN_ONLY = (ADMIN,)


class Capability(NamedTuple):
    key: str
    label: str
    group: str
    #: Roles holding it by default.
    roles: tuple[str, ...]
    #: May an admin grant it to someone whose role does not include it?
    delegable: bool = False
    #: Cannot be revoked from an admin, at any price.
    locked: bool = False


CAPABILITIES: list[Capability] = [
    # --- the counter: a sales executive runs all of this ------------------
    Capability("pos:sell", "Sell at the counter", AppModule.POS.value, BOTH),
    Capability("sales:read", "View the sales register", AppModule.SALES.value, BOTH),
    Capability("sales:reprint", "Reprint an invoice", AppModule.SALES.value, BOTH),
    Capability("sales:request_void", "Request a void", AppModule.SALES.value, BOTH),
    Capability(
        "sales:approve_void", "Approve a void", AppModule.SALES.value, ADMIN_ONLY
    ),
    Capability("returns:create", "Accept a return", AppModule.RETURNS.value, BOTH),
    Capability(
        "returns:approve",
        "Approve an out-of-window return",
        AppModule.RETURNS.value,
        ADMIN_ONLY,
    ),
    Capability(
        "credit_notes:read", "Look up a credit note", AppModule.RETURNS.value, BOTH
    ),
    Capability(
        "credit_notes:cancel", "Cancel a credit note", AppModule.RETURNS.value, ADMIN_ONLY
    ),

    # --- stock: the counter looks, the owner acts -------------------------
    Capability("inventory:read", "View stock and batches", AppModule.INVENTORY.value, BOTH),
    Capability(
        "inventory:adjust", "Adjust stock", AppModule.INVENTORY.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability("alerts:read", "View alerts", AppModule.ALERTS.value, BOTH),
    Capability("expiry:read", "View the expiry register", AppModule.EXPIRY.value, BOTH),
    Capability(
        "expiry:manage",
        "Write off and return expired stock",
        AppModule.EXPIRY.value,
        ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "purchases:read", "View purchases", AppModule.PURCHASES.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "purchases:receive", "Receive goods", AppModule.PURCHASES.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        # Separate from `purchases:receive`: sending goods back creates a
        # credit against the distributor, which is money. Whoever books
        # deliveries in need not also be able to raise one.
        "purchases:return",
        "Return goods to a supplier",
        AppModule.PURCHASES.value,
        ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "suppliers:read", "View suppliers", AppModule.SUPPLIERS.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "suppliers:manage", "Maintain suppliers", AppModule.SUPPLIERS.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "order_book:read", "View the order book", AppModule.ORDER_BOOK.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "order_book:manage", "Manage the order book", AppModule.ORDER_BOOK.value,
        ADMIN_ONLY, delegable=True,
    ),

    # --- masters: add freely, change carefully ----------------------------
    #
    # An executive may put a brand on file that a customer has just asked for.
    # Editing one is a different thing: pack size, HSN code and schedule drive
    # the unit maths, the tax on the bill and the prescription check.
    Capability("catalog:read", "View the catalogue", AppModule.MASTERS.value, BOTH),
    Capability("medicines:create", "Add a medicine", AppModule.MASTERS.value, BOTH),
    Capability(
        "medicines:edit", "Edit a medicine", AppModule.MASTERS.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "medicines:delete", "Delete a medicine", AppModule.MASTERS.value, ADMIN_ONLY,
        delegable=True,
    ),
    Capability(
        "compositions:create", "Add a composition", AppModule.COMPOSITIONS.value, BOTH
    ),
    Capability(
        "compositions:edit", "Edit a composition", AppModule.COMPOSITIONS.value,
        ADMIN_ONLY, delegable=True,
    ),
    Capability(
        "manufacturers:create", "Add a manufacturer", AppModule.MANUFACTURERS.value, BOTH
    ),
    Capability(
        "manufacturers:edit", "Edit a manufacturer", AppModule.MANUFACTURERS.value,
        ADMIN_ONLY, delegable=True,
    ),
    Capability("customers:read", "View customers", AppModule.MASTERS.value, BOTH),
    Capability("customers:create", "Add a customer", AppModule.MASTERS.value, BOTH),
    Capability("customers:edit", "Edit a customer", AppModule.MASTERS.value, BOTH),
    Capability(
        "customers:delete", "Delete or disable a customer", AppModule.MASTERS.value,
        ADMIN_ONLY, delegable=True,
    ),
    Capability("doctors:read", "View doctors", AppModule.MASTERS.value, BOTH),
    Capability("doctors:create", "Add a doctor", AppModule.MASTERS.value, BOTH),
    Capability("doctors:edit", "Edit a doctor", AppModule.MASTERS.value, BOTH),
    Capability(
        "doctors:delete", "Delete or disable a doctor", AppModule.MASTERS.value,
        ADMIN_ONLY, delegable=True,
    ),

    # --- the owner's own -------------------------------------------------
    #
    # None of these is delegable. `cost:view` is rule 4 itself; the rest would
    # let a non-admin rewrite the policy that governs them, or promote
    # themselves, which is escalation rather than delegation.
    Capability("cost:view", "See cost prices and margin", AppModule.REPORTS.value, ADMIN_ONLY),
    Capability("reports:view", "View reports", AppModule.REPORTS.value, ADMIN_ONLY),
    Capability("audit:view", "View the audit trail", AppModule.REPORTS.value, ADMIN_ONLY),
    Capability(
        "users:manage", "Manage users and access", AppModule.USERS.value, ADMIN_ONLY,
        locked=True,
    ),
    Capability("settings:manage", "Change store settings", AppModule.SETTINGS.value, ADMIN_ONLY),
    Capability("store:manage", "Edit the store profile", AppModule.SETTINGS.value, ADMIN_ONLY),
]

BY_KEY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}
ALL_PERMISSIONS: list[str] = [c.key for c in CAPABILITIES]

#: Cannot be taken from an admin, by themselves or anyone else. Without it two
#: admins revoking each other leaves a store with no way back in.
LOCKED_FOR_ADMIN = {c.key for c in CAPABILITIES if c.locked}

DELEGABLE = {c.key for c in CAPABILITIES if c.delegable}

#: Headings on the Users screen, in sidebar order. Mirrored in
#: frontend/src/constants.js.
GROUP_ORDER: list[tuple[str, str]] = [
    (AppModule.POS.value, "POS"),
    (AppModule.SALES.value, "Sales"),
    (AppModule.RETURNS.value, "Returns"),
    (AppModule.INVENTORY.value, "Inventory"),
    (AppModule.ALERTS.value, "Alerts"),
    (AppModule.EXPIRY.value, "Expiry register"),
    (AppModule.PURCHASES.value, "Purchases"),
    (AppModule.SUPPLIERS.value, "Suppliers"),
    (AppModule.ORDER_BOOK.value, "Order book"),
    (AppModule.MASTERS.value, "Medicines, customers & doctors"),
    (AppModule.COMPOSITIONS.value, "Compositions & salts"),
    (AppModule.MANUFACTURERS.value, "Manufacturers"),
    (AppModule.REPORTS.value, "Reports & cost"),
    (AppModule.USERS.value, "Users"),
    (AppModule.SETTINGS.value, "Settings"),
]
GROUP_LABELS = dict(GROUP_ORDER)


def default_permissions(role: str) -> list[str]:
    """What the role holds before an admin changes anything."""
    return [c.key for c in CAPABILITIES if role in c.roles]


def permissions_for(role: str) -> list[str]:
    """Alias kept for callers that only care about the role's own set."""
    return default_permissions(role)


def resolve_permissions(role: str, overrides: dict[str, bool]) -> list[str]:
    """Role defaults with each admin exception applied on top.

    A grant of something the role never had is honoured only when the
    capability is delegable — otherwise a stale row, or a hand-edited one,
    could hand out cost visibility that rule 4 forbids.
    """
    granted = set(default_permissions(role))

    for key, is_granted in overrides.items():
        capability = BY_KEY.get(key)
        if capability is None:
            continue
        if is_granted:
            if role in capability.roles or capability.delegable:
                granted.add(key)
        else:
            granted.discard(key)

    if role == ADMIN:
        granted |= LOCKED_FOR_ADMIN

    return [key for key in ALL_PERMISSIONS if key in granted]


def can_hold(role: str, key: str) -> bool:
    """Whether this role may ever hold this capability, granted or not."""
    capability = BY_KEY.get(key)
    if capability is None:
        return False
    return role in capability.roles or capability.delegable
