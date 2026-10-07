#!/usr/bin/env python3
# Copyright 2025 him
# See LICENSE file for licensing details.

"""Charm the application."""

import hashlib
import logging
import os
import re
import secrets
import socket
from typing import Any, Dict, List, Optional, cast

import ops
from ops.pebble import Error, Layer

from config import build_s3_config
from utils import create_bucket

logger = logging.getLogger(__name__)

CONTAINER_NAME = "seaweedfs"
STORAGE_PATH = "/data"
S3_CONFIG_PATH = "/config/s3.json"
MASTER_PORT = 9333
VOLUME_PORT = 8080
FILER_PORT = 8888
S3_PORT = 8333

# The first unit (ordinal 0) owns the single master and filer. Volume servers
# and S3 gateways run on every unit, so adding units adds storage capacity and
# gateway throughput without any manual configuration.
BOOTSTRAP_UNIT_SUFFIX = "/0"


class SeaweedfsK8S(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        container_events = self.on[CONTAINER_NAME]
        for event in (
            container_events.pebble_ready,
            container_events.pebble_check_recovered,
            self.on.config_changed,
            self.on.leader_elected,
            self.on.update_status,
            self.on["cluster"].relation_created,
            self.on["cluster"].relation_changed,
            self.on["cluster"].relation_joined,
            self.on["cluster"].relation_departed,
            self.on["s3-credentials"].relation_created,
            self.on["s3-credentials"].relation_changed,
            self.on["s3-credentials"].relation_joined,
        ):
            self.framework.observe(event, self._reconcile)

    def _reconcile(self, event: ops.EventBase) -> None:
        """Recreate the world."""
        container = self.unit.get_container(CONTAINER_NAME)
        if not container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for workload container")
            return

        peer = self.model.get_relation("cluster")
        if peer is None:
            self.unit.status = ops.WaitingStatus("waiting for cluster relation")
            return

        # Each unit publishes its own address; the leader decides which address
        # is the master/filer endpoint and writes it to application data.
        peer.data[self.unit]["fqdn"] = self._address
        if self.unit.is_leader():
            self._bootstrap_app_data(peer)

        master_address = peer.data[self.app].get("master-address")
        filer_address = peer.data[self.app].get("filer-address")
        credentials = self._credentials(peer)
        if not master_address or not filer_address or credentials is None:
            self.unit.status = ops.WaitingStatus("waiting for cluster bootstrap")
            return

        s3_config = build_s3_config([credentials])
        config_hash = hashlib.sha512(s3_config.encode()).hexdigest()
        container.push(S3_CONFIG_PATH, s3_config, make_dirs=True)
        container.add_layer(
            CONTAINER_NAME,
            self._pebble_layer(master_address, filer_address, config_hash),
            combine=True,
        )
        container.replan()

        self.unit.set_workload_version(self._seaweedfs_version or "")

        if self.unit.is_leader():
            self._update_relations(master_address, credentials)

        if not self._ensure_buckets(credentials):
            return

        self.unit.status = ops.ActiveStatus()

    # -- cluster wiring ----------------------------------------------------

    @property
    def _address(self) -> str:
        """Stable in-cluster address of this unit."""
        return socket.getfqdn()

    @property
    def _is_bootstrap_unit(self) -> bool:
        """Whether this unit runs the master and filer."""
        return self.unit.name.endswith(BOOTSTRAP_UNIT_SUFFIX)

    @property
    def _unit_count(self) -> int:
        peer = self.model.get_relation("cluster")
        return len(peer.units) + 1 if peer else 1

    @property
    def _replication(self) -> str:
        """Replication placement, downgraded for a single volume server.

        SeaweedFS cannot place replicas on a cluster with a single volume
        server, so a one-unit deployment always uses "000" regardless of the
        configured value.
        """
        configured = self.config.get("replication") or "001"
        if self._unit_count < 2:
            return "000"
        return str(configured)

    def _bootstrap_app_data(self, peer: ops.Relation) -> None:
        """Populate application data with the master address and credentials."""
        first = self._first_unit_fqdn(peer)
        if first:
            peer.data[self.app]["master-address"] = first
            peer.data[self.app]["filer-address"] = first

        data = peer.data[self.app]
        if not data.get("admin-access-key") or not data.get("admin-secret-key"):
            data["admin-access-key"] = secrets.token_hex(10)
            data["admin-secret-key"] = secrets.token_urlsafe(30)

    def _first_unit_fqdn(self, peer: ops.Relation) -> Optional[str]:
        units = list(peer.units) + [self.unit]
        units.sort(key=lambda unit: unit.name)
        return peer.data[units[0]].get("fqdn")

    def _credentials(self, peer: ops.Relation) -> Optional[Dict[str, str]]:
        data = peer.data[self.app]
        access_key = data.get("admin-access-key")
        secret_key = data.get("admin-secret-key")
        if not access_key or not secret_key:
            return None
        return {"name": "admin", "access-key": access_key, "secret-key": secret_key}

    # -- relations ---------------------------------------------------------

    def _relation_buckets(self) -> List[str]:
        return [
            f"{relation.name}-{relation.id}"
            for relation in self.model.relations.get("s3-credentials", [])
        ]

    def _update_relations(self, endpoint_address: str, credentials: Dict[str, str]) -> None:
        for relation in self.model.relations.get("s3-credentials", []):
            relation.data[self.app].update(
                {
                    "endpoint": f"http://{endpoint_address}:{S3_PORT}",
                    "access-key": credentials["access-key"],
                    "secret-key": credentials["secret-key"],
                    "bucket": f"{relation.name}-{relation.id}",
                }
            )

    # -- bucket provisioning ----------------------------------------------

    def _ensure_buckets(self, credentials: Dict[str, str]) -> bool:
        """Create every bucket the charm is responsible for.

        Returns False and sets a status when the S3 endpoint is not reachable
        yet, so the reconcile is retried on the next event.
        """
        if not self.unit.is_leader():
            return True

        buckets = [str(self.config.get("bucket") or "")] + self._relation_buckets()
        for bucket in buckets:
            if not bucket:
                continue
            if not self._try_create_bucket(bucket, credentials):
                self.unit.status = ops.WaitingStatus(
                    f"waiting for S3 endpoint to create bucket {bucket!r}"
                )
                return False
        return True

    def _try_create_bucket(self, bucket: str, credentials: Dict[str, str]) -> bool:
        try:
            response = create_bucket(
                bucket,
                access_key=credentials["access-key"],
                secret_key=credentials["secret-key"],
            )
        except OSError as e:
            logger.info("S3 endpoint not reachable yet: %s", e)
            return False

        # 2xx means created; 409 means it already exists.
        return 200 <= response.status < 300 or response.status == 409

    # -- workload ----------------------------------------------------------

    def _pebble_layer(
        self, master_address: str, filer_address: str, sentinel: str
    ) -> Layer:
        """Construct the Pebble layer for this unit's role.

        Args:
            master_address: Address of the cluster master.
            filer_address: Address of the cluster filer.
            sentinel: A value indicative of a change that should prompt a replan.
        """
        environment = {
            "https_proxy": os.environ.get("JUJU_CHARM_HTTPS_PROXY", ""),
            "http_proxy": os.environ.get("JUJU_CHARM_HTTP_PROXY", ""),
            "no_proxy": os.environ.get("JUJU_CHARM_NO_PROXY", ""),
        }

        services = {
            "volume": {
                "override": "replace",
                "summary": "SeaweedFS volume server",
                "command": (
                    f"/usr/bin/weed volume -port={VOLUME_PORT} -ip.bind=0.0.0.0 "
                    # Keep volume data at the storage root, matching the layout
                    # of the previous all-in-one deployment.
                    f"-dir={STORAGE_PATH} -mserver={master_address}:{MASTER_PORT} "
                    "-max=0"
                ),
                "startup": "enabled",
                "environment": dict(environment),
            },
            "s3": {
                "override": "replace",
                "summary": "SeaweedFS S3 gateway",
                "command": (
                    f"/usr/bin/weed s3 -port={S3_PORT} -ip.bind=0.0.0.0 "
                    f"-filer={filer_address}:{FILER_PORT} -config={S3_CONFIG_PATH}"
                ),
                "startup": "enabled",
                "environment": {**environment, "_config_hash": sentinel},
            },
        }

        if self._is_bootstrap_unit:
            services["master"] = {
                "override": "replace",
                "summary": "SeaweedFS master server",
                "command": (
                    f"/usr/bin/weed master -port={MASTER_PORT} -ip.bind=0.0.0.0 "
                    f"-mdir={STORAGE_PATH}/master "
                    f"-defaultReplication={self._replication} "
                    "-volumeSizeLimitMB=1024"
                ),
                "startup": "enabled",
                "environment": {
                    **environment,
                    "WEED_MASTER_VOLUME_GROWTH_COPY_OTHER": "1",
                    "WEED_MASTER_VOLUME_GROWTH_COPY_1": "1",
                    "WEED_MASTER_VOLUME_GROWTH_COPY_2": "1",
                    "WEED_MASTER_VOLUME_GROWTH_COPY_3": "1",
                },
            }
            services["filer"] = {
                "override": "replace",
                "summary": "SeaweedFS filer server",
                "command": (
                    f"/usr/bin/weed filer -port={FILER_PORT} -ip.bind=0.0.0.0 "
                    f"-master={master_address}:{MASTER_PORT} "
                    f"-defaultStoreDir={STORAGE_PATH}/filer"
                ),
                "startup": "enabled",
                "environment": dict(environment),
            }

        return Layer(
            cast(
                Any,
                {
                    "summary": "seaweedfs-k8s layer",
                    "description": "seaweedfs-k8s layer",
                    "services": services,
                    "checks": {
                        "s3-online": {
                            "override": "replace",
                            "level": "ready",
                            "threshold": 1,  # do not miss a potential "recovered" event
                            # /status is served unauthenticated, unlike the S3 API root.
                            "http": {"url": f"http://localhost:{S3_PORT}/status"},
                        },
                    },
                },
            )
        )

    @property
    def _seaweedfs_version(self) -> Optional[str]:
        """Returns the workload version."""
        try:
            container = self.unit.get_container(CONTAINER_NAME)
            version_output, _ = container.exec(
                ["/usr/bin/weed", "version"], timeout=30
            ).wait_output()
        except Error:
            return None

        # Output looks like this:
        # version 30GB 3.97 76452ab59 linux amd64
        result = re.search(r"version.*\s(\d+\.\d+\.?\d*)", version_output)
        if result is None:
            return None
        return result.group(1)


if __name__ == "__main__":  # pragma: nocover
    ops.main(SeaweedfsK8S)
