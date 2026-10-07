"""Build the SeaweedFS S3 identity configuration (``/etc/iam/identity.json``).

Ref: https://github.com/seaweedfs/seaweedfs/blob/master/docker/compose/s3.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Identity:
    """A single S3 identity (credential pair scoped to one or more buckets)."""

    name: str
    access_key: str
    secret_key: str
    buckets: List[str]
    actions: Optional[List[str]] = None

    def to_dict(self) -> dict:
        """Render this identity as the dict shape SeaweedFS expects."""
        return {
            "name": self.name,
            "credentials": [
                {"accessKey": self.access_key, "secretKey": self.secret_key},
            ],
            "actions": self.actions or ["Read", "List", "Tagging", "Write"],
            "allowed_buckets": list(self.buckets),
        }


def admin_identity(access_key: str, secret_key: str) -> Identity:
    """Build the admin identity: full access to every bucket."""
    return Identity(
        name="admin",
        access_key=access_key,
        secret_key=secret_key,
        buckets=["*"],
        actions=["Admin", "Read", "List", "Tagging", "Write"],
    )


def build_identity_config(identities: List[Identity]) -> str:
    """Render the full identity.json content for the given identities."""
    return json.dumps({"identities": [i.to_dict() for i in identities]}, indent=2)
