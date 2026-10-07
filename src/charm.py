#!/usr/bin/env python3
# Copyright 2025 him
# See LICENSE file for licensing details.

"""SeaweedFS S3 charm.

Deploys SeaweedFS as a drop-in S3-compatible object store, scaling via a peer
mesh with a capped (<=3) Raft master set. See spec.md for the full design.
"""

import logging
from typing import Dict, List, Optional, Tuple

import ops
from charms.data_platform_libs.v0.s3 import CredentialRequestedEvent, S3Provider
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateAvailableEvent,
    CertificateRequestAttributes,
    Mode,
    TLSCertificatesRequiresV4,
)
from charms.traefik_k8s.v2.ingress import IngressPerAppReadyEvent, IngressPerAppRequirer
from ops.pebble import ChangeError, ExecError, Layer

import peers
import utils
from identities import Identity, admin_identity, build_identity_config

logger = logging.getLogger(__name__)

CONTAINER = "seaweedfs"
STORAGE_NAME = "data"
STORAGE_PATH = "/data"
IDENTITY_PATH = "/etc/iam/identity.json"
CERT_PATH = "/etc/ssl/seaweedfs/s3.crt"
KEY_PATH = "/etc/ssl/seaweedfs/s3.key"

PEER_RELATION = "seaweedfs-peers"
S3_RELATION = "s3-credentials"
CERTIFICATES_RELATION = "certificates"
INGRESS_RELATION = "ingress"

MASTER_PORT = 9333
VOLUME_PORT = 8080
FILER_PORT = 8888
S3_PORT = 8333

ADMIN_SECRET_LABEL = "s3-admin"


def relation_secret_label(relation_id: int) -> str:
    """Return the Juju secret label used to store a relation's S3 credentials."""
    return f"s3-rel-{relation_id}"


class SeaweedfsK8SCharm(ops.CharmBase):
    """Charm the SeaweedFS S3-compatible object store."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self.s3_provider = S3Provider(self, S3_RELATION)
        self.certificates = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=CERTIFICATES_RELATION,
            certificate_requests=[self._certificate_request_attributes()],
            mode=Mode.UNIT,
        )
        self.ingress = IngressPerAppRequirer(
            self, relation_name=INGRESS_RELATION, port=S3_PORT, strip_prefix=True
        )

        self.framework.observe(self.on[CONTAINER].pebble_ready, self._on_reconcile)
        self.framework.observe(self.on.config_changed, self._on_reconcile)
        self.framework.observe(self.on.upgrade_charm, self._on_reconcile)
        self.framework.observe(self.on.leader_elected, self._on_leader_elected)

        self.framework.observe(
            self.on[PEER_RELATION].relation_joined, self._on_peer_relation_joined
        )
        self.framework.observe(
            self.on[PEER_RELATION].relation_changed, self._on_peer_relation_changed
        )
        self.framework.observe(
            self.on[PEER_RELATION].relation_departed, self._on_peer_relation_departed
        )
        self.framework.observe(
            self.on[STORAGE_NAME].storage_detaching, self._on_storage_detaching
        )

        self.framework.observe(
            self.s3_provider.on.credentials_requested, self._on_s3_credentials_requested
        )
        self.framework.observe(
            self.on[S3_RELATION].relation_broken, self._on_s3_relation_broken
        )

        self.framework.observe(
            self.certificates.on.certificate_available, self._on_certificate_available
        )
        self.framework.observe(self.ingress.on.ready, self._on_ingress_ready)

        self.framework.observe(
            self.on.get_admin_credentials_action, self._on_get_admin_credentials_action
        )
        self.framework.observe(
            self.on.rotate_admin_credentials_action, self._on_rotate_admin_credentials_action
        )
        self.framework.observe(
            self.on.rotate_relation_credentials_action,
            self._on_rotate_relation_credentials_action,
        )
        self.framework.observe(
            self.on.pre_upgrade_check_action, self._on_pre_upgrade_check_action
        )

    # ------------------------------------------------------------------
    # Peer mesh / master set
    # ------------------------------------------------------------------

    def _own_ordinal(self) -> int:
        return peers.ordinal_from_unit_name(self.unit.name)

    def _own_dns_name(self) -> str:
        return peers.pod_dns_name(self.app.name, self._own_ordinal(), self.model.name)

    def _peer_addresses(self) -> Dict[int, str]:
        """Collect ordinal -> stable pod DNS name for every peer that has published one."""
        relation = self.model.get_relation(PEER_RELATION)
        addresses: Dict[int, str] = {}
        if not relation:
            return addresses

        own_address = relation.data[self.unit].get("peer-address")
        if own_address:
            addresses[self._own_ordinal()] = own_address

        for unit in relation.units:
            address = relation.data[unit].get("peer-address")
            if address:
                addresses[peers.ordinal_from_unit_name(unit.name)] = address

        return addresses

    def _master_ordinal_set(self) -> List[int]:
        """Return the current master set, preferring the leader-published app data."""
        relation = self.model.get_relation(PEER_RELATION)
        if relation:
            published = relation.data[self.app].get("master-ordinals")
            if published:
                return [int(x) for x in published.split(",") if x]

        return peers.master_ordinals(self._peer_addresses().keys())

    def _publish_master_ordinals(self, exclude: Optional[int] = None) -> None:
        """Leader: compute and publish the authoritative master set to app data."""
        if not self.unit.is_leader():
            return
        relation = self.model.get_relation(PEER_RELATION)
        if not relation:
            return
        ordinals = set(self._peer_addresses().keys())
        if exclude is not None:
            ordinals.discard(exclude)
        masters = peers.master_ordinals(ordinals)
        relation.data[self.app]["master-ordinals"] = ",".join(str(o) for o in sorted(masters))

    def _num_units(self) -> int:
        return max(len(self._peer_addresses()), 1)

    def _maybe_fix_replication(self) -> None:
        """Leader: run ``volume.fix.replication`` once when auto-replication changes.

        E.g. when scaling 1 -> 2 units, the auto default changes 000 -> 001;
        existing volumes need to be brought up to the new replication level.
        """
        if not self.unit.is_leader():
            return
        relation = self.model.get_relation(PEER_RELATION)
        if not relation:
            return
        if self.model.config.get("replication"):
            return  # explicit override: no auto-fix needed

        current = self._num_units()
        previous_raw = relation.data[self.app].get("replication-unit-count")
        previous = int(previous_raw) if previous_raw else 1

        if previous < 2 <= current:
            container = self.unit.get_container(CONTAINER)
            if container.can_connect():
                try:
                    utils.exec_weed_shell(container, ["volume.fix.replication"], timeout=60)
                except (ExecError, ChangeError, OSError) as e:
                    logger.warning("volume.fix.replication failed: %s", e)

        relation.data[self.app]["replication-unit-count"] = str(current)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_leader_elected(self, _: ops.LeaderElectedEvent) -> None:
        if not self.unit.is_leader():
            return
        self._ensure_admin_secret()
        self._publish_master_ordinals()
        self._reconcile()

    def _on_peer_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        event.relation.data[self.unit]["peer-address"] = self._own_dns_name()
        self._publish_master_ordinals()
        self._maybe_fix_replication()
        self._reconcile()

    def _on_peer_relation_changed(self, _: ops.RelationChangedEvent) -> None:
        self._publish_master_ordinals()
        self._maybe_fix_replication()
        self._reconcile()

    def _on_peer_relation_departed(self, event: ops.RelationDepartedEvent) -> None:
        departing_ordinal = None
        if event.departing_unit:
            departing_ordinal = peers.ordinal_from_unit_name(event.departing_unit.name)
        self._publish_master_ordinals(exclude=departing_ordinal)
        self._reconcile()

    def _on_storage_detaching(self, _: ops.StorageDetachingEvent) -> None:
        container = self.unit.get_container(CONTAINER)
        if not container.can_connect():
            return
        try:
            utils.exec_weed_shell(
                container,
                [f"volume.server.evacuate -node {self._own_dns_name()}:{VOLUME_PORT}"],
                timeout=60,
            )
        except (ExecError, ChangeError, TimeoutError, OSError) as e:
            logger.error("Volume evacuation failed or timed out, proceeding anyway: %s", e)

    def _on_s3_credentials_requested(self, event: CredentialRequestedEvent) -> None:
        if not self.unit.is_leader():
            return

        relation = event.relation
        bucket = event.bucket or f"s3-{relation.app.name}-{relation.id}"
        access_key, secret_key = utils.generate_credentials()

        self._put_secret(
            relation_secret_label(relation.id),
            {"access-key": access_key, "secret-key": secret_key, "bucket": bucket},
        )

        container = self.unit.get_container(CONTAINER)
        if container.can_connect():
            self._try_create_bucket(container, bucket)

        self._reconcile()

    def _on_s3_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        if not self.unit.is_leader():
            return
        self._delete_secret(relation_secret_label(event.relation.id))
        self._reconcile()

    def _on_certificate_available(self, _: CertificateAvailableEvent) -> None:
        self._reconcile()

    def _on_ingress_ready(self, event: IngressPerAppReadyEvent) -> None:
        logger.info("Ingress ready, external URL: %s", event.url)

    def _on_reconcile(self, _: ops.EventBase) -> None:
        self._reconcile()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_get_admin_credentials_action(self, event: ops.ActionEvent) -> None:
        secret = self._get_secret(ADMIN_SECRET_LABEL)
        if not secret:
            event.fail("Admin credentials have not been generated yet.")
            return
        content = secret.get_content(refresh=True)
        event.set_results(
            {"access-key": content["access-key"], "secret-key": content["secret-key"]}
        )

    def _on_rotate_admin_credentials_action(self, event: ops.ActionEvent) -> None:
        secret = self._get_secret(ADMIN_SECRET_LABEL)
        if not secret:
            event.fail("Admin credentials have not been generated yet.")
            return
        access_key, secret_key = utils.generate_credentials()
        secret.set_content({"access-key": access_key, "secret-key": secret_key})
        self._reconcile()
        event.set_results({"access-key": access_key, "secret-key": secret_key})

    def _on_rotate_relation_credentials_action(self, event: ops.ActionEvent) -> None:
        relation_id = int(event.params["relation-id"])
        known_ids = {r.id for r in self.model.relations.get(S3_RELATION, [])}
        if relation_id not in known_ids:
            event.fail(f"No such s3-credentials relation: {relation_id}")
            return
        relation = self.model.get_relation(S3_RELATION, relation_id)
        assert relation is not None

        label = relation_secret_label(relation.id)
        secret = self._get_secret(label)
        if not secret:
            event.fail(f"No credentials found for relation {relation_id}")
            return

        bucket = secret.get_content(refresh=True)["bucket"]
        access_key, secret_key = utils.generate_credentials()
        secret.set_content({"access-key": access_key, "secret-key": secret_key, "bucket": bucket})

        self._reconcile()
        event.set_results({"access-key": access_key, "secret-key": secret_key})

    def _on_pre_upgrade_check_action(self, event: ops.ActionEvent) -> None:
        status = utils.cluster_status()
        if not utils.has_raft_leader(status):
            event.fail("Raft has no leader; refusing to proceed with upgrade.")
            return
        event.set_results({"result": "ok"})

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _reconcile(self) -> None:
        """Recompute desired state and apply it. Safe to call from any event."""
        container = self.unit.get_container(CONTAINER)
        if not container.can_connect():
            self.unit.status = ops.MaintenanceStatus("Waiting for container...")
            return

        if self.unit.is_leader():
            self._sync_identities()
            self._resync_relation_endpoints()

        tls_cert, tls_key, _ = self._tls_material()
        if tls_cert and tls_key:
            container.push(CERT_PATH, tls_cert, make_dirs=True)
            container.push(KEY_PATH, tls_key, make_dirs=True)

        command = self._build_command()
        container.add_layer(CONTAINER, self._pebble_layer(command), combine=True)
        container.replan()

        version = self._weed_version(container)
        if version:
            self.unit.set_workload_version(version)

        self.unit.status = self._compute_status()

    def _compute_status(self) -> ops.StatusBase:
        status = utils.cluster_status()
        if not utils.has_raft_leader(status):
            return ops.WaitingStatus("Waiting for master quorum")

        if not utils.http_ok(port=S3_PORT, path="/healthz"):
            return ops.WaitingStatus("S3 endpoint unreachable")

        num_units = self._num_units()
        if num_units <= 1:
            return ops.ActiveStatus("S3 ready (standalone)")

        num_masters = len(self._master_ordinal_set())
        return ops.ActiveStatus(f"S3 ready ({num_units} units, {num_masters} masters)")

    # ------------------------------------------------------------------
    # Pebble layer
    # ------------------------------------------------------------------

    def _replication(self) -> str:
        configured = self.model.config.get("replication") or ""
        if configured:
            return str(configured)
        return peers.default_replication(self._num_units())

    def _build_command(self) -> str:
        own_ordinal = self._own_ordinal()
        masters = self._master_ordinal_set()
        addresses = self._peer_addresses()
        i_am_master = own_ordinal in masters

        master_hosts = [
            f"{addresses[o]}:{MASTER_PORT}" for o in sorted(masters) if o in addresses
        ]
        master_peers_csv = ",".join(master_hosts)

        tokens = [
            "/usr/bin/weed server",
            f"-dir={STORAGE_PATH}",
            "-ip.bind=0.0.0.0",
            f"-master={'true' if i_am_master else 'false'}",
            f"-master.defaultReplication={self._replication()}",
            f"-master.volumeSizeLimitMB={self.model.config['volume-size-limit-mb']}",
            "-master.electionTimeout=1s",
            "-volume.max=0",
            "-filer",
            "-filer.maxMB=64",
            "-s3",
            f"-s3.config={IDENTITY_PATH}",
            f"-s3.port={S3_PORT}",
        ]
        if i_am_master and master_peers_csv:
            tokens.append(f"-master.peers={master_peers_csv}")
        if master_peers_csv:
            tokens.append(f"-volume.mserver={master_peers_csv}")
            tokens.append(f"-filer.master={master_peers_csv}")

        tls_cert, tls_key, _ = self._tls_material()
        if tls_cert and tls_key:
            tokens.append(f"-s3.cert.file={CERT_PATH}")
            tokens.append(f"-s3.key.file={KEY_PATH}")

        return " ".join(tokens)

    def _pebble_layer(self, command: str) -> Layer:
        return Layer(
            {
                "summary": "seaweedfs-k8s layer",
                "description": "seaweedfs-k8s layer",
                "services": {
                    CONTAINER: {
                        "override": "replace",
                        "summary": "seaweedfs-k8s service",
                        "command": command,
                        "startup": "enabled",
                        "on-check-failure": {"s3-online": "restart"},
                    },
                },
                "checks": {
                    "s3-online": {
                        "override": "replace",
                        "level": "ready",
                        "period": "10s",
                        "threshold": 3,
                        "http": {"url": f"http://localhost:{S3_PORT}/healthz"},
                    },
                },
            }
        )

    def _weed_version(self, container: ops.Container) -> Optional[str]:
        try:
            output, _ = container.exec(["/usr/bin/weed", "version"], timeout=30).wait_output()
        except (ExecError, ChangeError, OSError):
            return None
        return utils.parse_weed_version(output)

    # ------------------------------------------------------------------
    # TLS
    # ------------------------------------------------------------------

    def _certificate_request_attributes(self) -> CertificateRequestAttributes:
        service_dns = f"{self.app.name}.{self.model.name}.svc.cluster.local"
        pod_dns = peers.pod_dns_name(self.app.name, self._own_ordinal(), self.model.name)
        return CertificateRequestAttributes(
            common_name=service_dns, sans_dns=frozenset({service_dns, pod_dns})
        )

    def _tls_material(self) -> Tuple[Optional[str], Optional[str], List[str]]:
        provider_certificate, private_key = self.certificates.get_assigned_certificate(
            self._certificate_request_attributes()
        )
        if not provider_certificate or not private_key:
            return None, None, []
        chain = [str(c) for c in provider_certificate.chain] or [str(provider_certificate.ca)]
        return str(provider_certificate.certificate), str(private_key), chain

    def _tls_enabled(self) -> bool:
        return self._tls_material()[0] is not None

    def _s3_endpoint(self) -> str:
        scheme = "https" if self._tls_enabled() else "http"
        return f"{scheme}://{self.app.name}.{self.model.name}.svc.cluster.local:{S3_PORT}"

    # ------------------------------------------------------------------
    # S3 identities / credentials
    # ------------------------------------------------------------------

    def _ensure_admin_secret(self) -> ops.Secret:
        secret = self._get_secret(ADMIN_SECRET_LABEL)
        if secret:
            return secret
        access_key, secret_key = utils.generate_credentials()
        return self.app.add_secret(
            {"access-key": access_key, "secret-key": secret_key}, label=ADMIN_SECRET_LABEL
        )

    def _get_secret(self, label: str) -> Optional[ops.Secret]:
        try:
            return self.model.get_secret(label=label)
        except ops.SecretNotFoundError:
            return None

    def _put_secret(self, label: str, content: dict) -> ops.Secret:
        secret = self._get_secret(label)
        if secret:
            secret.set_content(content)
            return secret
        return self.app.add_secret(content, label=label)

    def _delete_secret(self, label: str) -> None:
        secret = self._get_secret(label)
        if secret:
            secret.remove_all_revisions()

    def _publish_relation_connection_info(
        self, relation_id: int, access_key: str, secret_key: str, bucket: str
    ) -> None:
        data: dict = {
            "endpoint": self._s3_endpoint(),
            "bucket": bucket,
            "access-key": access_key,
            "secret-key": secret_key,
            "s3-uri-style": "path",
        }
        if self._tls_enabled():
            data["tls-ca-chain"] = self._tls_material()[2]
        self.s3_provider.update_connection_info(relation_id, data)

    def _resync_relation_endpoints(self) -> None:
        """Leader: republish endpoint/TLS info for every already-provisioned relation."""
        for relation in self.model.relations.get(S3_RELATION, []):
            secret = self._get_secret(relation_secret_label(relation.id))
            if not secret:
                continue
            content = secret.get_content(refresh=True)
            self._publish_relation_connection_info(
                relation.id, content["access-key"], content["secret-key"], content["bucket"]
            )

    def _try_create_bucket(self, container: ops.Container, bucket_name: str) -> bool:
        try:
            utils.exec_weed_shell(container, [f"s3.bucket.create -name {bucket_name}"], timeout=30)
        except ExecError as e:
            combined = f"{e.stdout or ''}{e.stderr or ''}".lower()
            if "already exists" in combined:
                logger.info("Bucket %s already exists, skipping", bucket_name)
                return True
            logger.error("Failed to create bucket %s: %s", bucket_name, e)
            return False
        except (ChangeError, OSError) as e:
            logger.error("Failed to create bucket %s: %s", bucket_name, e)
            return False
        return True

    def _sync_identities(self) -> None:
        """Leader: rewrite the identity config from secrets + relations and hot-reload it."""
        admin_secret = self._ensure_admin_secret()
        admin_content = admin_secret.get_content(refresh=True)
        identities = [admin_identity(admin_content["access-key"], admin_content["secret-key"])]

        for relation in self.model.relations.get(S3_RELATION, []):
            secret = self._get_secret(relation_secret_label(relation.id))
            if not secret:
                continue
            content = secret.get_content(refresh=True)
            identities.append(
                Identity(
                    name=f"relation-{relation.id}",
                    access_key=content["access-key"],
                    secret_key=content["secret-key"],
                    buckets=[content["bucket"]],
                )
            )

        config = build_identity_config(identities)

        container = self.unit.get_container(CONTAINER)
        if not container.can_connect():
            return

        container.push(IDENTITY_PATH, config, make_dirs=True)

        commands = [
            (
                f"s3.configure -user {i.name} -access_key {i.access_key} "
                f"-secret_key {i.secret_key} -buckets {','.join(i.buckets)} -apply"
            )
            for i in identities
        ]
        try:
            utils.exec_weed_shell(container, commands, timeout=30)
        except (ExecError, ChangeError, OSError) as e:
            logger.debug("s3.configure hot-reload not available yet: %s", e)


if __name__ == "__main__":  # pragma: nocover
    ops.main(SeaweedfsK8SCharm)
