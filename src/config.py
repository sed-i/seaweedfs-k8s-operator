"""S3 configuration builder for SeaweedFS."""

import json
from typing import Dict, Iterable, List

# Permissions granted to every client the charm hands out. Bucket creation is an
# administrative operation in the SeaweedFS S3 API, so "Admin" is required for
# the charm's own bucket provisioning to succeed with the same credentials.
CLIENT_ACTIONS = ["Admin", "Read", "List", "Tagging", "Write"]


def build_s3_config(credentials: Iterable[Dict[str, str]]) -> str:
    """Render the SeaweedFS ``s3.json`` from a set of client credentials.

    Each credential mapping must provide ``name``, ``access-key`` and
    ``secret-key``.
    """
    identities: List[Dict[str, object]] = []
    for credential in credentials:
        identities.append(
            {
                "name": credential["name"],
                "credentials": [
                    {
                        "accessKey": credential["access-key"],
                        "secretKey": credential["secret-key"],
                    }
                ],
                "actions": list(CLIENT_ACTIONS),
            }
        )
    return json.dumps({"identities": identities}, indent=2)
