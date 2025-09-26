#!/usr/bin/env python3
# Copyright 2025 him
# See LICENSE file for licensing details.

"""Charm the application."""

import hashlib
import http.client
import logging
import os
import socket

import ops
from ops.pebble import Layer

from config import Config

logger = logging.getLogger(__name__)


class SeaweedfsK8S(ops.CharmBase):
    """Charm the application."""

    container_name = "seaweedfs"
    _storage_path = "/data"
    _config_path = "/config/s3.json"

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.reconcile()

    def reconcile(self):
        container = self.unit.get_container(self.container_name)
        if not container.can_connect():
            return

        config = Config().build()
        config_hash = hashlib.sha512(config.encode()).hexdigest()

        container.push(f"{self._config_path}", config)
        container.add_layer(
            self.container_name, self._pebble_layer(config_hash), combine=True
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
                    "endpoint": f"http://{socket.getfqdn()}:8333",
                    "access-key": "placeholder",
                    "secret-key": "placeholder",
                    "bucket": bucket_name,
                })

        self.unit.status = ops.ActiveStatus()

    def _pebble_layer(self, sentinel: str) -> Layer:
        """Construct the Pebble layer information.

        Args:
            sentinel: A value indicative of a change that should prompt a replan.
        """
        layer = Layer(
            {
                "summary": "seaweedfs-k8s layer",
                "description": "seaweedfs-k8s layer",
                "services": {
                    self.container_name: {
                        "override": "replace",
                        "summary": "seaweedfs-k8s service",
                        "command": f"/usr/bin/weed server -dir={self._storage_path} -s3 -s3.config={self._config_path} -ip.bind=0.0.0.0 -volumeSizeLimitMB=1024",
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
