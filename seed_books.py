"""Create books.db with a small fixed set of rows used by the query_books tool."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "books.db"

BOOKS = [
    ("The Left Hand of Darkness", "Ursula K. Le Guin", 1969, "science fiction", 4.6),
    ("A Wizard of Earthsea", "Ursula K. Le Guin", 1968, "fantasy", 4.4),
    ("The Dispossessed", "Ursula K. Le Guin", 1974, "science fiction", 4.5),
    ("Neuromancer", "William Gibson", 1984, "science fiction", 4.2),
    ("Pattern Recognition", "William Gibson", 2003, "science fiction", 4.0),
    ("Kindred", "Octavia E. Butler", 1979, "science fiction", 4.7),
    ("Parable of the Sower", "Octavia E. Butler", 1993, "science fiction", 4.6),
    ("Dawn", "Octavia E. Butler", 1987, "science fiction", 4.3),
    ("The Fifth Season", "N. K. Jemisin", 2015, "fantasy", 4.5),
    ("The Obelisk Gate", "N. K. Jemisin", 2016, "fantasy", 4.4),
    ("The City We Became", "N. K. Jemisin", 2020, "fantasy", 4.1),
    ("Gideon the Ninth", "Tamsyn Muir", 2019, "fantasy", 4.3),
    ("Piranesi", "Susanna Clarke", 2020, "fantasy", 4.5),
    ("Jonathan Strange & Mr Norrell", "Susanna Clarke", 2004, "fantasy", 4.2),
    ("Station Eleven", "Emily St. John Mandel", 2014, "literary", 4.4),
    ("Sea of Tranquility", "Emily St. John Mandel", 2022, "science fiction", 4.3),
    ("Project Hail Mary", "Andy Weir", 2021, "science fiction", 4.7),
    ("The Martian", "Andy Weir", 2011, "science fiction", 4.6),
    ("Klara and the Sun", "Kazuo Ishiguro", 2021, "literary", 4.0),
    ("Never Let Me Go", "Kazuo Ishiguro", 2005, "literary", 4.3),
]


def build():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE books (
            title  TEXT NOT NULL,
            author TEXT NOT NULL,
            year   INTEGER NOT NULL,
            genre  TEXT NOT NULL,
            rating REAL NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO books (title, author, year, genre, rating) VALUES (?, ?, ?, ?, ?)",
        BOOKS,
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    conn.close()
    print(f"wrote {count} rows to {DB_PATH}")


if __name__ == "__main__":
    build()
