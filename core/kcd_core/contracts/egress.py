"""Contract C6 (DRAFT, to agree with the Safety Verifier owner): the Safety Verifier's signed decision about
sending one specific payload to one specific cloud target.

The approval is bound to the exact bytes (via `payload_hash`), the target, the requesting seat and that seat's
manifest version, and it expires. It is not a flag: a flag could be replayed for different, un-anonymised data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EgressApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=8, description="Unique per decision; an approval can be used once")
    decision: Literal["approved", "rejected"]
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", description="content_hash of the anonymised payload")
    target: str = Field(min_length=1, description="The cloud model/service this decision is for")
    approved_by: str = Field(description="Key id of the signer; Core accepts only the Safety Verifier")
    requesting_seat: str
    manifest_version: str = Field(description="Manifest.ref of the requesting seat when this was decided")
    issued_at: datetime
    expires_at: datetime
    reason: str | None = Field(default=None, max_length=500, description="Required when rejected")
    sig: str | None = None

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)  # one canonical form, so the signed bytes are unambiguous

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.decision == "rejected" and not self.reason:
            raise ValueError("a rejection needs a reason")
        return self
