"""Utility helpers for the SeaweedFS charm."""

import http.client
import secrets
import string
from typing import List, Tuple


def generate_credential_pair(length: int = 20) -> Tuple[str, str]:
    """Generate a random access key and secret key pair.

    Uses secrets.token_urlsafe for cryptographically secure randomness.

    Args:
        length: Number of random bytes to use for each credential.

    Returns:
        A tuple of (access_key, secret_key).
    """
    access_key = "SK" + secrets.token_urlsafe(length)[:length]
    secret_key = secrets.token_urlsafe(length * 2)[:length * 2]
    return access_key, secret_key


def generate_password(length: int = 32) -> str:
    """Generate a cryptographically secure random password.

    Args:
        length: Desired password length.

    Returns:
        A random password string.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_bucket(
    bucket_name: str, host: str = "localhost", port: int = 8333
) -> http.client.HTTPResponse:
    """Create an S3 bucket via the SeaweedFS S3 API.

    Args:
        bucket_name: Name of the bucket to create.
        host: Hostname of the S3 endpoint.
        port: Port of the S3 endpoint.

    Returns:
        The HTTP response from the S3 endpoint.
    """
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("PUT", f"/{bucket_name}")
    return conn.getresponse()


def check_s3_health(host: str = "localhost", port: int = 8333) -> bool:
    """Check if the S3 endpoint is reachable and responding.

    Args:
        host: Hostname of the S3 endpoint.
        port: Port of the S3 endpoint.

    Returns:
        True if S3 is reachable, False otherwise.
    """
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/")
        response = conn.getresponse()
        return 200 <= response.status < 500
    except (ConnectionError, OSError):
        return False


def get_cluster_peers(peer_addresses: List[str], my_address: str) -> List[str]:
    """Filter out our own address from the list of peer addresses.

    Args:
        peer_addresses: All known peer addresses (ip:port format).
        my_address: Our own address (ip:port format).

    Returns:
        List of other peer addresses, sorted and deduplicated.
    """
    others = sorted({addr for addr in peer_addresses if addr and addr != my_address})
    return others
