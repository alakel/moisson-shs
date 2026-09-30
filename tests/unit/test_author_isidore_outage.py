"""
PAT-05 (retour d'expérience du 2026-09-30, B5) : une coupure d'Isidore dans
``search_by_author`` est une erreur réseau, pas une « erreur inattendue :
TypeError ».

Cause établie le 2026-09-30 : le compte rendu du flux Isidore publiait le
compteur ``total_available`` à ``None`` (aucune page reçue, ou réponse sans
``meta.@items``) et ``_apply_report`` additionnait les compteurs
(``0 + None``). Le vrai ``IsidoreConnector`` est exercé ici, derrière un
client HTTP simulé ; les doublures des tests d'OPT-14 publiaient un compte
rendu sans compteurs et ne passaient jamais par ce chemin.
"""

from __future__ import annotations

import pytest
import requests

import utils.lookups as lookups
from connectors.isidore import report_from_scan
from connectors.isidore_scan import IsidoreScanStatus

NAME = "Amar Lakel"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Schéma Zotero neutralisé ; aucune clé (OpenAlex et CORE ignorés)."""
    import mappers.zotero_schema_loader as zsl
    monkeypatch.setattr(zsl, "load_zotero_schema", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(lookups, "get_api_key",
                        lambda name, required=False: None)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.from_cache = False
        self.content = b"{}"

    def json(self):
        return self._payload


class Client:
    """Client simulé : Isidore selon le scénario, les autres hôtes en échec
    réseau (HAL, Semantic Scholar)."""

    cancel_signal = None

    def __init__(self, isidore):
        self.isidore = isidore

    def get(self, url, params=None, headers=None, **kwargs):
        if "isidore" in url:
            answer = self.isidore
        else:
            answer = requests.ConnectionError("Connection reset by peer")
        if isinstance(answer, int):
            raise requests.HTTPError(response=_Resp({}, answer))
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)

    def close(self):
        pass


def _isidore_result(client):
    out = lookups.search_by_author(NAME, http_client=client)
    return {r.display_name: r for r in out.report.sources}["Isidore"]


class TestIsidoreOutage:
    def test_connection_reset_is_network_error(self):
        client = Client(requests.ConnectionError(
            ConnectionResetError(54, "Connection reset by peer")))
        res = _isidore_result(client)
        assert res.status == "error"
        assert res.reason == "network_error", (res.reason, res.detail)
        assert "ConnectionError" in (res.detail or "")
        assert "TypeError" not in (res.detail or "")

    def test_page1_http_503_is_http_error(self):
        res = _isidore_result(Client(503))
        assert res.status == "error"
        assert res.reason == "http_error", (res.reason, res.detail)

    def test_reply_without_total_is_not_an_error(self):
        """Réponse 200 sans ``meta.@items`` : aucune panne, aucune erreur."""
        payload = {"response": {"replies": {"meta": {}, "content": {}}}}
        res = _isidore_result(Client(payload))
        assert res.status in ("complete", "empty"), (res.status, res.reason,
                                                     res.detail)
        assert "total_available" not in res.counters


def test_report_from_scan_error_counters_are_numeric():
    status = IsidoreScanStatus(source="isidore", query="\"Amar Lakel\"", cap=10)
    status.error = "ConnectionError"
    status.error_reason = "network_error"
    status.error_detail = "page 1 : ConnectionError"
    report = report_from_scan(status, "Isidore")
    assert report.status == "error"
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in report.counters.values()), report.counters
