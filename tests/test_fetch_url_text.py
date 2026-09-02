import httpx
import pytest

import server


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
    ],
)
def test_rejects_unsafe_urls(url):
    result = server.fetch_url_text(url)
    assert result["text"] == ""
    assert result["error"]


def test_rejects_public_host_that_resolves_to_private_ip(monkeypatch):
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda *a, **k: [(server.socket.AF_INET, None, None, "", ("10.1.2.3", 0))],
    )
    result = server.fetch_url_text("http://sneaky.example/")
    assert "non-public address" in result["error"]


def test_happy_path_extracts_title_and_text(mock_http):
    html = (
        "<html><head><title> Hi </title></head>"
        "<body><p>Hello world</p><script>x</script></body></html>"
    )
    mock_http(lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = server.fetch_url_text("https://example.com/page")
    assert result["title"] == "Hi"
    assert result["text"] == "Hi Hello world"
    assert result["truncated"] is False


def test_non_text_content_type_is_rejected(mock_http):
    mock_http(
        lambda req: httpx.Response(200, headers={"content-type": "application/json"}, text="{}")
    )
    error = server.fetch_url_text("https://example.com/data")["error"]
    assert "unsupported content type" in error


def test_response_larger_than_cap_is_rejected(mock_http):
    big = "a" * (server.MAX_RESPONSE_BYTES + 1)
    mock_http(lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=big))
    assert server.fetch_url_text("https://example.com/big")["error"] == "response too large"


def test_http_error_status_is_reported(mock_http):
    mock_http(lambda req: httpx.Response(404, headers={"content-type": "text/html"}, text="nope"))
    assert server.fetch_url_text("https://example.com/missing")["error"] == "HTTP 404"


def test_redirect_hop_to_internal_address_is_blocked(mock_http, monkeypatch):
    # First hop resolves to a public IP and passes the check; the redirect
    # target must then be re-checked and rejected.
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(server.socket.AF_INET, None, None, "", ("93.184.216.34", 0))]
        if host == "start.example"
        else [(server.socket.AF_INET, None, None, "", ("169.254.169.254", 0))],
    )

    def handler(req):
        if req.url.host == "start.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        raise AssertionError("should never reach the redirect target")

    mock_http(handler)
    result = server.fetch_url_text("https://start.example/")
    assert "non-public address" in result["error"]
