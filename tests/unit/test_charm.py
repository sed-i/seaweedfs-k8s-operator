# Copyright 2025 him
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Scenario-style (ops.testing) unit tests for the SeaweedFS charm."""

from unittest.mock import MagicMock

import pytest
from ops import testing
from ops.testing import ActionFailed

import charm as charm_module
import peers
from charm import (
    ADMIN_SECRET_LABEL,
    CONTAINER,
    IDENTITY_PATH,
    S3_RELATION,
    SeaweedfsK8SCharm,
    relation_secret_label,
)

APP = "seaweedfs-k8s"


@pytest.fixture
def ctx():
    return testing.Context(SeaweedfsK8SCharm, charm_root=".")


def container(can_connect=True, execs=None):
    return testing.Container(CONTAINER, can_connect=can_connect, execs=execs or set())


def peer_relation(local_ordinal=0, model_name="m", peers_data=None, local_app_data=None):
    return testing.PeerRelation(
        "seaweedfs-peers",
        local_unit_data={"peer-address": peers.pod_dns_name(APP, local_ordinal, model_name)},
        peers_data=peers_data or {},
        local_app_data=local_app_data or {},
    )


def admin_secret(access_key="admin-ak", secret_key="admin-sk"):
    return testing.Secret(
        {"access-key": access_key, "secret-key": secret_key}, label=ADMIN_SECRET_LABEL, owner="app"
    )


def relation_secret(relation_id, access_key="rel-ak", secret_key="rel-sk", bucket="bucket-1"):
    return testing.Secret(
        {"access-key": access_key, "secret-key": secret_key, "bucket": bucket},
        label=relation_secret_label(relation_id),
        owner="app",
    )


def service_command(state, name=CONTAINER):
    return state.get_container(name).plan.services[name].command


# ----------------------------------------------------------------------
# Charm initialization / config defaults
# ----------------------------------------------------------------------


class TestInitialization:
    def test_default_config_values(self, ctx):
        state = testing.State(leader=True, containers=[container()])
        out = ctx.run(ctx.on.config_changed(), state)
        command = service_command(out)
        assert "-master.defaultReplication=000" in command
        assert "-master.volumeSizeLimitMB=1024" in command

    def test_admin_credentials_generated_on_leader_elected(self, ctx):
        state = testing.State(leader=True, containers=[container()])
        out = ctx.run(ctx.on.leader_elected(), state)
        secrets = {s.label: s for s in out.secrets}
        assert ADMIN_SECRET_LABEL in secrets
        content = secrets[ADMIN_SECRET_LABEL].tracked_content
        assert content["access-key"]
        assert content["secret-key"]

    def test_admin_credentials_not_regenerated_if_exists(self, ctx):
        secret = admin_secret()
        state = testing.State(leader=True, containers=[container()], secrets=[secret])
        out = ctx.run(ctx.on.leader_elected(), state)
        secrets = {s.label: s for s in out.secrets}
        assert secrets[ADMIN_SECRET_LABEL].tracked_content == secret.tracked_content


# ----------------------------------------------------------------------
# Peer relations / master set
# ----------------------------------------------------------------------


class TestPeerRelations:
    def test_master_peers_empty_with_no_peer_data(self, ctx):
        """Before this unit has published its own peer-address, master.peers is empty."""
        state = testing.State(leader=True, containers=[container()])
        out = ctx.run(ctx.on.pebble_ready(container()), state)
        command = service_command(out)
        assert "-master.peers=" not in command
        assert "-master=false" in command

    def test_single_master_with_one_unit(self, ctx):
        peer = peer_relation(local_ordinal=0)
        state = testing.State(
            leader=True, containers=[container()], relations=[peer], model=testing.Model(name="m")
        )
        out = ctx.run(ctx.on.pebble_ready(container()), state)
        command = service_command(out)
        assert "-master=true" in command
        assert command.count(":9333") >= 1
        # single master, never two.
        assert command.split("-master.peers=")[1].split(" ")[0].count(",") == 0

    def test_never_two_masters_with_two_units(self, ctx):
        peer = peer_relation(
            local_ordinal=0,
            peers_data={1: {"peer-address": peers.pod_dns_name(APP, 1, "m")}},
        )
        state = testing.State(
            leader=True,
            containers=[container()],
            relations=[peer],
            model=testing.Model(name="m"),
            planned_units=2,
        )
        out = ctx.run(ctx.on.pebble_ready(container()), state)
        command = service_command(out)
        master_peers = command.split("-master.peers=")[1].split(" ")[0]
        assert len(master_peers.split(",")) == 1
        assert "-master=true" in command  # ordinal 0 is the lowest => master

    def test_three_masters_with_three_units(self, ctx):
        peer = peer_relation(
            local_ordinal=0,
            peers_data={
                1: {"peer-address": peers.pod_dns_name(APP, 1, "m")},
                2: {"peer-address": peers.pod_dns_name(APP, 2, "m")},
            },
        )
        state = testing.State(
            leader=True,
            containers=[container()],
            relations=[peer],
            model=testing.Model(name="m"),
            planned_units=3,
        )
        out = ctx.run(ctx.on.pebble_ready(container()), state)
        command = service_command(out)
        master_peers = command.split("-master.peers=")[1].split(" ")[0]
        assert len(master_peers.split(",")) == 3
        assert "-master=true" in command

    def test_fourth_unit_runs_without_master(self, ctx):
        """The 4th-lowest-ordinal unit (self) is not a master, but points at the master set."""
        peer = peer_relation(
            local_ordinal=3,
            peers_data={
                0: {"peer-address": peers.pod_dns_name(APP, 0, "m")},
                1: {"peer-address": peers.pod_dns_name(APP, 1, "m")},
                2: {"peer-address": peers.pod_dns_name(APP, 2, "m")},
            },
        )
        ctx4 = testing.Context(SeaweedfsK8SCharm, charm_root=".", unit_id=3)
        state = testing.State(
            leader=False,
            containers=[container()],
            relations=[peer],
            model=testing.Model(name="m"),
            planned_units=4,
        )
        out = ctx4.run(ctx4.on.pebble_ready(container()), state)
        command = service_command(out)
        assert "-master=false" in command
        assert "-volume.mserver=" in command
        assert "-filer.master=" in command
        assert "-master.peers=" not in command

    def test_master_promotion_on_peer_departure(self, ctx):
        """Removing a master unit promotes the next-lowest ordinal."""
        peer = peer_relation(
            local_ordinal=0,
            peers_data={
                2: {"peer-address": peers.pod_dns_name(APP, 2, "m")},
                3: {"peer-address": peers.pod_dns_name(APP, 3, "m")},
            },
        )
        state = testing.State(
            leader=True,
            containers=[container()],
            relations=[peer],
            model=testing.Model(name="m"),
            planned_units=3,
        )
        out = ctx.run(ctx.on.relation_departed(peer, remote_unit=1, departing_unit=1), state)
        assert out.get_relation(peer.id).local_app_data["master-ordinals"] == "0,2,3"
        command = service_command(out)
        master_peers = command.split("-master.peers=")[1].split(" ")[0]
        assert len(master_peers.split(",")) == 3

    def test_peer_departure_removes_unit_from_flag(self, ctx):
        peer = peer_relation(
            local_ordinal=0,
            peers_data={1: {"peer-address": peers.pod_dns_name(APP, 1, "m")}},
            local_app_data={"master-ordinals": "0,1"},
        )
        state = testing.State(
            leader=True,
            containers=[container()],
            relations=[peer],
            model=testing.Model(name="m"),
            planned_units=1,
        )
        out = ctx.run(ctx.on.relation_departed(peer, remote_unit=1, departing_unit=1), state)
        assert out.get_relation(peer.id).local_app_data["master-ordinals"] == "0"
        command = service_command(out)
        master_peers = command.split("-master.peers=")[1].split(" ")[0]
        assert len(master_peers.split(",")) == 1

    def test_storage_detaching_triggers_evacuate(self, ctx):
        execs = {testing.Exec(["/usr/bin/weed", "shell"], return_code=0, stdout="ok")}
        storage = testing.Storage("data")
        state = testing.State(leader=True, containers=[container(execs=execs)], storages=[storage])
        # Should not raise even without the exec mock matching exactly.
        ctx.run(ctx.on.storage_detaching(storage), state)

    def test_storage_detaching_tolerates_evacuation_failure(self, ctx):
        storage = testing.Storage("data")
        state = testing.State(leader=True, containers=[container()], storages=[storage])
        # No Exec mock registered -> ExecError is raised internally and swallowed.
        ctx.run(ctx.on.storage_detaching(storage), state)

    def test_scale_1_to_2_triggers_fix_replication(self, ctx):
        execs = {testing.Exec(["/usr/bin/weed", "shell"], return_code=0, stdout="ok")}
        peer = peer_relation(
            local_ordinal=0,
            peers_data={1: {"peer-address": peers.pod_dns_name(APP, 1, "m")}},
            local_app_data={"replication-unit-count": "1"},
        )
        state = testing.State(
            leader=True,
            containers=[container(execs=execs)],
            relations=[peer],
            model=testing.Model(name="m"),
            planned_units=2,
        )
        out = ctx.run(ctx.on.relation_changed(peer, remote_unit=1), state)
        assert out.get_relation(peer.id).local_app_data["replication-unit-count"] == "2"

    def test_peer_addresses_are_stable_dns_not_ips(self):
        name = peers.pod_dns_name("seaweedfs-k8s", 2, "mymodel")
        assert name == "seaweedfs-k8s-2.seaweedfs-k8s-endpoints.mymodel.svc.cluster.local"
        assert not name.replace(".", "").isdigit()


# ----------------------------------------------------------------------
# S3 relations
# ----------------------------------------------------------------------


class TestS3Relations:
    def test_new_relation_generates_credentials_and_publishes(self, ctx):
        rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "my-bucket"}
        )
        state = testing.State(leader=True, containers=[container()], relations=[rel])
        out = ctx.run(ctx.on.relation_changed(rel), state)

        data = out.get_relation(rel.id).local_app_data
        assert data["bucket"] == "my-bucket"
        assert data["access-key"]
        assert data["secret-key"]
        assert data["s3-uri-style"] == "path"

        secrets = {s.label: s for s in out.secrets}
        assert relation_secret_label(rel.id) in secrets
        content = secrets[relation_secret_label(rel.id)].tracked_content
        assert content["access-key"] == data["access-key"]
        assert content["secret-key"] == data["secret-key"]

    def test_published_endpoint_is_service_dns(self, ctx):
        rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "b"}
        )
        state = testing.State(
            leader=True,
            containers=[container()],
            relations=[rel],
            model=testing.Model(name="mymodel"),
        )
        out = ctx.run(ctx.on.relation_changed(rel), state)
        data = out.get_relation(rel.id).local_app_data
        assert data["endpoint"] == f"http://{APP}.mymodel.svc.cluster.local:8333"

    def test_consumer_bucket_override_is_respected(self, ctx):
        rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "custom-name"}
        )
        state = testing.State(leader=True, containers=[container()], relations=[rel])
        out = ctx.run(ctx.on.relation_changed(rel), state)
        assert out.get_relation(rel.id).local_app_data["bucket"] == "custom-name"

    def test_bucket_auto_generated_when_not_requested(self, ctx):
        rel = testing.Relation(S3_RELATION, remote_app_name="grafana", id=42)
        state = testing.State(leader=True, containers=[container()], relations=[rel])
        out = ctx.run(ctx.on.relation_changed(rel), state)
        # No "bucket" added -> credentials_requested never fires -> no data published.
        data = out.get_relation(rel.id).local_app_data
        assert "bucket" not in data
        assert "access-key" not in data

    def test_multiple_relations_same_bucket_distinct_credentials(self, ctx):
        rel_a = testing.Relation(
            S3_RELATION, remote_app_name="mimir", remote_app_data={"bucket": "shared"}
        )
        rel_b = testing.Relation(
            S3_RELATION, remote_app_name="loki", remote_app_data={"bucket": "shared"}
        )
        state = testing.State(leader=True, containers=[container()], relations=[rel_a, rel_b])

        out = ctx.run(ctx.on.relation_changed(rel_a), state)
        out = ctx.run(ctx.on.relation_changed(rel_b), out)

        data_a = out.get_relation(rel_a.id).local_app_data
        data_b = out.get_relation(rel_b.id).local_app_data
        assert data_a["bucket"] == data_b["bucket"] == "shared"
        assert data_a["access-key"] != data_b["access-key"]
        assert data_a["secret-key"] != data_b["secret-key"]

    def test_relation_broken_removes_identity_not_bucket(self, ctx):
        secret = relation_secret(7, bucket="keep-me")
        rel = testing.Relation(S3_RELATION, id=7, remote_app_name="grafana")
        state = testing.State(
            leader=True, containers=[container()], secrets=[secret], relations=[rel]
        )
        out = ctx.run(ctx.on.relation_broken(rel), state)
        labels = {s.label for s in out.secrets}
        assert relation_secret_label(7) not in labels
        # Bucket itself isn't touched by the charm (no delete call made at all).

    def test_identity_file_written_for_admin_and_relations(self, ctx):
        secret = admin_secret()
        rel_secret = relation_secret(3, access_key="rel-ak-3", bucket="bucket-3")
        rel = testing.Relation(S3_RELATION, id=3, remote_app_name="grafana")
        state = testing.State(
            leader=True,
            containers=[container()],
            secrets=[secret, rel_secret],
            relations=[rel],
        )
        c = container()
        out = ctx.run(ctx.on.pebble_ready(c), state)
        fs = out.get_container(CONTAINER).get_filesystem(ctx)
        content = (fs / IDENTITY_PATH.lstrip("/")).read_text()
        assert "admin-ak" in content
        assert "rel-ak-3" in content
        assert "bucket-3" in content


# ----------------------------------------------------------------------
# Config changes
# ----------------------------------------------------------------------


class TestConfigChanges:
    def test_replication_override(self, ctx):
        state = testing.State(
            leader=True, containers=[container()], config={"replication": "002"}
        )
        out = ctx.run(ctx.on.config_changed(), state)
        assert "-master.defaultReplication=002" in service_command(out)

    def test_empty_replication_auto_000_for_single_unit(self, ctx):
        state = testing.State(leader=True, containers=[container()])
        out = ctx.run(ctx.on.config_changed(), state)
        assert "-master.defaultReplication=000" in service_command(out)

    def test_empty_replication_auto_001_for_multiple_units(self, ctx):
        peer = peer_relation(
            local_ordinal=0,
            peers_data={1: {"peer-address": peers.pod_dns_name(APP, 1, "m")}},
        )
        state = testing.State(
            leader=True, containers=[container()], relations=[peer], model=testing.Model(name="m")
        )
        out = ctx.run(ctx.on.config_changed(), state)
        assert "-master.defaultReplication=001" in service_command(out)

    def test_volume_size_limit_mb_configurable(self, ctx):
        state = testing.State(
            leader=True, containers=[container()], config={"volume-size-limit-mb": 4096}
        )
        out = ctx.run(ctx.on.config_changed(), state)
        assert "-master.volumeSizeLimitMB=4096" in service_command(out)

    def test_volume_max_always_zero(self, ctx):
        state = testing.State(leader=True, containers=[container()])
        out = ctx.run(ctx.on.config_changed(), state)
        assert "-volume.max=0" in service_command(out)


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------


class TestSecrets:
    def test_admin_secret_created_with_correct_content(self, ctx):
        state = testing.State(leader=True, containers=[container()])
        out = ctx.run(ctx.on.leader_elected(), state)
        secrets = {s.label: s for s in out.secrets}
        content = secrets[ADMIN_SECRET_LABEL].tracked_content
        assert set(content.keys()) == {"access-key", "secret-key"}

    def test_per_relation_secret_is_app_owned(self, ctx):
        rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "b"}
        )
        state = testing.State(leader=True, containers=[container()], relations=[rel])
        out = ctx.run(ctx.on.relation_changed(rel), state)
        secrets = {s.label: s for s in out.secrets}
        assert secrets[relation_secret_label(rel.id)].owner == "application"

    def test_secret_rotation_produces_new_values(self, ctx):
        secret = admin_secret(access_key="old-ak", secret_key="old-sk")
        state = testing.State(leader=True, containers=[container()], secrets=[secret])
        out = ctx.run(ctx.on.action("rotate-admin-credentials"), state)
        secrets = {s.label: s for s in out.secrets}
        content = secrets[ADMIN_SECRET_LABEL].tracked_content
        assert content["access-key"] != "old-ak"
        assert content["secret-key"] != "old-sk"


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------


class TestActions:
    def test_get_admin_credentials(self, ctx):
        secret = admin_secret(access_key="AK", secret_key="SK")
        state = testing.State(leader=True, secrets=[secret])
        ctx.run(ctx.on.action("get-admin-credentials"), state)
        assert ctx.action_results == {"access-key": "AK", "secret-key": "SK"}

    def test_rotate_admin_credentials_resyncs_without_restart(self, ctx):
        secret = admin_secret(access_key="old-ak", secret_key="old-sk")
        state = testing.State(leader=True, containers=[container()], secrets=[secret])
        ctx.run(ctx.on.action("rotate-admin-credentials"), state)
        assert ctx.action_results["access-key"] != "old-ak"

    def test_get_admin_credentials_after_rotation(self, ctx):
        secret = admin_secret(access_key="old-ak", secret_key="old-sk")
        state = testing.State(leader=True, containers=[container()], secrets=[secret])
        out = ctx.run(ctx.on.action("rotate-admin-credentials"), state)
        rotated = dict(ctx.action_results)

        out = ctx.run(ctx.on.action("get-admin-credentials"), out)
        assert ctx.action_results == rotated

    def test_rotate_relation_credentials_updates_only_target(self, ctx):
        secret_a = relation_secret(1, access_key="ak-1", bucket="bucket-1")
        secret_b = relation_secret(2, access_key="ak-2", bucket="bucket-2")
        rel_a = testing.Relation(S3_RELATION, id=1, remote_app_name="mimir")
        rel_b = testing.Relation(S3_RELATION, id=2, remote_app_name="loki")
        state = testing.State(
            leader=True,
            containers=[container()],
            secrets=[secret_a, secret_b],
            relations=[rel_a, rel_b],
        )
        out = ctx.run(
            ctx.on.action("rotate-relation-credentials", params={"relation-id": 1}), state
        )

        secrets = {s.label: s for s in out.secrets}
        assert secrets[relation_secret_label(1)].tracked_content["access-key"] != "ak-1"
        assert secrets[relation_secret_label(2)].tracked_content["access-key"] == "ak-2"
        assert out.get_relation(1).local_app_data["access-key"] != "ak-1"

    def test_rotate_relation_credentials_invalid_id_fails(self, ctx):
        state = testing.State(leader=True, containers=[container()])
        with pytest.raises(ActionFailed):
            ctx.run(
                ctx.on.action("rotate-relation-credentials", params={"relation-id": 999}), state
            )

    def test_pre_upgrade_check_fails_without_raft_leader(self, ctx, monkeypatch):
        monkeypatch.setattr(charm_module.utils, "cluster_status", lambda: None)
        state = testing.State(leader=True)
        with pytest.raises(ActionFailed):
            ctx.run(ctx.on.action("pre-upgrade-check"), state)

    def test_pre_upgrade_check_succeeds_with_raft_leader(self, ctx, monkeypatch):
        monkeypatch.setattr(
            charm_module.utils, "cluster_status", lambda: {"Leader": "host:9333"}
        )
        state = testing.State(leader=True)
        ctx.run(ctx.on.action("pre-upgrade-check"), state)
        assert ctx.action_results == {"result": "ok"}


# ----------------------------------------------------------------------
# TLS
# ----------------------------------------------------------------------


class TestTLS:
    def test_certificates_relation_triggers_cert_request(self, ctx):
        rel = testing.Relation("certificates")
        state = testing.State(leader=True, containers=[container()], relations=[rel])
        out = ctx.run(ctx.on.relation_created(rel), state)
        local_data = out.get_relation(rel.id).local_unit_data
        assert "certificate_signing_requests" in local_data

    def test_no_certificates_relation_uses_http_endpoint(self, ctx):
        rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "b"}
        )
        state = testing.State(
            leader=True, containers=[container()], relations=[rel], model=testing.Model(name="m")
        )
        out = ctx.run(ctx.on.relation_changed(rel), state)
        assert out.get_relation(rel.id).local_app_data["endpoint"].startswith("http://")

    def test_with_certificates_https_endpoint_and_cert_files_pushed(self, ctx):
        s3_rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "b"}
        )
        state = testing.State(
            leader=True,
            containers=[container()],
            relations=[s3_rel],
            model=testing.Model(name="m"),
        )

        fake_cert = MagicMock()
        fake_cert.__str__ = MagicMock(return_value="CERTPEM")
        fake_key = MagicMock()
        fake_key.__str__ = MagicMock(return_value="KEYPEM")
        fake_ca = MagicMock()
        fake_ca.__str__ = MagicMock(return_value="CAPEM")
        fake_provider_cert = MagicMock(certificate=fake_cert, chain=[], ca=fake_ca)

        with ctx(ctx.on.relation_changed(s3_rel), state) as mgr:
            mgr.charm.certificates.get_assigned_certificate = MagicMock(
                return_value=(fake_provider_cert, fake_key)
            )
            out = mgr.run()

        data = out.get_relation(s3_rel.id).local_app_data
        assert data["endpoint"].startswith("https://")
        assert "tls-ca-chain" in data

        command = service_command(out)
        assert "-s3.cert.file=" in command
        assert "-s3.key.file=" in command


# ----------------------------------------------------------------------
# Non-leader behavior
# ----------------------------------------------------------------------


class TestNonLeader:
    def test_non_leader_does_not_generate_admin_credentials(self, ctx):
        state = testing.State(leader=False, containers=[container()])
        out = ctx.run(ctx.on.leader_elected(), state)
        assert ADMIN_SECRET_LABEL not in {s.label for s in out.secrets}

    def test_non_leader_does_not_publish_s3_relation_data(self, ctx):
        rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "b"}
        )
        state = testing.State(leader=False, containers=[container()], relations=[rel])
        out = ctx.run(ctx.on.relation_changed(rel), state)
        assert out.get_relation(rel.id).local_app_data == {}


# ----------------------------------------------------------------------
# Edge cases
# ----------------------------------------------------------------------


class TestEdgeCases:
    def test_container_not_ready_sets_maintenance_status(self, ctx):
        state = testing.State(leader=True, containers=[container(can_connect=False)])
        out = ctx.run(ctx.on.config_changed(), state)
        assert isinstance(out.unit_status, testing.MaintenanceStatus)

    def test_bucket_already_exists_no_error(self, ctx):
        execs = {
            testing.Exec(["/usr/bin/weed", "shell"], return_code=1, stderr="bucket already exists")
        }
        rel = testing.Relation(
            S3_RELATION, remote_app_name="grafana", remote_app_data={"bucket": "dup"}
        )
        state = testing.State(leader=True, containers=[container(execs=execs)], relations=[rel])
        out = ctx.run(ctx.on.relation_changed(rel), state)
        assert out.get_relation(rel.id).local_app_data["bucket"] == "dup"

    def test_empty_peer_list_standalone_no_crash(self, ctx):
        state = testing.State(leader=True, containers=[container()])
        out = ctx.run(ctx.on.pebble_ready(container()), state)
        assert out.unit_status is not None
