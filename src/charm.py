#!/usr/bin/env python3
"""A scalable S3-compatible object store charm powered by SeaweedFS."""

import hashlib
import json
import logging
import os
import re
import socket
from typing import Dict, List, Optional

import ops
from ops.pebble import APIError, Layer

from config import S3Config
from dashboard import build_dashboard
from utils import check_s3_health, create_bucket, generate_credential_pair, get_cluster_peers

logger = logging.getLogger(__name__)


class SeaweedfsK8S(ops.CharmBase):
    """A scalable S3-compatible object store charm."""

    container_name = "seaweedfs"
    _storage_path = "/data"
    _config_path = "/config/s3.json"
    _s3_port = 8333
    _master_port = 9333
    _filer_port = 8888
    _metrics_port = 9321
    _admin_secret_label = "s3-admin"
    _relation_secret_label_fmt = "s3-rel-{}"
    _peer_relation_name = "seaweedfs-peers"
    _s3_relation_name = "s3-credentials"

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._container = self.unit.get_container(self.container_name)
        framework.observe(self.on.seaweedfs_pebble_ready, self._on_reconcile)
        framework.observe(self.on.config_changed, self._on_reconcile)
        framework.observe(self.on.leader_elected, self._on_reconcile)
        framework.observe(self.on.upgrade_charm, self._on_reconcile)
        framework.observe(self.on.seaweedfs_peers_relation_changed, self._on_reconcile)
        framework.observe(self.on.seaweedfs_peers_relation_departed, self._on_reconcile)
        framework.observe(self.on.seaweedfs_peers_relation_created, self._on_reconcile)
        framework.observe(self.on.s3_credentials_relation_changed, self._on_reconcile)
        framework.observe(self.on.s3_credentials_relation_broken, self._on_reconcile)
        framework.observe(self.on.s3_credentials_relation_created, self._on_reconcile)
        framework.observe(self.on.metrics_endpoint_relation_joined, self._on_reconcile)
        framework.observe(self.on.metrics_endpoint_relation_changed, self._on_reconcile)
        framework.observe(self.on.grafana_dashboard_relation_joined, self._on_reconcile)
        framework.observe(self.on.grafana_dashboard_relation_changed, self._on_reconcile)
        framework.observe(self.on.get_admin_credentials_action, self._on_get_admin_credentials)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_reconcile(self, event: ops.EventBase) -> None:
        if not self._container.can_connect():
            self.unit.status = ops.MaintenanceStatus("Waiting for container")
            return

        self._ensure_credentials()
        self._publish_peer_data()
        self._configure_workload()
        self.unit.set_workload_version(self._seaweedfs_version or "")
        self._update_s3_relations()
        self._update_cos_relations()
        self._set_status()

    def _on_get_admin_credentials(self, event: ops.ActionEvent) -> None:
        try:
            secret = self.model.get_secret(label=self._admin_secret_label)
            content = secret.get_content(refresh=True)
        except ops.SecretNotFoundError:
            event.fail("Admin credentials not yet generated")
            return
        event.set_results({
            "access-key": content["access-key"],
            "secret-key": content["secret-key"],
            "endpoint": self._s3_endpoint_url,
        })

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def _ensure_credentials(self) -> None:
        if not self.unit.is_leader():
            return

        try:
            self.model.get_secret(label=self._admin_secret_label)
        except ops.SecretNotFoundError:
            access_key, secret_key = self._resolve_admin_credentials()
            self.app.add_secret(
                {"access-key": access_key, "secret-key": secret_key},
                label=self._admin_secret_label,
            )
            logger.info("Generated new admin S3 credentials")

    def _resolve_admin_credentials(self) -> tuple:
        config_key = self.model.config.get("admin-access-key", "")
        config_secret = self.model.config.get("admin-secret-key", "")
        if config_key and config_secret:
            return config_key, config_secret
        return generate_credential_pair()

    def _get_admin_credentials(self) -> Dict[str, str]:
        try:
            secret = self.model.get_secret(label=self._admin_secret_label)
            return secret.get_content(refresh=True)
        except ops.SecretNotFoundError:
            return {}

    def _get_or_create_relation_credentials(self, relation_id: int) -> Dict[str, str]:
        label = self._relation_secret_label_fmt.format(relation_id)
        try:
            secret = self.model.get_secret(label=label)
            return secret.get_content(refresh=True)
        except ops.SecretNotFoundError:
            if not self.unit.is_leader():
                return {}
            access_key, secret_key = generate_credential_pair()
            self.app.add_secret(
                {"access-key": access_key, "secret-key": secret_key},
                label=label,
            )
            logger.info("Generated credentials for relation %d", relation_id)
            return {"access-key": access_key, "secret-key": secret_key}

    # ------------------------------------------------------------------
    # Cluster peers
    # ------------------------------------------------------------------

    def _publish_peer_data(self) -> None:
        peer_relation = self.model.get_relation(self._peer_relation_name)
        if not peer_relation:
            return
        peer_relation.data[self.unit].update({
            "master-address": self._my_address,
            "metrics-address": self._metrics_address,
        })

    def _get_peer_addresses(self) -> List[str]:
        peer_relation = self.model.get_relation(self._peer_relation_name)
        if not peer_relation:
            return []
        addresses = []
        for unit in peer_relation.units:
            addr = peer_relation.data[unit].get("master-address", "")
            if addr:
                addresses.append(addr)
        return sorted(set(addresses))

    @property
    def _other_peers_str(self) -> str:
        all_peers = self._get_peer_addresses()
        others = get_cluster_peers(all_peers, self._my_address)
        return ",".join(others)

    @property
    def _my_address(self) -> str:
        return f"{socket.getfqdn()}:{self._master_port}"

    @property
    def _metrics_address(self) -> str:
        return socket.getfqdn()

    # ------------------------------------------------------------------
    # Workload configuration
    # ------------------------------------------------------------------

    def _configure_workload(self) -> None:
        s3_config = self._build_s3_config()
        config_hash = hashlib.sha512(s3_config.encode()).hexdigest()
        self._container.push(self._config_path, s3_config, make_dirs=True)
        self._container.add_layer(
            self.container_name,
            self._pebble_layer(config_hash),
            combine=True,
        )
        self._container.replan()

    def _build_s3_config(self) -> str:
        cfg = S3Config()
        admin_creds = self._get_admin_credentials()
        if admin_creds:
            cfg.add_admin_identity(
                access_key=admin_creds["access-key"],
                secret_key=admin_creds["secret-key"],
            )
        cfg.add_anonymous_identity()

        for relation in self.model.relations.get(self._s3_relation_name, []):
            creds = self._get_or_create_relation_credentials(relation.id)
            if not creds:
                continue
            bucket = f"{self._s3_relation_name}-{relation.id}"
            cfg.add_relation_identity(
                relation_id=relation.id,
                access_key=creds["access-key"],
                secret_key=creds["secret-key"],
                bucket=bucket,
            )

        return cfg.build()

    def _pebble_layer(self, sentinel: str) -> Layer:
        command = (
            "/usr/bin/weed server "
            "-filer "
            f"-filer.maxMB={self._filer_max_mb} "
            f"-dir={self._storage_path} "
            "-s3 "
            f"-s3.config={self._config_path} "
            "-ip.bind=0.0.0.0 "
            "-master.electionTimeout=1s "
            f"-master.volumeSizeLimitMB={self._volume_size_limit_mb} "
            "-volume.max=0 "
            f"-master.defaultReplication={self._replication} "
        )
        if self._is_metrics_enabled:
            command += f"-metricsPort={self._metrics_port} "
        peers = self._other_peers_str
        if peers:
            command += f"-master.peers={peers} "

        return Layer({
            "summary": "seaweedfs-k8s layer",
            "description": "seaweedfs-k8s layer",
            "services": {
                self.container_name: {
                    "override": "replace",
                    "summary": "seaweedfs service",
                    "command": command.strip(),
                    "startup": "enabled",
                    "environment": {
                        "_config_hash": sentinel,
                        "https_proxy": os.environ.get("JUJU_CHARM_HTTPS_PROXY", ""),
                        "http_proxy": os.environ.get("JUJU_CHARM_HTTP_PROXY", ""),
                        "no_proxy": os.environ.get("JUJU_CHARM_NO_PROXY", ""),
                        "WEED_MASTER_VOLUME_GROWTH_COPY_OTHER": "1",
                        "WEED_MASTER_VOLUME_GROWTH_COPY_1": "1",
                        "WEED_MASTER_VOLUME_GROWTH_COPY_2": "1",
                        "WEED_MASTER_VOLUME_GROWTH_COPY_3": "1",
                    },
                },
            },
            "checks": {
                "s3-online": {
                    "override": "replace",
                    "level": "ready",
                    "threshold": 1,
                    "http": {"url": f"http://localhost:{self._s3_port}"},
                },
                "master-online": {
                    "override": "replace",
                    "level": "ready",
                    "threshold": 1,
                    "http": {"url": f"http://localhost:{self._master_port}/cluster/healthz"},
                },
            },
        })

    # ------------------------------------------------------------------
    # S3 relation management (leader only)
    # ------------------------------------------------------------------

    def _update_s3_relations(self) -> None:
        if not self.unit.is_leader():
            return

        for relation in self.model.relations.get(self._s3_relation_name, []):
            creds = self._get_or_create_relation_credentials(relation.id)
            if not creds:
                continue
            bucket = f"{self._s3_relation_name}-{relation.id}"
            if not self._try_create_bucket(bucket, creds["access-key"], creds["secret-key"]):
                return
            relation.data[self.app].update({
                "endpoint": self._s3_endpoint_url,
                "access-key": creds["access-key"],
                "secret-key": creds["secret-key"],
                "bucket": bucket,
            })

    def _try_create_bucket(self, bucket: str, access_key: str, secret_key: str) -> bool:
        try:
            response = create_bucket(bucket)
        except ConnectionError as e:
            self.unit.status = ops.MaintenanceStatus(
                f"Waiting for S3 to be ready: {e}"
            )
            return False
        ok = 200 <= response.status < 300 or response.status == 409
        if not ok:
            logger.error(
                "Bucket creation failed: %s status=%d body=%s",
                bucket,
                response.status,
                response.read().decode(errors="replace")[:500],
            )
        return ok

    # ------------------------------------------------------------------
    # COS integration
    # ------------------------------------------------------------------

    def _update_cos_relations(self) -> None:
        if not self.unit.is_leader():
            return
        self._update_metrics_endpoint()
        self._update_grafana_dashboard()

    def _update_metrics_endpoint(self) -> None:
        if not self._is_metrics_enabled:
            return
        relation = self.model.get_relation("metrics-endpoint")
        if not relation:
            return
        scrape_jobs = json.dumps([{
            "metrics_path": "/metrics",
            "static_configs": [{"targets": [f"*:{self._metrics_port}"]}],
        }])
        relation.data[self.app]["scrape_jobs"] = scrape_jobs

    def _update_grafana_dashboard(self) -> None:
        relation = self.model.get_relation("grafana-dashboard")
        if not relation:
            return
        dashboards = json.dumps({"seaweedfs": build_dashboard()})
        relation.data[self.app]["dashboards"] = dashboards

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _set_status(self) -> None:
        if not check_s3_health():
            self.unit.status = ops.MaintenanceStatus("S3 endpoint not yet ready")
            return

        unit_count = len(self._get_peer_addresses()) + 1
        msg = f"S3 ready ({unit_count} unit{'s' if unit_count > 1 else ''} in cluster)"
        self.unit.status = ops.ActiveStatus(msg)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @property
    def _volume_size_limit_mb(self) -> int:
        return int(self.model.config.get("volume-size-limit-mb", 1024))

    @property
    def _filer_max_mb(self) -> int:
        return int(self.model.config.get("filer-max-mb", 64))

    @property
    def _replication(self) -> str:
        return str(self.model.config.get("replication", "001"))

    @property
    def _is_metrics_enabled(self) -> bool:
        return bool(self.model.config.get("metrics", True))

    @property
    def _s3_endpoint_url(self) -> str:
        return f"http://{self.app.name}.{self.model.name}.svc.cluster.local:{self._s3_port}"

    # ------------------------------------------------------------------
    # Version detection
    # ------------------------------------------------------------------

    @property
    def _seaweedfs_version(self) -> Optional[str]:
        try:
            version_output, _ = self._container.exec(
                ["/usr/bin/weed", "version"], timeout=30
            ).wait_output()
        except APIError:
            return None
        result = re.search(r"version.*\s(\d+\.\d+\.?\d*)", version_output)
        if result is None:
            return result
        return result.group(1)


if __name__ == "__main__":
    ops.main(SeaweedfsK8S)
