#!/usr/bin/env python3
# Copyright 2025 him
# See LICENSE file for licensing details.

"""Charm the application.

This charm turns SeaweedFS into a self-clustering, S3-compatible storage
backend: deploy a single unit for lightweight testing, or scale out with
``juju add-unit`` to grow a replicated, highly-available storage cluster
usable as a home-lab replacement for minio or ceph/radosgw -- no manual
configuration required.
"""

import hashlib
import json
import logging
import os
import re
import socket
from typing import Optional

import ops
from ops.pebble import APIError, ChangeError, ExecError, Layer

from config import Config
from utils import (
    FILER_PORT,
    MASTER_PORT,
    S3_PORT,
    VOLUME_PORT,
    cluster_peers,
    create_bucket,
    generate_credential,
    num_master_units,
    replication_placement,
)

logger = logging.getLogger(__name__)

PEER_RELATION = "swfs-peers"
S3_RELATION = "s3-credentials"
IDENTITIES_SECRET_LABEL = "swfs-identities"
IDENTITIES_SECRET_ID_KEY = "identities-secret-id"


def hook() -> str:
    """Return the name of the Juju event currently being handled.

    Juju has, over time, exposed this via different environment variables.
    ``JUJU_HOOK_NAME`` is the legacy/shell-hook variable and is not set for
    every event (notably actions), so fall back to parsing the modern
    ``JUJU_DISPATCH_PATH`` (e.g. ``hooks/install``, ``actions/get-admin-credentials``).
    """
    name = os.environ.get("JUJU_HOOK_NAME")
    if name:
        return name
    return os.environ.get("JUJU_DISPATCH_PATH", "").rsplit("/", 1)[-1]


class SeaweedfsK8S(ops.CharmBase):
    """Charm the application."""

    container_name = "seaweedfs"
    _storage_path = "/data"
    _config_path = "/config/s3.json"

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.framework.observe(
            self.on["get-admin-credentials"].action, self._on_get_admin_credentials
        )
        self.reconcile()

    def reconcile(self):
        """Recreate the world."""
        container = self.unit.get_container(self.container_name)
        if not container.can_connect():
            return

        if hook() in ["install", "remove", "stop"]:
            return

        peer_relation = self.model.get_relation(PEER_RELATION)
        if peer_relation is None:
            self.unit.status = ops.WaitingStatus("waiting for peer relation")
            return

        identities = self._load_or_init_identities(peer_relation)
        if identities is None:
            self.unit.status = ops.WaitingStatus("waiting for leader to initialize credentials")
            return

        own_fqdn = socket.getfqdn()
        own_unit_number = int(self.unit.name.rsplit("/", 1)[-1])
        planned_units = self.app.planned_units()
        num_masters = num_master_units(planned_units)
        is_master = own_unit_number < num_masters
        master_peers = cluster_peers(own_fqdn, num_masters, port=MASTER_PORT)
        replication = self._replication_placement(planned_units)

        s3_config = self._build_s3_config(identities)
        config_hash = hashlib.sha512(s3_config.encode()).hexdigest()

        container.push(self._config_path, s3_config, make_dirs=True)
        container.add_layer(
            self.container_name,
            self._pebble_layer(
                sentinel=config_hash,
                is_master=is_master,
                master_peers=master_peers,
                own_fqdn=own_fqdn,
                replication=replication,
            ),
            combine=True,
        )
        container.replan()

        self.unit.set_ports(S3_PORT, MASTER_PORT, FILER_PORT, VOLUME_PORT)
        self.unit.set_workload_version(self._seaweedfs_version or "")

        admin_access_key = identities["admin_access_key"]
        admin_secret_key = identities["admin_secret_key"]

        if self.unit.is_leader():
            # Create the bucket requested via the config option, if any.
            bucket = str(self.model.config.get("bucket") or "")
            if bucket and not self.try_create_bucket(bucket, admin_access_key, admin_secret_key):
                return

            # Create (and publish credentials for) one bucket per relation.
            for relation in self.model.relations.get(S3_RELATION, []):
                rid = str(relation.id)
                bucket_identity = identities["buckets"][rid]
                bucket_name = bucket_identity["bucket"]

                if not self.try_create_bucket(
                    bucket_name,
                    bucket_identity["access_key"],
                    bucket_identity["secret_key"],
                ):
                    return

                relation.data[self.app].update(
                    {
                        "endpoint": f"http://{own_fqdn}:{S3_PORT}",
                        "access-key": bucket_identity["access_key"],
                        "secret-key": bucket_identity["secret_key"],
                        "bucket": bucket_name,
                    }
                )

        cluster_desc = (
            f"{num_masters} master/{planned_units} unit(s), replication={replication}"
        )
        self.unit.status = ops.ActiveStatus(
            f"s3 ready at http://{own_fqdn}:{S3_PORT} ({cluster_desc})"
        )

    def _replication_placement(self, planned_units: int) -> str:
        """Return the effective replica placement string.

        Honours the ``replication`` config option when set to something
        other than ``"auto"``, otherwise derives a sensible default from the
        number of planned units.
        """
        override = str(self.model.config.get("replication", "auto"))
        if override and override != "auto":
            return override
        return replication_placement(planned_units)

    def _load_or_init_identities(self, peer_relation: ops.Relation) -> Optional[dict]:
        """Return the shared admin/per-bucket S3 identities for this app.

        The identities (an admin access/secret key pair, plus one scoped
        access/secret key pair per ``s3-credentials`` relation) are stored in
        a single Juju secret owned by the application, so that every unit's
        independent S3 gateway process authenticates client requests against
        the same set of credentials. The leader creates the secret (and
        allocates per-relation credentials) on demand; other units simply
        read it via the secret ID shared through peer relation data.

        Returns ``None`` if this is a non-leader unit and the leader has not
        yet published the secret.
        """
        secret_id = peer_relation.data[self.app].get(IDENTITIES_SECRET_ID_KEY)

        if secret_id:
            secret = self.model.get_secret(id=secret_id)
            identities = json.loads(secret.get_content(refresh=True)["identities-json"])
        elif self.unit.is_leader():
            identities = {
                "admin_access_key": generate_credential(),
                "admin_secret_key": generate_credential(40),
                "buckets": {},
            }
            secret = self.app.add_secret(
                {"identities-json": json.dumps(identities)},
                label=IDENTITIES_SECRET_LABEL,
                description="SeaweedFS S3 admin and per-relation credentials",
            )
            assert secret.id is not None
            peer_relation.data[self.app][IDENTITIES_SECRET_ID_KEY] = secret.id
        else:
            return None

        if self.unit.is_leader():
            self._sync_bucket_identities(secret, identities)

        return identities

    def _sync_bucket_identities(self, secret: ops.Secret, identities: dict) -> None:
        """Allocate/prune per-relation bucket credentials and persist changes.

        Mutates ``identities["buckets"]`` in place and writes it back to
        ``secret`` only if something actually changed, to avoid creating
        needless secret revisions.
        """
        changed = False
        active_ids = set()

        for relation in self.model.relations.get(S3_RELATION, []):
            rid = str(relation.id)
            active_ids.add(rid)
            if rid not in identities["buckets"]:
                identities["buckets"][rid] = {
                    "bucket": f"{relation.name}-{relation.id}",
                    "access_key": generate_credential(),
                    "secret_key": generate_credential(40),
                }
                changed = True

        for rid in list(identities["buckets"]):
            if rid not in active_ids:
                del identities["buckets"][rid]
                changed = True

        if changed:
            secret.set_content({"identities-json": json.dumps(identities)})

    def _build_s3_config(self, identities: dict) -> str:
        """Render the SeaweedFS S3 identities file from shared credentials."""
        cfg = Config().add_admin(identities["admin_access_key"], identities["admin_secret_key"])
        for rid, bucket_identity in identities["buckets"].items():
            cfg.add_bucket_identity(
                name=f"relation-{rid}",
                access_key=bucket_identity["access_key"],
                secret_key=bucket_identity["secret_key"],
                bucket=bucket_identity["bucket"],
            )
        return cfg.build()

    def try_create_bucket(self, bucket_name: str, access_key: str, secret_key: str) -> bool:
        """Create a bucket, returning True on success.

        Returns False and sets unit status if the connection fails.
        """
        try:
            response = create_bucket(bucket_name, access_key, secret_key)
        except ConnectionError as e:
            self.unit.status = ops.MaintenanceStatus(str(e))
            return False

        assert (
            200 <= response.status < 300  # success
            or response.status == 409  # conflict: already exists
        )
        return True

    def _on_get_admin_credentials(self, event: ops.ActionEvent) -> None:
        """Return the admin S3 credentials and endpoint for manual use."""
        peer_relation = self.model.get_relation(PEER_RELATION)
        secret_id = (
            peer_relation.data[self.app].get(IDENTITIES_SECRET_ID_KEY) if peer_relation else None
        )
        if not secret_id:
            event.fail("Credentials are not ready yet, please retry shortly.")
            return

        secret = self.model.get_secret(id=secret_id)
        identities = json.loads(secret.get_content(refresh=True)["identities-json"])

        own_fqdn = socket.getfqdn()
        planned_units = self.app.planned_units()
        endpoints = [
            f"http://{peer}" for peer in cluster_peers(own_fqdn, planned_units, port=S3_PORT)
        ]

        event.set_results(
            {
                "access-key": identities["admin_access_key"],
                "secret-key": identities["admin_secret_key"],
                "endpoint": f"http://{own_fqdn}:{S3_PORT}",
                "endpoints": ",".join(endpoints),
            }
        )

    def _pebble_layer(
        self,
        sentinel: str,
        is_master: bool,
        master_peers: list,
        own_fqdn: str,
        replication: str,
    ) -> Layer:
        """Construct the Pebble layer information.

        Args:
            sentinel: A value indicative of a change that should prompt a replan.
            is_master: Whether this unit should run the SeaweedFS master/Raft role.
            master_peers: ``host:port`` list of every master-eligible unit.
            own_fqdn: This unit's stable, cluster-internal FQDN.
            replication: SeaweedFS replica placement string, e.g. ``"001"``.
        """
        volume_size_limit = self.model.config.get("volume-size-limit-mb", 1024)
        peers_csv = ",".join(master_peers)

        layer = Layer(
            {
                "summary": "seaweedfs-k8s layer",
                "description": "seaweedfs-k8s layer",
                "services": {
                    self.container_name: {
                        "override": "replace",
                        "summary": "seaweedfs-k8s service",
                        "command": (
                            "/usr/bin/weed server "
                            f"-master={'true' if is_master else 'false'} "
                            f"-master.peers={peers_csv} "
                            "-filer -filer.maxMB=64 "
                            f"-filer.defaultReplicaPlacement={replication} "
                            f"-dir={self._storage_path} "
                            f"-s3 -s3.config={self._config_path} "
                            f"-ip={own_fqdn} "
                            "-ip.bind=0.0.0.0 "
                            "-master.electionTimeout=1s "
                            f"-master.volumeSizeLimitMB={volume_size_limit} "
                            f"-master.defaultReplication={replication} "
                            "-volume.max=0"
                        ),
                        "startup": "enabled",
                        "environment": {
                            "_config_hash": sentinel,  # Restarts the service via pebble replan
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
                    # The volume server's own /status endpoint requires no
                    # authentication and is present on every unit regardless
                    # of its master/replication role, unlike the S3 gateway
                    # (which now rejects anonymous requests) or the master
                    # server (which only runs on master-eligible units).
                    "volume-online": {
                        "override": "replace",
                        "level": "ready",
                        "threshold": 1,  # we do not want to miss a potential "recovered" event!
                        "http": {
                            "url": f"http://localhost:{VOLUME_PORT}/status",
                        },
                    },
                },
            }
        )

        return layer

    @property
    def _seaweedfs_version(self) -> Optional[str]:
        """Returns the workload version."""
        try:
            container = self.unit.get_container(self.container_name)
            version_output, _ = container.exec(
                ["/usr/bin/weed", "version"], timeout=30
            ).wait_output()
        except (APIError, ExecError, ChangeError):
            return None

        # Output looks like this:
        # version 30GB 3.97 76452ab59 linux amd64
        result = re.search(r"version.*\s(\d+\.\d+\.?\d*)", version_output)
        if result is None:
            return result
        return result.group(1)


if __name__ == "__main__":  # pragma: nocover
    ops.main(SeaweedfsK8S)
