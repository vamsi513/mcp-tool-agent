import httpcore
import httpx
import pytest

import server


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_backend_refuses_non_public_literal(host):
    with pytest.raises(httpcore.ConnectError, match="non-public"):
        server._GuardedBackend().connect_tcp(host, 80)


def test_backend_pins_to_validated_ip(monkeypatch):
    calls = {}

    def fake_super(self, connect_host, port, **kwargs):
        calls["host"] = connect_host
        return "stream"

    addr = (server.socket.AF_INET, server.socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
    monkeypatch.setattr(server.socket, "getaddrinfo", lambda *a, **k: [addr])
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_super)

    server._GuardedBackend().connect_tcp("example.com", 443)
    # connected to the resolved IP, not the hostname
    assert calls["host"] == "93.184.216.34"


def test_guarded_client_blocks_private_via_httpx():
    client = httpx.Client(transport=server._GuardedTransport(), timeout=5)
    try:
        with pytest.raises(httpx.ConnectError, match="non-public"):
            client.get("http://10.1.2.3:8080/")
    finally:
        client.close()
