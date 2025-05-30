#!/usr/bin/env python3
# Copyright 2025 him
# See LICENSE file for licensing details.

"""Charm the application."""

import logging

import ops

logger = logging.getLogger(__name__)


class SeaweedfsK8S(ops.CharmBase):
    """Charm the application."""

    container_name = "seaweedfs"
    _storage_path = "/data"

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.reconcile()
        
    def reconcile(self):
        container = self.unit.get_container(self.container_name)
        container.add_layer(
            self.container_name, self._pebble_layer(""), combine=True
        )
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
                        "command": f"/usr/bin/weed server -dir={self._storage_path} -s3",
                        "startup": "enabled",
                        "environment": {
                            "_config_hash": sentinel,  # Restarts the service via pebble replan
                            "https_proxy": os.environ.get("JUJU_CHARM_HTTPS_PROXY", ""),
                            "http_proxy": os.environ.get("JUJU_CHARM_HTTP_PROXY", ""),
                            "no_proxy": os.environ.get("JUJU_CHARM_NO_PROXY", ""),
                        },
                    }
                },
            }
        )

        return layer
    



if __name__ == "__main__":  # pragma: nocover
    ops.main(SeaweedfsK8S)
