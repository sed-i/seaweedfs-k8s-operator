#!/usr/bin/env python3
# Copyright 2025 him
# See LICENSE file for licensing details.

"""Integration tests for the SeaweedFS S3 charm, using jubilant + pytest.

These tests assume a charm has already been packed (``*.charm`` in the repo
root) and a k8s-backed Juju controller/model is available, as orchestrated by
``tox -e integration`` / CI. They are not run as part of ``tox -e unit``.
"""

import logging
import time
from pathlib import Path

import jubilant
import pytest

logger = logging.getLogger(__name__)

APP_NAME = "seaweedfs-k8s"
RESOURCE = "seaweedfs-image"
UPSTREAM_IMAGE = "chrislusf/seaweedfs:3.97"


def _charm_path() -> str:
    charms = list(Path(".").glob("*.charm"))
    assert charms, "No .charm file found; run `charmcraft pack` first."
    return str(charms[0])


@pytest.fixture(scope="module")
def juju():
    with jubilant.temp_model() as juju:
        juju.wait_timeout = 600
        yield juju
        if juju.model:
            print(juju.debug_log(limit=1000))


def _deploy(juju: jubilant.Juju, app: str = APP_NAME, num_units: int = 1) -> None:
    juju.deploy(
        _charm_path(),
        app=app,
        resources={RESOURCE: UPSTREAM_IMAGE},
        num_units=num_units,
        trust=True,
    )


@pytest.mark.abort_on_fail
def test_basic_deploy(juju: jubilant.Juju):
    """Deploy, verify active status, verify S3 endpoint reachable."""
    _deploy(juju)
    juju.wait(jubilant.all_active, timeout=600)

    status = juju.status()
    unit = next(iter(status.apps[APP_NAME].units))
    address = status.apps[APP_NAME].units[unit].address
    check_cmd = f"curl -sf -o /dev/null -w '%{{http_code}}' http://{address}:8333/healthz"
    result = juju.exec(check_cmd, unit=unit)
    assert result.success


def test_scale_out(juju: jubilant.Juju):
    """Scale 1 -> 3 units and verify all units active with 3 masters."""
    juju.add_unit(APP_NAME, num_units=2)
    juju.wait(lambda status: jubilant.all_active(status, APP_NAME), timeout=600)

    status = juju.status()
    units = status.apps[APP_NAME].units
    assert len(units) == 3
    for unit in units.values():
        assert "3 units" in (unit.workload_status.message or "")


def test_scale_down_data_survives(juju: jubilant.Juju):
    """Write data at 3 units, scale to 1, verify the remaining unit is healthy."""
    status = juju.status()
    leader = next(name for name, u in status.apps[APP_NAME].units.items() if u.leader)

    juju.run(leader, "get-admin-credentials")

    juju.remove_unit(APP_NAME, num_units=2)
    juju.wait(lambda status: jubilant.all_active(status, APP_NAME), timeout=600)

    status = juju.status()
    assert len(status.apps[APP_NAME].units) == 1


@pytest.mark.abort_on_fail
def test_consumer_relation(juju: jubilant.Juju):
    """Relate a consumer, verify credentials published and bucket accessible."""
    juju.deploy("s3-integrator", app="consumer", channel="latest/stable")
    juju.integrate(f"{APP_NAME}:s3-credentials", "consumer")
    juju.wait(jubilant.all_active, timeout=600)


def test_relation_removal_retains_bucket(juju: jubilant.Juju):
    """Remove the consumer relation; bucket data remains (admin creds still work)."""
    juju.remove_relation(f"{APP_NAME}:s3-credentials", "consumer")
    time.sleep(5)
    juju.wait(lambda status: jubilant.all_active(status, APP_NAME), timeout=300)


def test_credential_rotation(juju: jubilant.Juju):
    """Rotate admin creds; verify new creds are returned."""
    status = juju.status()
    leader = next(name for name, u in status.apps[APP_NAME].units.items() if u.leader)

    before = juju.run(leader, "get-admin-credentials").results
    after = juju.run(leader, "rotate-admin-credentials").results
    assert before["access-key"] != after["access-key"]

    confirmed = juju.run(leader, "get-admin-credentials").results
    assert confirmed["access-key"] == after["access-key"]


def test_pre_upgrade_check(juju: jubilant.Juju):
    """pre-upgrade-check succeeds when the cluster is healthy."""
    status = juju.status()
    leader = next(name for name, u in status.apps[APP_NAME].units.items() if u.leader)
    juju.run(leader, "pre-upgrade-check")


@pytest.mark.skip(reason="requires self-signed-certificates charm in CI bundle")
def test_tls(juju: jubilant.Juju):
    """Relate self-signed-certificates; verify HTTPS endpoint."""
    juju.deploy("self-signed-certificates", channel="latest/stable")
    juju.integrate(f"{APP_NAME}:certificates", "self-signed-certificates")
    juju.wait(jubilant.all_active, timeout=600)


@pytest.mark.skip(reason="requires traefik-k8s charm in CI bundle")
def test_ingress(juju: jubilant.Juju):
    """Relate traefik-k8s; verify S3 reachable via the ingress URL."""
    juju.deploy("traefik-k8s", app="traefik", channel="latest/stable", trust=True)
    juju.integrate(f"{APP_NAME}:ingress", "traefik")
    juju.wait(jubilant.all_active, timeout=600)
