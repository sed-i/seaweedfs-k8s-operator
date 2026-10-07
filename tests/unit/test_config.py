# Copyright 2025 him
# See LICENSE file for licensing details.

import json

from config import build_s3_config


def test_build_s3_config_renders_credentials_and_actions():
    rendered = build_s3_config(
        [{"name": "admin", "access-key": "access", "secret-key": "secret"}]
    )

    config = json.loads(rendered)
    identity = config["identities"][0]
    assert identity["name"] == "admin"
    assert identity["credentials"] == [{"accessKey": "access", "secretKey": "secret"}]
    assert "Admin" in identity["actions"]
    assert "Write" in identity["actions"]


def test_build_s3_config_supports_multiple_identities():
    rendered = build_s3_config(
        [
            {"name": "one", "access-key": "a1", "secret-key": "s1"},
            {"name": "two", "access-key": "a2", "secret-key": "s2"},
        ]
    )

    assert len(json.loads(rendered)["identities"]) == 2
