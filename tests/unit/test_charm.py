# Copyright 2025 him
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import types

import pytest
from ops.testing import Harness

import charm as charm_module
from charm import FILER_PORT, MASTER_PORT, S3_PORT, SeaweedfsK8S

ADDRESS = "seaweedfs-k8s-0.seaweedfs-k8s-endpoints.default.svc.cluster.local"


@pytest.fixture
def harness(monkeypatch):
    # No real S3 endpoint, and a deterministic in-cluster address.
    monkeypatch.setattr(
        charm_module,
        "create_bucket",
        lambda *args, **kwargs: types.SimpleNamespace(status=200),
    )
    monkeypatch.setattr(charm_module.socket, "getfqdn", lambda: ADDRESS)

    harness = Harness(SeaweedfsK8S)
    harness.begin()
    harness.set_can_connect("seaweedfs", True)
    harness.add_relation("cluster", "seaweedfs-k8s")
    harness.set_leader(True)
    return harness


def test_layer_runs_the_whole_cluster_on_the_first_unit(harness):
    services = harness.get_container_pebble_plan("seaweedfs").services

    assert set(services) == {"master", "volume", "filer", "s3"}
    assert f"-port={MASTER_PORT}" in services["master"].command
    assert f"-mserver={ADDRESS}:{MASTER_PORT}" in services["volume"].command
    assert "-defaultStoreDir=/data/filer" in services["filer"].command


def test_s3_port_is_passed_explicitly_to_avoid_env_var_collision(harness):
    # When the application is named "s3", Juju injects S3_PORT and SeaweedFS
    # reads it as the s3.port flag; an explicit flag takes precedence over the
    # environment variable.
    s3 = harness.get_container_pebble_plan("seaweedfs").services["s3"]

    assert f"-port={S3_PORT}" in s3.command
    assert f"-filer={ADDRESS}:{FILER_PORT}" in s3.command


def test_single_unit_disables_replication(harness):
    master = harness.get_container_pebble_plan("seaweedfs").services["master"]

    assert "-defaultReplication=000" in master.command


def test_relation_data_exposes_endpoint_bucket_and_credentials(harness):
    relation_id = harness.add_relation("s3-credentials", "consumer")

    data = harness.get_relation_data(relation_id, "seaweedfs-k8s")

    assert data["bucket"] == f"s3-credentials-{relation_id}"
    assert data["endpoint"] == f"http://{ADDRESS}:{S3_PORT}"
    assert data["access-key"]
    assert data["secret-key"]


def test_credentials_are_generated_once(harness):
    peer = harness.charm.model.get_relation("cluster")
    first_access = peer.data[harness.charm.app]["admin-access-key"]

    harness.charm._reconcile(None)

    assert peer.data[harness.charm.app]["admin-access-key"] == first_access
