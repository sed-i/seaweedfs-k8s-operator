#!/usr/bin/env python3
# Copyright 2025 him
# See LICENSE file for licensing details.

"""Charm the application."""

from dataclasses import dataclass
import http.client
import logging
import os
from pathlib import Path
import socket
from typing import Optional

import ops
from ops.pebble import Layer
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    Mode,
    TLSCertificatesRequiresV4,
)

logger = logging.getLogger(__name__)

@dataclass
class TLSConfig:
    """TLS configuration received by the coordinator over the `certificates` relation.

    This is an internal object that we use as facade so that the individual Coordinator charms don't have to know the API of the charm libs that implements the relation interface.
    """

    server_cert: str
    ca_cert: str
    private_key: str

KEY_PATH = f"/etc/seaweedfs/server.key"
CERT_PATH = f"/etc/seaweedfs/server.cert"
CA_CERT_PATH = "/usr/local/share/ca-certificates/ca.cert"


class SeaweedfsK8S(ops.CharmBase):
    """Charm the application."""

    container_name = "seaweedfs"
    _storage_path = "/data"

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self._certificates = TLSCertificatesRequiresV4(
            self,
            relationship_name="certificates",
            certificate_requests=[self._certificate_request_attributes],
            mode=Mode.APP,
        )

        self.reconcile()

    def reconcile(self):
        container = self.unit.get_container(self.container_name)
        if not container.can_connect():
            return
        
        self._reconcile_tls()

        container.add_layer(
            self.container_name, self._pebble_layer(""), combine=True
        )
        container.replan()

        if self.unit.is_leader():
            for relation in self.model.relations.get("s3-credentials", []):
                bucket_name = f"{relation.name}-{relation.id}"

                try:
                    conn = http.client.HTTPConnection("localhost:8333")
                    conn.request("PUT", f"/{bucket_name}")
                    response = conn.getresponse()
                except ConnectionError as e:
                    self.unit.status = ops.MaintenanceStatus(str(e))
                    return

                assert (
                    200 <= response.status < 300 or  # success
                    response.status == 409  # conflict: already exists
                )

                relation.data[self.app].update({
                    "endpoint": f"{socket.getfqdn()}:{8334 if self.tls_config else 8333}",
                    "access-key": "placeholder",
                    "secret-key": "placeholder",
                    "bucket": bucket_name,
                })

        self.unit.status = ops.ActiveStatus()


    def _reconcile_tls(self):
        self._certificates.sync()

        if tls_config := self.tls_config:
            self._configure_tls(
                server_cert=tls_config.server_cert,
                ca_cert=tls_config.ca_cert,
                private_key=tls_config.private_key,
            )
        else:
            self._delete_certificates()


    def _configure_tls(self, private_key: str, server_cert: str, ca_cert: str) -> None:
        """Save the certificates file to disk."""
        container = self.unit.get_container(self.container_name)
        if container.can_connect():
            # Read the current content of the files (if they exist)
            current_server_cert = (
                container.pull(CERT_PATH).read() if container.exists(CERT_PATH) else ""
            )
            current_private_key = (
                container.pull(KEY_PATH).read() if container.exists(KEY_PATH) else ""
            )
            current_ca_cert = (
                container.pull(CA_CERT_PATH).read()
                if container.exists(CA_CERT_PATH)
                else ""
            )

            if (
                current_server_cert == server_cert
                and current_private_key == private_key
                and current_ca_cert == ca_cert
            ):
                # No update needed
                return
            container.push(KEY_PATH, private_key, make_dirs=True)
            container.push(CERT_PATH, server_cert, make_dirs=True)
            container.push(CA_CERT_PATH, ca_cert, make_dirs=True)


    def _delete_certificates(self) -> None:
        """Delete the certificate files from disk."""
        container = self.unit.get_container(self.container_name)
        if container.can_connect():
            if container.exists(CERT_PATH):
                container.remove_path(CERT_PATH, recursive=True)
            if container.exists(KEY_PATH):
                container.remove_path(KEY_PATH, recursive=True)
            if container.exists(CA_CERT_PATH):
                container.remove_path(CA_CERT_PATH, recursive=True)


    @property
    def tls_config(self) -> Optional[TLSConfig]:
        """Returns the TLS configuration, including certificates and private key, if available; None otherwise."""
        certificates, key = self._certificates.get_assigned_certificate(
            certificate_request=self._certificate_request_attributes
        )
        if not (key and certificates):
            return None
        return TLSConfig(certificates.certificate.raw, certificates.ca.raw, key.raw)

    @property
    def _certificate_request_attributes(self) -> CertificateRequestAttributes:
        return CertificateRequestAttributes(
            # common_name is required and has a limit of 64 chars.
            # it is superseded by sans anyway, so we can use a constrained name,
            # such as app_name
            common_name=self.app.name,
            # update certificate with new SANs whenever a worker is added/removed
            sans_dns=frozenset(
                (socket.getfqdn(), self._k8s_service_fqdn)
            ),
        )
    
    @property
    def _are_certificates_on_disk(self) -> bool:
        """Return True if the certificates files are on disk."""
        container = self.unit.get_container(self.container_name)
        return (
            container.can_connect()
            and container.exists(CERT_PATH)
            and container.exists(KEY_PATH)
            and container.exists(CA_CERT_PATH)
        )


    @property
    def _k8s_service_fqdn(self) -> str:
        """The FQDN of the k8s service associated with this application.

        This service load balances traffic across all application units.
        Falls back to this unit's DNS name if the hostname does not resolve to a Kubernetes-style fqdn.
        """
        # example: 'tempo-0.tempo-headless.default.svc.cluster.local'
        hostname = socket.getfqdn()
        hostname_parts = hostname.split(".")
        # 'svc' is always there in a K8s service fqdn
        # ref: https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/#services
        if "svc" not in hostname_parts:
            logger.debug(f"expected K8s-style fqdn, but got {hostname} instead")
            return hostname

        dns_name_parts = hostname_parts[hostname_parts.index("svc") :]
        dns_name = ".".join(dns_name_parts)  # 'svc.cluster.local'
        return f"{self.app.name}.{self.model.name}.{dns_name}"  # 'tempo.model.svc.cluster.local'


    def _pebble_layer(self, sentinel: str) -> Layer:
        """Construct the Pebble layer information.

        Args:
            sentinel: A value indicative of a change that should prompt a replan.
        """
        tls_env = {
            "TLS_CERT": CERT_PATH,
            "TLS_KEY": KEY_PATH,
            "TLS_CA": CA_CERT_PATH,
        }

        layer = Layer(
            {
                "summary": "seaweedfs-k8s layer",
                "description": "seaweedfs-k8s layer",
                "services": {
                    self.container_name: {
                        "override": "replace",
                        "summary": "seaweedfs-k8s service",
                        "command": f"/usr/bin/weed server -dir={self._storage_path} -s3 -s3.port.https=8334",
                        "startup": "enabled",
                        "environment": {
                            "_config_hash": sentinel,  # Restarts the service via pebble replan
                            "https_proxy": os.environ.get("JUJU_CHARM_HTTPS_PROXY", ""),
                            "http_proxy": os.environ.get("JUJU_CHARM_HTTP_PROXY", ""),
                            "no_proxy": os.environ.get("JUJU_CHARM_NO_PROXY", ""),
                            **(tls_env if self._are_certificates_on_disk else {})
                        },
                    },
                },
                "checks": {
                    "s3-online": {
                        "override": "replace",
                        "level": "ready",
                        "threshold": 1,  # we do not want to miss a potential "recovered" event!
                        "http": {
                            "url": "http://localhost:8333",
                        },
                    },
                },
            }
        )

        return layer


if __name__ == "__main__":  # pragma: nocover
    ops.main(SeaweedfsK8S)
