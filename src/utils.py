"""Utility helpers for the SeaweedFS charm."""

import http.client


def create_bucket(bucket_name: str, host: str = "localhost", port: int = 8333) -> http.client.HTTPResponse:
    """Create an S3 bucket via the SeaweedFS S3 API.

    Args:
        bucket_name: Name of the bucket to create.
        host: Hostname of the S3 endpoint.
        port: Port of the S3 endpoint.

    Returns:
        The HTTP response from the S3 endpoint.
    """
    conn = http.client.HTTPConnection(host, port)
    conn.request("PUT", f"/{bucket_name}")
    return conn.getresponse()
