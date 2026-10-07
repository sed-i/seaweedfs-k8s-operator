# Copyright 2025 him
# See LICENSE file for licensing details.

import types

import utils


def test_create_bucket_sends_a_signed_put_request(monkeypatch):
    captured = {}

    class FakeConnection:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host

        def request(self, method, uri, headers=None):
            captured["method"] = method
            captured["uri"] = uri
            captured["headers"] = headers

        def getresponse(self):
            return types.SimpleNamespace(status=200)

    monkeypatch.setattr(utils.http.client, "HTTPConnection", FakeConnection)

    response = utils.create_bucket("my-bucket", access_key="ak", secret_key="sk")

    assert response.status == 200
    assert captured["method"] == "PUT"
    assert captured["uri"] == "/my-bucket"
    assert captured["headers"]["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=ak/"
    )
