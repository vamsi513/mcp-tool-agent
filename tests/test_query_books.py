import server


def test_filter_by_author_substring(books_db):
    result = server.query_books(author="le guin")
    assert result["count"] == 3
    assert all("Le Guin" in row["author"] for row in result["results"])


def test_filters_combine_with_and(books_db):
    result = server.query_books(genre="fantasy", min_year=2015)
    assert {row["title"] for row in result["results"]} == {
        "The Fifth Season",
        "The Obelisk Gate",
        "The City We Became",
        "Gideon the Ninth",
        "Piranesi",
    }


def test_min_rating_zero_is_respected(books_db):
    # 0.0 is a real lower bound now, not "unset"
    assert server.query_books(min_rating=0)["count"] == 10  # capped by default limit
    assert server.query_books(min_rating=0, limit=20)["count"] == 20


def test_results_ordered_by_rating_desc(books_db):
    ratings = [row["rating"] for row in server.query_books(limit=20)["results"]]
    assert ratings == sorted(ratings, reverse=True)


def test_limit_is_clamped(books_db):
    assert server.query_books(limit=999)["count"] == 20
    assert server.query_books(limit=0)["count"] == 1


def test_like_wildcards_match_literally(books_db):
    assert server.query_books(author="%")["count"] == 0
    assert server.query_books(genre="_")["count"] == 0


def test_missing_database_returns_error(books_db, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "nope.db")
    result = server.query_books(author="anyone")
    assert "not found" in result["error"]
    assert result["results"] == []
