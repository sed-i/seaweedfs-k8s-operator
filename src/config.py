"""Config builder for seaweedfs."""

import textwrap


class Config:
    """Config builder for seaweedfs.

    Ref: https://github.com/seaweedfs/seaweedfs/blob/master/docker/compose/s3.json
    """

    def build(self) -> str:
        """Create a json string of the config."""
        return textwrap.dedent("""
            {
              "identities": [
                {
                  "name": "admin",
                  "credentials": [
                    {
                      "accessKey": "admin",
                      "secretKey": "admin"
                    }
                  ],
                  "actions": ["*"],
                  "allowed_buckets": ["*"]
                },
                {
                  "name": "anonymous",
                  "actions": [
                    "Admin",
                    "Read",
                    "List",
                    "Tagging",
                    "Write"
                  ]
                },
                {
                  "name": "placeholder",
                  "credentials": [
                    {
                      "accessKey": "placeholder",
                      "secretKey": "placeholder"
                    }
                  ],
                  "actions": [
                    "Admin",
                    "Read",
                    "List",
                    "Tagging",
                    "Write"
                  ]
                }
              ]
            }
        """)
