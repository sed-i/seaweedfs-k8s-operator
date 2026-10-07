# Copyright 2025 him
# See LICENSE file for licensing details.

import json

from config import Config


class TestConfig:
    def test_empty_config(self):
        cfg = Config().build()
        assert json.loads(cfg) == {"identities": []}

    def test_admin_identity_has_global_actions(self):
        cfg = json.loads(Config().add_admin("AK", "SK").build())
        [identity] = cfg["identities"]
        assert identity["name"] == "admin"
        assert identity["credentials"] == [{"accessKey": "AK", "secretKey": "SK"}]
        # Bare (unsuffixed) action names grant that action on every bucket.
        assert identity["actions"] == ["Admin", "Read", "List", "Tagging", "Write"]

    def test_bucket_identity_actions_are_scoped_to_bucket(self):
        cfg = json.loads(
            Config().add_bucket_identity("relation-1", "AK", "SK", bucket="mybucket").build()
        )
        [identity] = cfg["identities"]
        assert identity["name"] == "relation-1"
        assert identity["credentials"] == [{"accessKey": "AK", "secretKey": "SK"}]
        # Every action must be suffixed with the bucket name, or it would
        # grant access to *every* bucket (see auth_credentials.go upstream).
        assert identity["actions"] == [
            "Read:mybucket",
            "List:mybucket",
            "Tagging:mybucket",
            "Write:mybucket",
        ]
        for action in identity["actions"]:
            assert action.endswith(":mybucket")

    def test_no_anonymous_identity(self):
        cfg = json.loads(Config().add_admin("AK", "SK").build())
        names = [identity["name"] for identity in cfg["identities"]]
        assert "anonymous" not in names

    def test_multiple_identities_are_independent(self):
        cfg = json.loads(
            Config()
            .add_admin("admin-ak", "admin-sk")
            .add_bucket_identity("relation-1", "ak1", "sk1", bucket="bucket1")
            .add_bucket_identity("relation-2", "ak2", "sk2", bucket="bucket2")
            .build()
        )
        names = {identity["name"] for identity in cfg["identities"]}
        assert names == {"admin", "relation-1", "relation-2"}
        bucket1_actions = next(
            i["actions"] for i in cfg["identities"] if i["name"] == "relation-1"
        )
        bucket2_actions = next(
            i["actions"] for i in cfg["identities"] if i["name"] == "relation-2"
        )
        assert all(a.endswith(":bucket1") for a in bucket1_actions)
        assert all(a.endswith(":bucket2") for a in bucket2_actions)
