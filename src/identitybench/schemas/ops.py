"""JML plan operations (SPEC §7.5). Used by gold plans and by model outputs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class OpType(StrEnum):
    GRANT_ROLE = "GRANT_ROLE"
    REVOKE_ROLE = "REVOKE_ROLE"
    GRANT = "GRANT"
    REVOKE = "REVOKE"
    REVOKE_ALL = "REVOKE_ALL"
    DISABLE_ACCOUNT = "DISABLE_ACCOUNT"
    SUSPEND_ACCOUNT = "SUSPEND_ACCOUNT"
    RESUME_ACCOUNT = "RESUME_ACCOUNT"
    TRANSFER_OWNERSHIP = "TRANSFER_OWNERSHIP"


OP_ORDER: tuple[OpType, ...] = (
    OpType.REVOKE_ALL,
    OpType.REVOKE_ROLE,
    OpType.REVOKE,
    OpType.DISABLE_ACCOUNT,
    OpType.SUSPEND_ACCOUNT,
    OpType.TRANSFER_OWNERSHIP,
    OpType.RESUME_ACCOUNT,
    OpType.GRANT_ROLE,
    OpType.GRANT,
)
"""Total order of operations within one effective date (SPEC §7.5)."""

OP_RANK: dict[OpType, int] = {op: i for i, op in enumerate(OP_ORDER)}

REQUIRED_FIELDS: dict[OpType, tuple[str, ...]] = {
    OpType.GRANT_ROLE: ("identity_id", "role_id"),
    OpType.REVOKE_ROLE: ("membership_id",),
    OpType.GRANT: ("identity_id", "entitlement_id"),
    OpType.REVOKE: ("assignment_id",),
    OpType.REVOKE_ALL: ("identity_id",),
    OpType.DISABLE_ACCOUNT: ("account_id",),
    OpType.SUSPEND_ACCOUNT: ("account_id",),
    OpType.RESUME_ACCOUNT: ("account_id",),
    OpType.TRANSFER_OWNERSHIP: ("account_id", "new_owner_id"),
}

TARGET_FIELDS: tuple[str, ...] = (
    "identity_id",
    "role_id",
    "membership_id",
    "entitlement_id",
    "assignment_id",
    "account_id",
    "new_owner_id",
)


class Operation(BaseModel):
    """One plan operation. ``effective`` is ``IMMEDIATE`` or an ISO date (``YYYY-MM-DD``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: OpType
    identity_id: str | None = None
    role_id: str | None = None
    membership_id: str | None = None
    entitlement_id: str | None = None
    assignment_id: str | None = None
    account_id: str | None = None
    new_owner_id: str | None = None
    effective: str

    @model_validator(mode="after")
    def _check_fields(self) -> Operation:
        missing = [f for f in REQUIRED_FIELDS[self.op] if not getattr(self, f)]
        if missing:
            raise ValueError(f"{self.op} requires {', '.join(missing)}")
        return self

    def target_key(self) -> tuple[str, ...]:
        """(op, target...) key for diagnostics matching. Scoring simulates instead."""
        return (self.op.value, *(getattr(self, f) or "" for f in REQUIRED_FIELDS[self.op]))
