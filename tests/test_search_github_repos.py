import httpx

import server


def _repo(name, description="", stars=0, language="Python"):
    return {
        "name": name,
        "full_name": f"octocat/{name}",
        "description": description,
        "stargazers_count": stars,
        "language": language,
        "html_url": f"https://github.com/octocat/{name}",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def test_user_not_found(mock_http):
    mock_http(lambda req: httpx.Response(404, json={"message": "Not Found"}))
    result = server.search_github_repos("ghost")
    assert result["error"] == "GitHub user 'ghost' not found"
    assert result["results"] == []


def test_non_200_is_surfaced(mock_http):
    mock_http(lambda req: httpx.Response(403, text="rate limited"))
    result = server.search_github_repos("octocat")
    assert "403" in result["error"]


def test_query_filters_by_name_or_description(mock_http):
    repos = [
        _repo("alpha", "the alpha project"),
        _repo("beta", "unrelated"),
        _repo("gamma", "mentions alpha in the description"),
    ]
    mock_http(lambda req: httpx.Response(200, json=repos))
    names = {r["name"] for r in server.search_github_repos("octocat", query="alpha")["results"]}
    assert names == {"octocat/alpha", "octocat/gamma"}


def test_limit_is_clamped(mock_http):
    repos = [_repo(f"r{i}") for i in range(50)]
    mock_http(lambda req: httpx.Response(200, json=repos))
    assert server.search_github_repos("octocat", limit=999)["count"] == 20


def test_follows_link_header_pagination(mock_http):
    page1 = [_repo(f"a{i}") for i in range(100)]
    page2 = [_repo("needle", "found on page two")]

    def handler(req):
        if req.url.params.get("page") == "2":
            return httpx.Response(200, json=page2)
        return httpx.Response(
            200,
            json=page1,
            headers={"link": f'<{server.GITHUB_API}/x?page=2>; rel="next"'},
        )

    mock_http(handler)
    result = server.search_github_repos("octocat", query="needle")
    assert result["count"] == 1
    assert result["results"][0]["name"] == "octocat/needle"
