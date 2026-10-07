"""Config builder for the SeaweedFS S3 gateway identities file.

Ref: https://github.com/seaweedfs/seaweedfs/blob/master/docker/compose/s3.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Identity:
    """A single SeaweedFS S3 identity (one access/secret key pair)."""

    name: str
    access_key: str
    secret_key: str
    actions: list = field(default_factory=lambda: ["Read", "List", "Tagging", "Write"])

    def as_dict(self) -> dict:
        """Render this identity the way SeaweedFS expects it in s3.json."""
        return {
            "name": self.name,
            "credentials": [{"accessKey": self.access_key, "secretKey": self.secret_key}],
            "actions": self.actions,
        }


class Config:
    """Builds the JSON identities file consumed by ``weed server -s3.config``.

    Unlike earlier revisions of this charm, there is intentionally no
    "anonymous" identity with blanket read/write/admin rights: every client
    must authenticate with a generated access/secret key pair, and (aside
    from the admin identity) is scoped to only the bucket it owns. This is
    what makes the charm safe to use as a real storage backend rather than
    just a throwaway testing double.
    """

    def __init__(self) -> None:
        self._identities: list[Identity] = []

    def add_admin(self, access_key: str, secret_key: str) -> "Config":
        """Add a full-access administrative identity.

        The admin identity is used by the charm itself to create buckets,
        and can also be handed to a human operator (e.g. via the
        ``get-admin-credentials`` action) for manual/home-lab use.
        """
        self._identities.append(
            Identity(
                name="admin",
                access_key=access_key,
                secret_key=secret_key,
                actions=["Admin", "Read", "List", "Tagging", "Write"],
            )
        )
        return self

    def add_bucket_identity(
        self, name: str, access_key: str, secret_key: str, bucket: str
    ) -> "Config":
        """Add an identity scoped to read/write/list a single bucket.

        SeaweedFS has no separate "allowed buckets" field: bucket scoping is
        expressed by suffixing each action with ``:<bucket>`` (see
        ``Identity.canDo`` in seaweedfs/weed/s3api/auth_credentials.go). A
        bare action name (e.g. just ``"Write"``) would instead grant that
        action on *every* bucket, so the suffix is essential here.
        """
        self._identities.append(
            Identity(
                name=name,
                access_key=access_key,
                secret_key=secret_key,
                actions=[f"{action}:{bucket}" for action in ("Read", "List", "Tagging", "Write")],
            )
        )
        return self

    def build(self) -> str:
        """Render the identities file as JSON."""
        return json.dumps({"identities": [i.as_dict() for i in self._identities]}, indent=2)
