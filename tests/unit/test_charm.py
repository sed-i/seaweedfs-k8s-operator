#!/usr/bin/env python3
"""Unit tests for the SeaweedFS charm."""

import json
import unittest
from unittest.mock import MagicMock, patch

import ops
import ops.testing

from charm import SeaweedfsK8S
from config import S3Config, S3Identity
from dashboard import build_dashboard
from utils import generate_credential_pair, get_cluster_peers


class TestS3Identity(unittest.TestCase):
    """Tests for S3Identity."""

    def test_full_identity(self):
        identity = S3Identity(
            name="test-user",
            access_key="access",
            secret_key="secret",
            actions=["Read", "Write"],
            allowed_buckets=["bucket1"],
        )
        d = identity.to_dict()
        self.assertEqual(d["name"], "test-user")
        self.assertEqual(d["credentials"], [{"accessKey": "access", "secretKey": "secret"}])
        self.assertEqual(d["actions"], ["Read", "Write"])
        self.assertEqual(d["allowed_buckets"], ["bucket1"])

    def test_no_credentials(self):
        identity = S3Identity(name="anon")
        d = identity.to_dict()
        self.assertNotIn("credentials", d)

    def test_default_actions_and_buckets(self):
        identity = S3Identity(name="test")
        d = identity.to_dict()
        self.assertEqual(d["actions"], ["Admin", "Read", "List", "Tagging", "Write"])
        self.assertEqual(d["allowed_buckets"], ["*"])


class TestS3Config(unittest.TestCase):
    """Tests for S3Config."""

    def test_empty_config(self):
        result = json.loads(S3Config().build())
        self.assertEqual(result, {"identities": []})

    def test_admin_identity(self):
        cfg = S3Config()
        cfg.add_admin_identity("admin-key", "admin-secret")
        result = json.loads(cfg.build())
        ident = result["identities"][0]
        self.assertEqual(ident["name"], "admin")
        self.assertEqual(ident["actions"], ["*"])
        self.assertEqual(ident["allowed_buckets"], ["*"])

    def test_anonymous_identity(self):
        cfg = S3Config()
        cfg.add_anonymous_identity()
        result = json.loads(cfg.build())
        self.assertEqual(result["identities"][0]["name"], "anonymous")

    def test_relation_identity(self):
        cfg = S3Config()
        cfg.add_relation_identity(42, "rk", "rs", "my-bucket")
        result = json.loads(cfg.build())
        ident = result["identities"][0]
        self.assertEqual(ident["name"], "relation-42")
        self.assertEqual(ident["allowed_buckets"], ["my-bucket"])

    def test_multiple_identities(self):
        cfg = S3Config()
        cfg.add_admin_identity("ak", "sk")
        cfg.add_anonymous_identity()
        cfg.add_relation_identity(1, "r1", "s1", "b1")
        cfg.add_relation_identity(2, "r2", "s2", "b2")
        result = json.loads(cfg.build())
        self.assertEqual(len(result["identities"]), 4)

    def test_output_is_valid_json(self):
        cfg = S3Config()
        cfg.add_admin_identity("ak", "sk")
        json.loads(cfg.build())


class TestUtils(unittest.TestCase):
    """Tests for utility functions."""

    def test_credential_pair_random(self):
        a, b = generate_credential_pair()
        c, d = generate_credential_pair()
        self.assertNotEqual((a, b), (c, d))
        self.assertTrue(a.startswith("SK"))
        self.assertGreater(len(a), 0)
        self.assertGreater(len(b), 0)

    def test_get_cluster_peers_excludes_self(self):
        self.assertEqual(
            get_cluster_peers(["10.0.0.1:9333", "10.0.0.2:9333"], "10.0.0.1:9333"),
            ["10.0.0.2:9333"],
        )

    def test_get_cluster_peers_empty(self):
        self.assertEqual(get_cluster_peers([], "10.0.0.1:9333"), [])

    def test_get_cluster_peers_deduplicates(self):
        self.assertEqual(
            get_cluster_peers(["a:9333", "a:9333", "b:9333"], "c:9333"),
            ["a:9333", "b:9333"],
        )

    def test_get_cluster_peers_filters_blanks(self):
        self.assertEqual(
            get_cluster_peers(["", "10.0.0.1:9333", ""], "10.0.0.2:9333"),
            ["10.0.0.1:9333"],
        )


class TestDashboard(unittest.TestCase):
    """Tests for Grafana dashboard."""

    def test_valid_json(self):
        parsed = json.loads(build_dashboard())
        self.assertEqual(parsed["title"], "SeaweedFS")
        self.assertGreater(len(parsed["panels"]), 0)


# ---------------------------------------------------------------------------
# Charm tests
# ---------------------------------------------------------------------------

META = """
name: seaweedfs-k8s
containers:
  seaweedfs:
    resource: seaweedfs-image
storage:
  data:
    type: filesystem
peers:
  seaweedfs-peers:
    interface: seaweedfs_peers
provides:
  s3-credentials:
    interface: s3
  metrics-endpoint:
    interface: prometheus_scrape
  grafana-dashboard:
    interface: grafana_dashboard
requires: {}
"""


class CharmTestBase(unittest.TestCase):
    """Base class for charm tests."""

    def setUp(self):
        self.harness = ops.testing.Harness(SeaweedfsK8S, meta=META)
        self.addCleanup(self.harness.cleanup)
        self.harness.set_model_name("test-model")
        self.harness.set_leader(True)
        self.harness.begin()

    def start_container(self):
        self.harness.set_can_connect("seaweedfs", True)
        self.harness.container_pebble_ready("seaweedfs")

    @property
    def container(self):
        return self.harness.charm.unit.get_container("seaweedfs")

    def add_s3_relation(self, remote_app="requirer"):
        rel_id = self.harness.add_relation("s3-credentials", remote_app)
        self.harness.add_relation_unit(rel_id, f"{remote_app}/0")
        self.harness.update_relation_data(rel_id, remote_app, {})
        return rel_id

    def add_peer_unit(self, rel_id, unit_name, master_addr=None):
        self.harness.add_relation_unit(rel_id, unit_name)
        if master_addr:
            self.harness.update_relation_data(
                rel_id, unit_name,
                {"master-address": master_addr, "metrics-address": master_addr.split(":")[0]},
            )


class TestCharmInit(CharmTestBase):
    """Tests for initial deployment and credentials."""

    @patch("charm.check_s3_health", return_value=False)
    def test_waiting_for_container(self, _mock):
        self.harness.charm.on.config_changed.emit()
        self.assertIsInstance(self.harness.charm.unit.status, ops.MaintenanceStatus)

    @patch("charm.check_s3_health", return_value=False)
    def test_credentials_generated(self, _mock):
        self.start_container()
        secret = self.harness.model.get_secret(label="s3-admin")
        content = secret.get_content(refresh=True)
        self.assertIn("access-key", content)
        self.assertIn("secret-key", content)
        self.assertTrue(content["access-key"].startswith("SK"))

    @patch("charm.check_s3_health", return_value=False)
    def test_custom_admin_credentials(self, _mock):
        self.harness.update_config({
            "admin-access-key": "my-key",
            "admin-secret-key": "my-secret",
        })
        self.start_container()
        secret = self.harness.model.get_secret(label="s3-admin")
        content = secret.get_content(refresh=True)
        self.assertEqual(content["access-key"], "my-key")
        self.assertEqual(content["secret-key"], "my-secret")

    @patch("charm.check_s3_health", return_value=False)
    def test_credentials_persist(self, _mock):
        self.start_container()
        first = self.harness.model.get_secret(label="s3-admin").get_content(refresh=True)
        self.harness.charm.on.config_changed.emit()
        second = self.harness.model.get_secret(label="s3-admin").get_content(refresh=True)
        self.assertEqual(first["access-key"], second["access-key"])

    @patch("charm.check_s3_health", return_value=False)
    def test_action_before_start_fails(self, _mock):
        with self.assertRaises(ops.testing.ActionFailed):
            self.harness.run_action("get-admin-credentials")

    @patch("charm.check_s3_health", return_value=False)
    def test_get_admin_credentials_action(self, _mock):
        self.start_container()
        output = self.harness.run_action("get-admin-credentials")
        self.assertIn("access-key", output.results)
        self.assertIn("secret-key", output.results)
        self.assertIn("endpoint", output.results)
        self.assertTrue(output.results["access-key"].startswith("SK"))


class TestCharmConfig(CharmTestBase):
    """Tests for config options."""

    @patch("charm.check_s3_health", return_value=True)
    def test_defaults(self, _mock):
        self.start_container()
        c = self.harness.charm
        self.assertEqual(c._volume_size_limit_mb, 1024)
        self.assertEqual(c._filer_max_mb, 64)
        self.assertEqual(c._replication, "001")
        self.assertTrue(c._is_metrics_enabled)

    @patch("charm.check_s3_health", return_value=True)
    def test_custom_values(self, _mock):
        self.harness.update_config({
            "volume-size-limit-mb": 2048,
            "filer-max-mb": 128,
            "replication": "000",
            "metrics": False,
        })
        self.start_container()
        c = self.harness.charm
        self.assertEqual(c._volume_size_limit_mb, 2048)
        self.assertEqual(c._filer_max_mb, 128)
        self.assertEqual(c._replication, "000")
        self.assertFalse(c._is_metrics_enabled)


class TestCharmWorkload(CharmTestBase):
    """Tests for Pebble layer and S3 config."""

    @patch("charm.check_s3_health", return_value=True)
    def test_s3_config_pushed(self, _mock):
        self.start_container()
        config = json.loads(self.container.pull("/config/s3.json").read())
        identities = {i["name"] for i in config["identities"]}
        self.assertIn("admin", identities)
        self.assertIn("anonymous", identities)

    @patch("charm.check_s3_health", return_value=True)
    def test_pebble_service_exists(self, _mock):
        self.start_container()
        self.assertIn("seaweedfs", self.container.get_plan().services)

    @patch("charm.check_s3_health", return_value=True)
    def test_no_peers_flag_when_single(self, _mock):
        self.start_container()
        cmd = self.container.get_plan().services["seaweedfs"].command
        self.assertNotIn("-master.peers", cmd)

    @patch("charm.check_s3_health", return_value=True)
    def test_metrics_in_command(self, _mock):
        self.start_container()
        cmd = self.container.get_plan().services["seaweedfs"].command
        self.assertIn("-metricsPort=9321", cmd)

    @patch("charm.check_s3_health", return_value=True)
    def test_metrics_disabled_in_command(self, _mock):
        self.harness.update_config({"metrics": False})
        self.start_container()
        cmd = self.container.get_plan().services["seaweedfs"].command
        self.assertNotIn("-metricsPort", cmd)

    @patch("charm.check_s3_health", return_value=True)
    def test_replication_in_command(self, _mock):
        self.start_container()
        cmd = self.container.get_plan().services["seaweedfs"].command
        self.assertIn("-master.defaultReplication=001", cmd)

    @patch("charm.check_s3_health", return_value=True)
    def test_health_checks(self, _mock):
        self.start_container()
        plan = self.container.get_plan()
        self.assertIn("s3-online", plan.checks)
        self.assertIn("master-online", plan.checks)


class TestCharmCluster(CharmTestBase):
    """Tests for peer clustering."""

    @patch("charm.check_s3_health", return_value=True)
    def test_publishes_own_address(self, _mock):
        self.start_container()
        rel_id = self.harness.add_relation("seaweedfs-peers", "seaweedfs-k8s")
        self.harness.add_relation_unit(rel_id, "seaweedfs-k8s/1")
        self.harness.charm._publish_peer_data()
        relation = self.harness.charm.model.get_relation("seaweedfs-peers")
        assert relation is not None
        local_data = relation.data[self.harness.charm.unit]
        self.assertIn("master-address", local_data)
        self.assertIn("metrics-address", local_data)

    @patch("charm.check_s3_health", return_value=True)
    def test_peers_flag_with_remote_unit(self, _mock):
        self.start_container()
        rel_id = self.harness.add_relation("seaweedfs-peers", "seaweedfs-k8s")
        self.add_peer_unit(rel_id, "seaweedfs-k8s/1", "10.0.0.2:9333")
        cmd = self.container.get_plan().services["seaweedfs"].command
        self.assertIn("-master.peers=10.0.0.2:9333", cmd)

    @patch("charm.check_s3_health", return_value=True)
    def test_peers_flag_excludes_own_address(self, _mock):
        self.start_container()
        my_addr = self.harness.charm._my_address
        rel_id = self.harness.add_relation("seaweedfs-peers", "seaweedfs-k8s")
        self.add_peer_unit(rel_id, "seaweedfs-k8s/1", my_addr)
        cmd = self.container.get_plan().services["seaweedfs"].command
        self.assertNotIn("-master.peers", cmd)

    @patch("charm.check_s3_health", return_value=True)
    def test_cluster_count_in_status(self, _mock):
        self.start_container()
        rel_id = self.harness.add_relation("seaweedfs-peers", "seaweedfs-k8s")
        self.add_peer_unit(rel_id, "seaweedfs-k8s/1", "10.0.0.2:9333")
        self.add_peer_unit(rel_id, "seaweedfs-k8s/2", "10.0.0.3:9333")
        self.assertIn("3 units", self.harness.charm.unit.status.message)


class TestCharmS3Relations(CharmTestBase):
    """Tests for S3 relations."""

    @patch("charm.create_bucket")
    @patch("charm.check_s3_health", return_value=True)
    def test_publishes_relation_data(self, _health, mock_create):
        mock_create.return_value = MagicMock(status=200)
        self.start_container()
        rel_id = self.add_s3_relation("myapp")
        data = self.harness.get_relation_data(rel_id, self.harness.charm.app)
        self.assertIn("access-key", data)
        self.assertIn("secret-key", data)
        self.assertIn("bucket", data)
        self.assertIn("endpoint", data)
        self.assertTrue(data["access-key"].startswith("SK"))

    @patch("charm.create_bucket")
    @patch("charm.check_s3_health", return_value=True)
    def test_unique_credentials_per_relation(self, _health, mock_create):
        mock_create.return_value = MagicMock(status=200)
        self.start_container()
        r1 = self.add_s3_relation("app1")
        r2 = self.add_s3_relation("app2")
        d1 = self.harness.get_relation_data(r1, self.harness.charm.app)
        d2 = self.harness.get_relation_data(r2, self.harness.charm.app)
        self.assertNotEqual(d1["access-key"], d2["access-key"])
        self.assertNotEqual(d1["secret-key"], d2["secret-key"])
        self.assertNotEqual(d1["bucket"], d2["bucket"])

    @patch("charm.create_bucket")
    @patch("charm.check_s3_health", return_value=True)
    def test_identity_in_s3_config(self, _health, mock_create):
        mock_create.return_value = MagicMock(status=200)
        self.start_container()
        rel_id = self.add_s3_relation("app")
        config = json.loads(self.container.pull("/config/s3.json").read())
        names = {i["name"] for i in config["identities"]}
        self.assertIn(f"relation-{rel_id}", names)

    @patch("charm.create_bucket")
    @patch("charm.check_s3_health", return_value=True)
    def test_endpoint_uses_service_dns(self, _health, mock_create):
        mock_create.return_value = MagicMock(status=200)
        self.start_container()
        rel_id = self.add_s3_relation("app")
        data = self.harness.get_relation_data(rel_id, self.harness.charm.app)
        self.assertIn("seaweedfs-k8s.test-model.svc.cluster.local:8333", data["endpoint"])


class TestCharmCOS(CharmTestBase):
    """Tests for COS integration."""

    @patch("charm.check_s3_health", return_value=True)
    def test_scrape_jobs_published(self, _mock):
        self.start_container()
        rel_id = self.harness.add_relation("metrics-endpoint", "prom")
        self.harness.add_relation_unit(rel_id, "prom/0")
        data = self.harness.get_relation_data(rel_id, self.harness.charm.app)
        self.assertIn("scrape_jobs", data)
        jobs = json.loads(data["scrape_jobs"])
        self.assertEqual(jobs[0]["metrics_path"], "/metrics")

    @patch("charm.check_s3_health", return_value=True)
    def test_no_scrape_jobs_when_metrics_disabled(self, _mock):
        self.harness.update_config({"metrics": False})
        self.start_container()
        rel_id = self.harness.add_relation("metrics-endpoint", "prom")
        self.harness.add_relation_unit(rel_id, "prom/0")
        data = self.harness.get_relation_data(rel_id, self.harness.charm.app)
        self.assertNotIn("scrape_jobs", data)

    @patch("charm.check_s3_health", return_value=True)
    def test_dashboard_published(self, _mock):
        self.start_container()
        rel_id = self.harness.add_relation("grafana-dashboard", "grafana")
        self.harness.add_relation_unit(rel_id, "grafana/0")
        data = self.harness.get_relation_data(rel_id, self.harness.charm.app)
        self.assertIn("dashboards", data)


class TestCharmNonLeader(CharmTestBase):
    """Tests for non-leader behavior."""

    @patch("charm.check_s3_health", return_value=True)
    def test_does_not_generate_credentials(self, _mock):
        self.harness.set_leader(False)
        self.start_container()
        with self.assertRaises(ops.model.SecretNotFoundError):
            self.harness.model.get_secret(label="s3-admin")

    @patch("charm.check_s3_health", return_value=True)
    def test_reads_existing_secrets(self, _mock):
        self.start_container()
        self.harness.set_leader(False)
        secret = self.harness.model.get_secret(label="s3-admin")
        content = secret.get_content(refresh=True)
        self.assertIn("access-key", content)

    @patch("charm.create_bucket")
    @patch("charm.check_s3_health", return_value=True)
    def test_does_not_publish_relation_data(self, _health, mock_create):
        mock_create.return_value = MagicMock(status=200)
        self.start_container()
        self.harness.set_leader(False)
        rel_id = self.add_s3_relation("app")
        data = self.harness.get_relation_data(rel_id, self.harness.charm.app)
        self.assertNotIn("access-key", data)


if __name__ == "__main__":
    unittest.main()
