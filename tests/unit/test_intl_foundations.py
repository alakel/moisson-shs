"""
Tests des briques transverses de l'extension internationale :
utils.names, mappers.scholarly_types, utils.secrets, utils.http_scholarly.
"""

import pytest

from utils.names import split_display_name
from mappers.scholarly_types import (
    crossref_type_to_zotero, openalex_type_to_zotero,
    s2_type_to_zotero, core_type_to_zotero,
)


# === utils.names ===

class TestSplitDisplayName:
    def test_simple(self):
        assert split_display_name("Patrick Charaudeau") == ("Patrick", "Charaudeau")

    def test_single_token(self):
        assert split_display_name("Foucault") == ("", "Foucault")

    def test_empty(self):
        assert split_display_name("") == ("", "")
        assert split_display_name(None) == ("", "")

    def test_three_tokens(self):
        assert split_display_name("Jean Pierre Dupont") == ("Jean Pierre", "Dupont")

    def test_particle_attached_to_last_name(self):
        assert split_display_name("Pierre van der Berg") == ("Pierre", "van der Berg")
        assert split_display_name("Ludwig von Mises") == ("Ludwig", "von Mises")

    def test_strips_surrounding_space(self):
        assert split_display_name("  Jane  Doe  ") == ("Jane", "Doe")


# === mappers.scholarly_types ===

class TestScholarlyTypes:
    def test_crossref_known(self):
        assert crossref_type_to_zotero("journal-article") == "journalArticle"
        assert crossref_type_to_zotero("posted-content") == "preprint"
        assert crossref_type_to_zotero("dataset") == "dataset"
        assert crossref_type_to_zotero("book-chapter") == "bookSection"

    def test_crossref_unknown_and_empty(self):
        assert crossref_type_to_zotero("nonsense") == "document"
        assert crossref_type_to_zotero("") == "document"
        assert crossref_type_to_zotero(None) == "document"

    def test_openalex(self):
        assert openalex_type_to_zotero("article") == "journalArticle"
        assert openalex_type_to_zotero("preprint") == "preprint"
        assert openalex_type_to_zotero("dataset") == "dataset"
        assert openalex_type_to_zotero(None) == "document"

    def test_s2_first_recognized(self):
        assert s2_type_to_zotero(["JournalArticle", "Review"]) == "journalArticle"
        assert s2_type_to_zotero(["Conference"]) == "conferencePaper"
        assert s2_type_to_zotero(["Dataset"]) == "dataset"
        assert s2_type_to_zotero([]) == "document"
        assert s2_type_to_zotero(None) == "document"
        # type non reconnu en tête, reconnu ensuite
        assert s2_type_to_zotero(["Unknown", "Book"]) == "book"

    def test_core(self):
        assert core_type_to_zotero("research") == "journalArticle"
        assert core_type_to_zotero("thesis") == "thesis"
        assert core_type_to_zotero("presentation") == "presentation"
        assert core_type_to_zotero("") == "document"


# === utils.secrets ===

class TestSecrets:
    def test_returns_value(self, monkeypatch):
        from utils.secrets import get_api_key
        monkeypatch.setenv("MOISSON_TEST_KEY", "abc123")
        assert get_api_key("MOISSON_TEST_KEY") == "abc123"

    def test_strips_whitespace(self, monkeypatch):
        from utils.secrets import get_api_key
        monkeypatch.setenv("MOISSON_TEST_KEY", "  abc  ")
        assert get_api_key("MOISSON_TEST_KEY") == "abc"

    def test_missing_optional_returns_none(self, monkeypatch):
        from utils.secrets import get_api_key
        monkeypatch.delenv("MOISSON_ABSENT_KEY", raising=False)
        assert get_api_key("MOISSON_ABSENT_KEY") is None

    def test_missing_required_raises(self, monkeypatch):
        from utils.secrets import get_api_key, MissingAPIKey
        monkeypatch.delenv("MOISSON_ABSENT_KEY", raising=False)
        with pytest.raises(MissingAPIKey):
            get_api_key("MOISSON_ABSENT_KEY", required=True)

    def test_has_api_key(self, monkeypatch):
        from utils.secrets import has_api_key
        monkeypatch.setenv("MOISSON_TEST_KEY", "x")
        assert has_api_key("MOISSON_TEST_KEY") is True
        monkeypatch.setenv("MOISSON_TEST_KEY", "")
        assert has_api_key("MOISSON_TEST_KEY") is False


# === utils.http_scholarly ===

class _FakeResp:
    def __init__(self, status_code, headers=None, payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    """Session jouant une file de réponses prédéfinies."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return self._responses.pop(0)


class TestHttpScholarly:
    def test_200_returns_json(self):
        from utils.http_scholarly import get_json
        sess = _FakeSession([_FakeResp(200, payload={"ok": True})])
        assert get_json("http://x", session=sess) == {"ok": True}
        assert sess.calls == 1

    def test_409_raises_budget_exhausted(self):
        from utils.http_scholarly import get_json, BudgetExhausted
        sess = _FakeSession([_FakeResp(409, text="payment required")])
        with pytest.raises(BudgetExhausted):
            get_json("http://x", session=sess)

    def test_402_raises_budget_exhausted(self):
        from utils.http_scholarly import get_json, BudgetExhausted
        sess = _FakeSession([_FakeResp(402)])
        with pytest.raises(BudgetExhausted):
            get_json("http://x", session=sess)

    def test_429_zero_budget_raises_budget_exhausted(self):
        from utils.http_scholarly import get_json, BudgetExhausted
        sess = _FakeSession([
            _FakeResp(429, headers={"X-RateLimit-Prepaid-Remaining-USD": "0"})
        ])
        with pytest.raises(BudgetExhausted):
            get_json("http://x", session=sess)

    def test_429_long_retry_after_raises(self):
        from utils.http_scholarly import get_json, BudgetExhausted
        sess = _FakeSession([_FakeResp(429, headers={"Retry-After": "7200"})])
        with pytest.raises(BudgetExhausted):
            get_json("http://x", session=sess)

    def test_429_then_200_retries(self, monkeypatch):
        from utils import http_scholarly
        monkeypatch.setattr(http_scholarly.time, "sleep", lambda s: None)
        sess = _FakeSession([
            _FakeResp(429, headers={"Retry-After": "1"}),
            _FakeResp(200, payload={"ok": 1}),
        ])
        assert http_scholarly.get_json("http://x", session=sess) == {"ok": 1}
        assert sess.calls == 2

    def test_5xx_then_200_retries(self, monkeypatch):
        from utils import http_scholarly
        monkeypatch.setattr(http_scholarly.time, "sleep", lambda s: None)
        sess = _FakeSession([
            _FakeResp(503),
            _FakeResp(200, payload={"ok": 2}),
        ])
        assert http_scholarly.get_json("http://x", session=sess) == {"ok": 2}
        assert sess.calls == 2

    def test_429_retries_exhausted_raises_http_error(self, monkeypatch):
        from utils import http_scholarly
        import requests
        monkeypatch.setattr(http_scholarly.time, "sleep", lambda s: None)
        sess = _FakeSession([
            _FakeResp(429, headers={"Retry-After": "1"}) for _ in range(3)
        ])
        with pytest.raises(requests.HTTPError):
            http_scholarly.get_json("http://x", session=sess, max_retries=3)
