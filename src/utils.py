"""Utility helpers for the SeaweedFS charm."""

import hashlib
import hmac
import http.client
from datetime import datetime, timezone
from urllib.parse import quote

S3_SERVICE = "s3"


def _sign(key: bytes, message: str) -> bytes:
    """Derive one AWS SigV4 signing key step."""
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    """Derive the AWS SigV4 signing key for a request scope."""
    key = _sign(("AWS4" + secret_key).encode(), datestamp)
    key = _sign(key, region)
    key = _sign(key, service)
    return _sign(key, "aws4_request")


def create_bucket(
    bucket_name: str,
    access_key: str = "",
    secret_key: str = "",
    host: str = "localhost",
    port: int = 8333,
    region: str = "us-east-1",
    timeout: float = 5.0,
) -> http.client.HTTPResponse:
    """Create an S3 bucket through the SeaweedFS S3 API.

    The request is signed with AWS Signature Version 4 because bucket creation
    is an administrative operation and the S3 endpoint is not left open to
    anonymous callers. SeaweedFS ignores the region but it is part of the
    signature scope, so any stable value works.
    """
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_uri = "/" + quote(bucket_name, safe="")
    host_header = f"{host}:{port}"

    canonical_headers = (
        f"host:{host_header}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        ["PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{datestamp}/{region}/{S3_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, S3_SERVICE),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.request(
        "PUT",
        canonical_uri,
        headers={
            "Host": host_header,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": authorization,
        },
    )
    return conn.getresponse()
