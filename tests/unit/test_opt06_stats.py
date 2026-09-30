"""
OPT-06 (C19, C24) : statistiques exactes. DOI lu dans le champ ou dans
``extra``, extrait Google Scholar distingué d'un résumé, auteurs tronqués
comptés, sources normalisées ; toutes les clés historiques conservées.
"""

from main import compute_stats, write_stats_file

HISTORIC_KEYS = {"total_items", "by_type", "by_source", "by_year",
                 "by_language", "with_doi", "with_abstract",
                 "doi_coverage_pct", "abstract_coverage_pct", "source_overlap"}

SNIPPET = ("Scholar snippet: le champ résumé contient un extrait Google "
           "Scholar, non le résumé de l'éditeur")
TRUNC = "Scholar truncation: auteurs et/ou revue tronqués par Google Scholar"


def _items():
    return [
        # Livre : DOI migré dans extra par validate_and_clean.
        {"itemType": "book", "title": "A", "date": "2018", "DOI": "",
         "extra": "Sources: OpenEdition\nDOI: 10.4000/books.pufc.40815"},
        # Notice Scholar : extrait et auteurs tronqués.
        {"itemType": "journalArticle", "title": "B", "date": "2024",
         "DOI": "10.1/b", "abstractNote": "extrait…",
         "creators": [{"lastName": "X"}],
         "extra": f"Sources: Google Scholar (SerpAPI)\n{SNIPPET}\n{TRUNC}"},
        # Vrai résumé ; deux graphies de CrossRef.
        {"itemType": "journalArticle", "title": "C", "date": "2020",
         "abstractNote": "Résumé de l'éditeur", "extra": "Sources: Crossref, HAL"},
        {"itemType": "journalArticle", "title": "D", "date": "2020",
         "extra": "Sources: CrossRef\nDOI source: appariement bibliographique"},
    ]


def test_historic_keys_kept():
    assert HISTORIC_KEYS <= set(compute_stats(_items()))


def test_doi_counted_from_field_or_extra():
    """Échoue sur l'ancien calcul (champ DOI seul)."""
    stats = compute_stats(_items())
    assert stats["with_doi"] == 2
    assert stats["with_doi_field"] == 1
    assert stats["with_doi_extra_only"] == 1
    assert stats["doi_coverage_pct"] == 50.0


def test_scholar_snippet_is_not_an_abstract():
    """Échoue sur l'ancien calcul (tout abstractNote non vide)."""
    stats = compute_stats(_items())
    assert stats["with_abstract"] == 1
    assert stats["with_abstract_field"] == 2
    assert stats["with_scholar_snippet"] == 1
    assert stats["abstract_coverage_pct"] == 25.0


def test_truncated_authors_counted():
    stats = compute_stats(_items())
    assert stats["truncated_authors"] == 1
    assert stats["truncated_authors_pct"] == 25.0


def test_sources_normalized():
    stats = compute_stats(_items())
    assert stats["by_source"] == {"CrossRef": 2, "OpenEdition": 1,
                                  "Google Scholar": 1, "HAL": 1}
    assert stats["by_source_key"]["crossref"] == 2
    assert stats["source_overlap"] == {"crossref + hal": 1}


def test_merged_record_with_crossref_abstract_is_a_true_abstract():
    from utils.deduplication import deduplicate
    scholar = {"itemType": "journalArticle", "title": "Un titre suffisamment long pour tout",
               "date": "2020", "DOI": "10.1/m", "abstractNote": "extrait…",
               "creators": [{"lastName": "X"}],
               "extra": f"Sources: Google Scholar (SerpAPI)\n{SNIPPET}"}
    crossref = dict(scholar, abstractNote="Résumé éditeur", extra="Sources: CrossRef")
    merged = deduplicate([scholar, crossref])
    stats = compute_stats(merged)
    assert stats["with_abstract"] == 1 and stats["with_scholar_snippet"] == 0
    assert stats["merged_items"] == 1


def test_stats_file_shows_new_counts(tmp_path):
    path = tmp_path / "s_stats.txt"
    write_stats_file(compute_stats(_items()), path, "requête")
    text = path.read_text(encoding="utf-8")
    assert "Items avec DOI : 2 (50.0%)" in text
    assert "dont DOI lu dans 'extra' seulement : 1" in text
    assert "Items avec extrait Google Scholar (pas un résumé) : 1" in text
    assert "tronqués par Google Scholar : 1" in text
    assert "  CrossRef : 2" in text


def test_stats_file_accepts_older_stats_dict(tmp_path):
    old = {k: v for k, v in compute_stats(_items()).items() if k in HISTORIC_KEYS}
    path = tmp_path / "old_stats.txt"
    write_stats_file(old, path, "q")
    assert "Total items : 4" in path.read_text(encoding="utf-8")
