"""Utility helpers for the SeaweedFS charm.

These helpers are kept free of any ``ops`` imports so that they can be
unit-tested in isolation and reused from anywhere in the charm.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import http.client
import secrets
import string
from urllib.parse import quote

MASTER_PORT = 9333
VOLUME_PORT = 8080
FILER_PORT = 8888
S3_PORT = 8333

# Keep requests scoped to an arbitrary-but-fixed AWS region/service. SeaweedFS
# does not validate the region, it just needs the client and server to agree.
_SIGV4_REGION = "us-east-1"
_SIGV4_SERVICE = "s3"

# Access/secret keys use a conservative, URL- and shell-safe alphabet so that
# generated credentials never need extra quoting by downstream tooling.
_CREDENTIAL_ALPHABET = string.ascii_letters + string.digits


def generate_credential(length: int = 24) -> str:
    """Generate a random, URL-safe credential (access key or secret key)."""
    return "".join(secrets.choice(_CREDENTIAL_ALPHABET) for _ in range(length))


def peer_unit_fqdn(own_fqdn: str, unit_number: int) -> str:
    """Return the stable Kubernetes DNS name of a peer unit.

    Juju k8s sidecar charms are backed by a ``StatefulSet`` fronted by a
    headless "endpoints" service, so every unit has a predictable and stable
    hostname of the form
    ``<app>-<unit-number>.<app>-endpoints.<namespace>.svc.cluster.local``.

    This derives that name for an arbitrary unit number from the *current*
    unit's own FQDN (as returned by ``socket.getfqdn()``), without needing any
    peer relation data exchange.

    Args:
        own_fqdn: The FQDN of the unit calling this function.
        unit_number: The Juju unit number (the suffix after ``/``) to build
            the FQDN for.
    """
    own_host, _, domain = own_fqdn.partition(".")
    # own_host looks like "<app>-<N>"; strip the trailing "-<N>" to get the
    # application name, which is also the first label of the headless svc.
    app_name, _, _ = own_host.rpartition("-")
    return f"{app_name}-{unit_number}.{domain}"


def num_master_units(planned_units: int, max_masters: int = 5) -> int:
    """Compute how many units should run the SeaweedFS master/Raft role.

    SeaweedFS masters use Raft for leader election, which (like every Raft
    implementation) only tolerates an *odd* number of voting members -- in
    fact ``weed`` refuses to start at all otherwise ("Only odd number of
    masters are supported"). So as the charm scales out, only the lowest
    numbered units (0, 1, 2, ...) up to the largest odd number (capped at
    ``max_masters`` to bound Raft overhead) run the master role; any
    additional units join purely as volume/filer/S3 servers that talk to
    that fixed set of masters.

    Args:
        planned_units: Number of units Juju has planned for this application.
        max_masters: Upper bound on the number of master-eligible units.
    """
    candidate = min(max(planned_units, 1), max_masters)
    if candidate % 2 == 0:
        candidate -= 1
    return max(candidate, 1)


def cluster_peers(own_fqdn: str, unit_count: int, port: int = MASTER_PORT) -> list[str]:
    """Return ``host:port`` for a set of units expected to be part of the cluster.

    Args:
        own_fqdn: The FQDN of the unit calling this function.
        unit_count: Number of (lowest-numbered) units to build addresses for.
        port: TCP port to append to each peer address.
    """
    unit_count = max(unit_count, 1)
    return [f"{peer_unit_fqdn(own_fqdn, i)}:{port}" for i in range(unit_count)]


def replication_placement(unit_count: int) -> str:
    """Compute a sane default SeaweedFS replica placement string.

    The placement string is three digits ``XYZ`` meaning extra copies in
    other (X) data centers, (Y) racks, and (Z) servers, respectively. As this
    charm only ever runs within a single Juju model/k8s namespace, only the
    "other servers" digit is relevant here.

    Mirrors how most distributed storage systems (e.g. Ceph) default to
    keeping 3 copies of data once enough failure domains (here: units) exist,
    while avoiding pointless replication (and the 3x storage overhead that
    comes with it) on single-unit deployments typically used for testing.
    """
    extra_copies = min(max(unit_count - 1, 0), 2)
    return f"00{extra_copies}"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def _sigv4_headers(
    method: str,
    canonical_uri: str,
    host: str,
    access_key: str,
    secret_key: str,
    payload: bytes = b"",
) -> dict[str, str]:
    """Build the minimal set of headers needed to sign an S3 REST request.

    Implements AWS Signature Version 4 using only the standard library, so
    that the charm does not need to depend on ``boto3``/``botocore`` just to
    perform a couple of administrative bucket operations against the local
    SeaweedFS S3 gateway.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = _sha256_hex(payload)

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )

    credential_scope = f"{date_stamp}/{_SIGV4_REGION}/{_SIGV4_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )

    signing_key = _sigv4_signing_key(secret_key, date_stamp, _SIGV4_REGION, _SIGV4_SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    return {
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }


def create_bucket(
    bucket_name: str,
    access_key: str,
    secret_key: str,
    host: str = "localhost",
    port: int = S3_PORT,
) -> http.client.HTTPResponse:
    """Create an S3 bucket via the SeaweedFS S3 API, authenticated with SigV4.

    Args:
        bucket_name: Name of the bucket to create.
        access_key: Access key of an identity allowed to create buckets.
        secret_key: Secret key of that identity.
        host: Hostname of the S3 endpoint.
        port: Port of the S3 endpoint.

    Returns:
        The HTTP response from the S3 endpoint.
    """
    canonical_uri = quote(f"/{bucket_name}", safe="/")
    hostname = f"{host}:{port}"
    headers = _sigv4_headers("PUT", canonical_uri, hostname, access_key, secret_key)

    conn = http.client.HTTPConnection(host, port)
    conn.request("PUT", canonical_uri, headers=headers)
    return conn.getresponse()
