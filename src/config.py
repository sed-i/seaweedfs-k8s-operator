"""S3 identity configuration builder for SeaweedFS.

Ref: https://github.com/seaweedfs/seaweedfs/blob/master/docker/compose/s3.json
"""

import json
from typing import Dict, List, Optional


class S3Identity:
    """A single S3 identity entry."""

    def __init__(
        self,
        name: str,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        actions: Optional[List[str]] = None,
        allowed_buckets: Optional[List[str]] = None,
    ):
        self.name = name
        self.access_key = access_key
        self.secret_key = secret_key
        self.actions = actions or ["Admin", "Read", "List", "Tagging", "Write"]
        self.allowed_buckets = allowed_buckets or ["*"]

    def to_dict(self) -> Dict:
        """Serialize to the seaweedfs S3 identity dict format."""
        identity: Dict = {
            "name": self.name,
            "actions": self.actions,
        }
        if self.access_key and self.secret_key:
            identity["credentials"] = [
                {"accessKey": self.access_key, "secretKey": self.secret_key}
            ]
        if self.allowed_buckets:
            identity["allowed_buckets"] = self.allowed_buckets
        return identity


class S3Config:
    """Builds the s3.json configuration for SeaweedFS."""

    def __init__(self) -> None:
        self._identities: List[S3Identity] = []

    def add_admin_identity(self, access_key: str, secret_key: str) -> None:
        """Add the admin identity with full access to all buckets."""
        self._identities.append(
            S3Identity(
                name="admin",
                access_key=access_key,
                secret_key=secret_key,
                actions=["*"],
                allowed_buckets=["*"],
            )
        )

    def add_anonymous_identity(self) -> None:
        """Add an anonymous identity for unauthenticated access."""
        self._identities.append(
            S3Identity(
                name="anonymous",
                actions=["Admin", "Read", "List", "Tagging", "Write"],
                allowed_buckets=["*"],
            )
        )

    def add_relation_identity(
        self, relation_id: int, access_key: str, secret_key: str, bucket: str
    ) -> None:
        """Add a per-relation identity scoped to a single bucket."""
        self._identities.append(
            S3Identity(
                name=f"relation-{relation_id}",
                access_key=access_key,
                secret_key=secret_key,
                actions=["Admin", "Read", "List", "Tagging", "Write"],
                allowed_buckets=[bucket],
            )
        )

    def build(self) -> str:
        """Build the full s3.json content string."""
        config = {"identities": [i.to_dict() for i in self._identities]}
        return json.dumps(config, indent=2)
