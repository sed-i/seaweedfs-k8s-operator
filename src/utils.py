"""Utility helpers for the SeaweedFS charm.

Network calls here rely on the Juju sidecar-charm networking model: the charm
container and the workload container share the same Pod network namespace,
so ``localhost`` from the charm process reaches the workload's ports.
"""

from __future__ import annotations

import http.client
import json
import secrets
from typing import Optional, Tuple

import ops


def generate_credentials() -> Tuple[str, str]:
    """Generate a new (access-key, secret-key) pair.

    Per spec §4.2: ``secrets.token_urlsafe(16)``.
    """
    return secrets.token_urlsafe(16), secrets.token_urlsafe(16)


def parse_weed_version(version_output: str) -> Optional[str]:
    """Parse the version string out of ``weed version`` output.

    Example output: ``version 30GB 3.97 76452ab59 linux amd64``.
    """
    import re

    result = re.search(r"version.*\s(\d+\.\d+\.?\d*)", version_output)
    if result is None:
        return None
    return result.group(1)


def cluster_status(host: str = "localhost", port: int = 9333) -> Optional[dict]:
    """Query the local master's ``/cluster/status`` endpoint.

    Returns None if the master is unreachable (e.g. not started yet).
    """
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/cluster/status")
        response = conn.getresponse()
        body = response.read()
    except OSError:
        return None

    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None


def has_raft_leader(status: Optional[dict]) -> bool:
    """Return True if the given ``/cluster/status`` payload shows a Raft leader."""
    if not status:
        return False
    return bool(status.get("Leader"))


def http_ok(host: str = "localhost", port: int = 8333, path: str = "/healthz") -> bool:
    """Return True if an HTTP GET to the given local endpoint returns a 2xx status."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
    except OSError:
        return False
    return 200 <= response.status < 300


def exec_weed_shell(
    container: ops.Container, commands: list, timeout: float = 30.0
) -> Tuple[str, Optional[str]]:
    """Run one or more ``weed shell`` commands against the local cluster.

    Args:
        container: The workload container.
        commands: A list of weed-shell command lines, e.g.
            ``["s3.bucket.create -name foo"]``.
        timeout: Bounded timeout in seconds; callers should treat a timeout
            as non-fatal (log and proceed) per spec §2.4.

    Returns:
        (stdout, stderr) of the shell invocation.

    Raises:
        ops.pebble.ExecError: on nonzero exit.
        TimeoutError: if the command exceeds `timeout`.
    """
    script = "\n".join(commands) + "\n"
    process = container.exec(
        ["/usr/bin/weed", "shell"],
        stdin=script,
        timeout=timeout,
    )
    return process.wait_output()
