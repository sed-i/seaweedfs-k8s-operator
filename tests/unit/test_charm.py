# Copyright 2025 him
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import ops.testing as testing
import pytest
import yaml

from charm import IDENTITIES_SECRET_ID_KEY, IDENTITIES_SECRET_LABEL, SeaweedfsK8S

CHARMCRAFT_YAML = yaml.safe_load(
    (Path(__file__).parent.parent.parent / "charmcraft.yaml").read_text()
)
OWN_FQDN = "swfs-0.swfs-endpoints.welcome-k8s.svc.cluster.local"


def make_context(unit_id: int = 0) -> testing.Context:
    return testing.Context(
        SeaweedfsK8S,
        meta=CHARMCRAFT_YAML,
        actions=CHARMCRAFT_YAML.get("actions"),
        config=CHARMCRAFT_YAML.get("config"),
        unit_id=unit_id,
    )


def weed_version_exec() -> testing.Exec:
    return testing.Exec(
        ["/usr/bin/weed", "version"],
        return_code=0,
        stdout="version 30GB 3.97 76452ab59 linux amd64",
    )


def identities_secret(**buckets) -> testing.Secret:
    content = {
        "identities-json": json.dumps(
            {"admin_access_key": "ADMIN-AK", "admin_secret_key": "ADMIN-SK", "buckets": buckets}
        )
    }
    return testing.Secret(tracked_content=content, owner="app", label=IDENTITIES_SECRET_LABEL)


@pytest.fixture
def fqdn():
    with patch("socket.getfqdn", return_value=OWN_FQDN) as mocked:
        yield mocked


class TestReconcileEarlyExits:
    def test_container_not_ready_does_not_crash(self, fqdn):
        ctx = make_context()
        container = testing.Container("seaweedfs", can_connect=False)
        peer = testing.PeerRelation("swfs-peers")
        state_in = testing.State(containers=[container], relations=[peer], leader=True)

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, testing.UnknownStatus)

    @pytest.mark.parametrize("event_name", ["install", "remove", "stop"])
    def test_lifecycle_hooks_skip_reconcile(self, fqdn, event_name):
        ctx = make_context()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation("swfs-peers")
        state_in = testing.State(containers=[container], relations=[peer], leader=True)

        state_out = ctx.run(getattr(ctx.on, event_name)(), state_in)

        assert isinstance(state_out.unit_status, testing.UnknownStatus)
        assert state_out.get_container("seaweedfs").plan.services == {}

    def test_waiting_without_peer_relation(self, fqdn):
        ctx = make_context()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        state_in = testing.State(containers=[container], relations=[], leader=True)

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, testing.WaitingStatus)

    def test_non_leader_waits_until_secret_is_published(self, fqdn):
        ctx = make_context(unit_id=1)
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation("swfs-peers")  # no identities-secret-id yet
        state_in = testing.State(
            containers=[container], relations=[peer], leader=False, planned_units=2
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, testing.WaitingStatus)


class TestIdentityBootstrap:
    def test_leader_creates_identities_secret(self, fqdn):
        ctx = make_context()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation("swfs-peers")
        state_in = testing.State(
            containers=[container], relations=[peer], leader=True, planned_units=1
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)):
            state_out = ctx.run(ctx.on.config_changed(), state_in)

        [secret] = state_out.secrets
        assert secret.label == IDENTITIES_SECRET_LABEL
        identities = json.loads(secret.tracked_content["identities-json"])
        assert identities["admin_access_key"]
        assert identities["admin_secret_key"]
        assert identities["admin_access_key"] != identities["admin_secret_key"]

        peer_out = state_out.get_relation(peer.id)
        assert peer_out.local_app_data[IDENTITIES_SECRET_ID_KEY] == secret.id

    def test_non_leader_reuses_published_secret(self, fqdn):
        ctx = make_context(unit_id=1)
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=False,
            planned_units=2,
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, testing.ActiveStatus)
        plan = state_out.get_container("seaweedfs").plan
        config_hash_env = plan.services["seaweedfs"].environment["_config_hash"]
        assert config_hash_env  # a config file was rendered from the shared identities


class TestClusterTopology:
    def test_single_unit_is_master_with_no_replication(self, fqdn):
        ctx = make_context(unit_id=0)
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=1,
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)):
            state_out = ctx.run(ctx.on.config_changed(), state_in)

        command = state_out.get_container("seaweedfs").plan.services["seaweedfs"].command
        assert "-master=true" in command
        assert "-filer.defaultReplicaPlacement=000" in command
        assert "-master.defaultReplication=000" in command

    def test_fourth_unit_is_not_master_and_replicates(self, fqdn):
        ctx = make_context(unit_id=3)
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=False,
            planned_units=4,
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        command = state_out.get_container("seaweedfs").plan.services["seaweedfs"].command
        assert "-master=false" in command
        assert "-filer.defaultReplicaPlacement=002" in command
        # Only the 3 lowest-numbered units are master-eligible.
        assert "swfs-0" in command and "swfs-1" in command and "swfs-2" in command
        assert "swfs-3" not in command

    def test_replication_config_override(self, fqdn):
        ctx = make_context(unit_id=0)
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=1,
            config={"replication": "002", "bucket": ""},
        )

        state_out = ctx.run(ctx.on.config_changed(), state_in)

        command = state_out.get_container("seaweedfs").plan.services["seaweedfs"].command
        assert "-filer.defaultReplicaPlacement=002" in command


class TestBucketsAndRelations:
    def test_default_bucket_created_by_leader(self, fqdn):
        ctx = make_context()
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=1,
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)) as cb:
            ctx.run(ctx.on.config_changed(), state_in)

        cb.assert_any_call("default-bucket", "ADMIN-AK", "ADMIN-SK")

    def test_empty_bucket_config_skips_creation(self, fqdn):
        ctx = make_context()
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=1,
            config={"bucket": ""},
        )

        with patch("charm.create_bucket") as cb:
            ctx.run(ctx.on.config_changed(), state_in)

        cb.assert_not_called()

    def test_s3_relation_gets_scoped_bucket_and_credentials(self, fqdn):
        ctx = make_context()
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        s3_relation = testing.Relation("s3-credentials", remote_app_name="grafana")
        state_in = testing.State(
            containers=[container],
            relations=[peer, s3_relation],
            secrets=[secret],
            leader=True,
            planned_units=1,
            config={"bucket": ""},
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)) as cb:
            state_out = ctx.run(ctx.on.relation_changed(s3_relation), state_in)

        relation_out = state_out.get_relation(s3_relation.id)
        bucket_name = relation_out.local_app_data["bucket"]
        assert bucket_name == f"s3-credentials-{s3_relation.id}"
        access_key = relation_out.local_app_data["access-key"]
        secret_key = relation_out.local_app_data["secret-key"]
        assert access_key and secret_key
        assert relation_out.local_app_data["endpoint"] == f"http://{OWN_FQDN}:8333"
        cb.assert_any_call(bucket_name, access_key, secret_key)

        # Credentials must be scoped: persisted in the shared secret too, not
        # just handed out ad hoc.
        [updated_secret] = state_out.secrets
        assert updated_secret.latest_content is not None
        identities = json.loads(updated_secret.latest_content["identities-json"])
        assert identities["buckets"][str(s3_relation.id)]["bucket"] == bucket_name

    def test_removed_relation_prunes_bucket_identity(self, fqdn):
        ctx = make_context()
        secret = identities_secret(**{"99": {
            "bucket": "s3-credentials-99",
            "access_key": "OLD-AK",
            "secret_key": "OLD-SK",
        }})
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],  # the s3-credentials relation for id 99 is gone
            secrets=[secret],
            leader=True,
            planned_units=1,
            config={"bucket": ""},
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)):
            state_out = ctx.run(ctx.on.config_changed(), state_in)

        [updated_secret] = state_out.secrets
        assert updated_secret.latest_content is not None
        identities = json.loads(updated_secret.latest_content["identities-json"])
        assert identities["buckets"] == {}

    def test_bucket_creation_connection_error_sets_maintenance_status(self, fqdn):
        ctx = make_context()
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=1,
        )

        with patch("charm.create_bucket", side_effect=ConnectionError("s3 not up yet")):
            state_out = ctx.run(ctx.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, testing.MaintenanceStatus)


class TestPortsAndStatus:
    def test_ports_opened(self, fqdn):
        ctx = make_context()
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=1,
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)):
            state_out = ctx.run(ctx.on.config_changed(), state_in)

        opened = {p.port for p in state_out.opened_ports}
        assert opened == {9333, 8080, 8888, 8333}

    def test_health_check_uses_unauthenticated_volume_endpoint(self, fqdn):
        ctx = make_context()
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=1,
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)):
            state_out = ctx.run(ctx.on.config_changed(), state_in)

        plan = state_out.get_container("seaweedfs").plan
        http_check = plan.checks["volume-online"].http
        assert http_check is not None
        assert http_check.get("url") == "http://localhost:8080/status"


class TestGetAdminCredentialsAction:
    def test_returns_credentials_and_endpoints(self, fqdn):
        ctx = make_context()
        secret = identities_secret()
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation(
            "swfs-peers", local_app_data={IDENTITIES_SECRET_ID_KEY: secret.id}
        )
        state_in = testing.State(
            containers=[container],
            relations=[peer],
            secrets=[secret],
            leader=True,
            planned_units=3,
        )

        with patch("charm.create_bucket", return_value=MagicMock(status=200)):
            ctx.run(ctx.on.action("get-admin-credentials"), state_in)

        results = ctx.action_results
        assert results is not None
        assert results["access-key"] == "ADMIN-AK"
        assert results["secret-key"] == "ADMIN-SK"
        assert results["endpoint"] == f"http://{OWN_FQDN}:8333"
        # No doubled port suffix, and one endpoint per planned unit.
        endpoints = results["endpoints"].split(",")
        assert len(endpoints) == 3
        for endpoint in endpoints:
            assert endpoint.count(":8333") == 1

    def test_fails_when_not_yet_ready(self, fqdn):
        ctx = make_context(unit_id=1)
        container = testing.Container(
            "seaweedfs", can_connect=True, execs={weed_version_exec()}
        )
        peer = testing.PeerRelation("swfs-peers")  # no secret published yet
        state_in = testing.State(
            containers=[container], relations=[peer], leader=False, planned_units=2
        )

        with pytest.raises(testing.ActionFailed):
            ctx.run(ctx.on.action("get-admin-credentials"), state_in)
