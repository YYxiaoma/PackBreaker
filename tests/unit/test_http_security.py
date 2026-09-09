from backend.app.infrastructure.http_security import TrustedProxyPolicy


def test_untrusted_peer_cannot_spoof_forwarded_headers() -> None:
    policy = TrustedProxyPolicy(("10.0.0.0/8",))

    context = policy.resolve(
        direct_host="203.0.113.10",
        direct_scheme="http",
        forwarded_for="198.51.100.7",
        forwarded_proto="https",
    )

    assert context.client_source == "203.0.113.10"
    assert context.scheme == "http"
    assert not context.trusted_proxy


def test_trusted_proxy_chain_resolves_first_untrusted_client_from_right() -> None:
    policy = TrustedProxyPolicy(("10.0.0.0/8", "192.0.2.0/24"))

    context = policy.resolve(
        direct_host="10.0.0.5",
        direct_scheme="http",
        forwarded_for="198.51.100.8, 192.0.2.20",
        forwarded_proto="https",
    )

    assert context.client_source == "198.51.100.8"
    assert context.scheme == "https"
    assert context.trusted_proxy


def test_invalid_forwarded_chain_falls_back_to_direct_proxy_address() -> None:
    policy = TrustedProxyPolicy(("10.0.0.0/8",))

    context = policy.resolve(
        direct_host="10.0.0.5",
        direct_scheme="http",
        forwarded_for="198.51.100.8, invalid",
        forwarded_proto="https",
    )

    assert context.client_source == "10.0.0.5"
    assert context.scheme == "https"
