# Copyright 2025 him
# See LICENSE file for licensing details.

import string

import pytest

from utils import (
    _sigv4_headers,
    cluster_peers,
    generate_credential,
    num_master_units,
    peer_unit_fqdn,
    replication_placement,
)

OWN_FQDN = "swfs-0.swfs-endpoints.welcome-k8s.svc.cluster.local"


class TestGenerateCredential:
    def test_default_length(self):
        assert len(generate_credential()) == 24

    def test_custom_length(self):
        assert len(generate_credential(40)) == 40

    def test_alphabet_is_url_safe(self):
        allowed = set(string.ascii_letters + string.digits)
        credential = generate_credential(200)
        assert set(credential) <= allowed

    def test_randomness(self):
        # Vanishingly unlikely to collide; guards against a constant stub.
        assert generate_credential() != generate_credential()


class TestPeerUnitFqdn:
    def test_derives_sibling_from_own_fqdn(self):
        assert (
            peer_unit_fqdn(OWN_FQDN, 2)
            == "swfs-2.swfs-endpoints.welcome-k8s.svc.cluster.local"
        )

    def test_same_unit_number_returns_itself(self):
        assert peer_unit_fqdn(OWN_FQDN, 0) == OWN_FQDN

    def test_app_name_with_hyphens(self):
        fqdn = "my-app-1.my-app-endpoints.ns.svc.cluster.local"
        assert (
            peer_unit_fqdn(fqdn, 3) == "my-app-3.my-app-endpoints.ns.svc.cluster.local"
        )


class TestNumMasterUnits:
    @pytest.mark.parametrize(
        "planned_units,expected",
        [
            (0, 1),
            (1, 1),
            (2, 1),
            (3, 3),
            (4, 3),
            (5, 5),
            (6, 5),
            (7, 5),
            (100, 5),
        ],
    )
    def test_odd_and_capped(self, planned_units, expected):
        assert num_master_units(planned_units) == expected

    def test_custom_max_masters(self):
        assert num_master_units(10, max_masters=7) == 7
        assert num_master_units(10, max_masters=4) == 3


class TestClusterPeers:
    def test_single_unit(self):
        assert cluster_peers(OWN_FQDN, 1, port=9333) == [
            "swfs-0.swfs-endpoints.welcome-k8s.svc.cluster.local:9333"
        ]

    def test_multiple_units(self):
        peers = cluster_peers(OWN_FQDN, 3, port=9333)
        assert peers == [
            "swfs-0.swfs-endpoints.welcome-k8s.svc.cluster.local:9333",
            "swfs-1.swfs-endpoints.welcome-k8s.svc.cluster.local:9333",
            "swfs-2.swfs-endpoints.welcome-k8s.svc.cluster.local:9333",
        ]

    def test_zero_clamped_to_one(self):
        assert cluster_peers(OWN_FQDN, 0, port=9333) == [
            "swfs-0.swfs-endpoints.welcome-k8s.svc.cluster.local:9333"
        ]


class TestReplicationPlacement:
    @pytest.mark.parametrize(
        "unit_count,expected",
        [
            (1, "000"),
            (2, "001"),
            (3, "002"),
            (4, "002"),
            (10, "002"),
        ],
    )
    def test_scales_with_unit_count(self, unit_count, expected):
        assert replication_placement(unit_count) == expected


class TestSigv4Headers:
    def test_contains_expected_headers(self):
        headers = _sigv4_headers(
            "PUT", "/mybucket", "localhost:8333", "AKEY", "SKEY", payload=b""
        )
        assert set(headers) == {
            "Host",
            "X-Amz-Date",
            "X-Amz-Content-Sha256",
            "Authorization",
        }
        assert headers["Host"] == "localhost:8333"
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKEY/")
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in headers["Authorization"]

    def test_signature_changes_with_secret_key(self):
        def headers_for(secret_key: str) -> dict[str, str]:
            return _sigv4_headers(
                "PUT", "/b", "localhost:8333", "AKEY", secret_key
            )

        headers_a = headers_for("SECRET-A")
        headers_b = headers_for("SECRET-B")
        assert headers_a["Authorization"] != headers_b["Authorization"]
