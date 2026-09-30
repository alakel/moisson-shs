"""
Tests du Sprint 09 — Opérations longues : progression, annulation, deadline,
concurrence, plafond anti-DoS.

Couvre la DoD :
- Progression : ≥ 1 notification par source (via le pont `ProgressReporter`).
- Annulation coopérative : l'`Event` interrompt avant la source suivante, aucun
  corpus final écrit, raw/ partiels nettoyés.
- Deadline serveur : dépassement → résultat d'erreur propre, pas de thread
  fantôme (on attend la fin réelle du thread).
- Concurrence bornée : sémaphore plein → refus explicite (« serveur occupé »).
- Plafond serveur : `max_per_source` clampé en HTTP, libre en stdio.

Aucun réseau (run_source est stubé ; depuis OPT-07, celui du moteur :
``engine.orchestrator.run_source``).
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _items(name, n=2):
    return [{"itemType": "journalArticle", "title": f"{name}-{i}",
             "date": "2024", "DOI": f"10.0/{name}.{i}"} for i in range(n)]


class _FakeReporter:
    """Capture les rapports de progression ; pas d'annulation."""
    def __init__(self):
        self.reports = []

    def report(self, progress, total, message):
        self.reports.append((progress, total, message))

    def check_cancelled(self):
        pass


# === Progression ===

class TestProgress:
    def test_one_report_per_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: _items(name))
        reporter = _FakeReporter()
        mcp_server._run_moisson_sync(
            query="q", max_per_source=5, sources=["hal", "isidore"],
            enrich_oai=False, do_dedup=True, workers=1,
            progress=reporter, cancel_event=None,
        )
        # 2 sources → 2 rapports ; total cohérent.
        assert len(reporter.reports) == 2
        assert all(total == 2.0 for _, total, _ in reporter.reports)
        assert reporter.reports[-1][0] == 2.0  # dernier progress = 2/2

    def test_no_progress_without_reporter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: _items(name))
        # progress=None → aucune notification, moisson OK.
        blocks, structured = mcp_server._run_moisson_sync(
            query="q", max_per_source=5, sources=["hal"],
            enrich_oai=False, do_dedup=True, workers=1,
        )
        assert structured["total_unique"] >= 1


# === Annulation coopérative ===

class TestCancellation:
    def test_cancel_between_sources(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        cancel = threading.Event()
        ran, written = [], []

        def stub_run_source(name, cls, query, mps, output_dir, run_id,
                            http_client, **kw):
            # Mimer la vraie écriture de raw/ pour tester le nettoyage.
            ran.append(name)
            raw = Path(output_dir) / "raw"
            raw.mkdir(parents=True, exist_ok=True)
            path = raw / f"{name.lower()}_{run_id}.json"
            path.write_text(json.dumps(_items(name)), encoding="utf-8")
            written.append(path)
            # Annulation demandée pendant que la 1re source se termine
            # (depuis OPT-07, le moteur consulte le signal plus souvent :
            # compter les appels à is_set() déclenchait l'annulation avant
            # la 1re source, et le nettoyage n'était plus vérifié).
            cancel.set()
            return _items(name)

        monkeypatch.setattr("engine.orchestrator.run_source", stub_run_source)

        with pytest.raises(mcp_server.MoissonCancelled):
            mcp_server._run_moisson_sync(
                query="q", max_per_source=5, sources=["hal", "isidore"],
                enrich_oai=False, do_dedup=True, workers=1,
                progress=None, cancel_event=cancel,
            )
        # La 1re source a fini et écrit son raw/ ; la 2e n'a pas démarré.
        assert ran == ["HAL"] and len(written) == 1
        # Aucun corpus FINAL écrit.
        assert list(tmp_path.glob("corpus_*.json")) == []
        # raw/ partiels nettoyés.
        assert not written[0].exists()
        assert list((tmp_path / "raw").glob("*.json")) == []

    def test_cancel_before_any_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: _items(name))

        class _AlwaysSet:
            def is_set(self):
                return True

        with pytest.raises(mcp_server.MoissonCancelled):
            mcp_server._run_moisson_sync(
                query="q", max_per_source=5, sources=["hal"],
                enrich_oai=False, do_dedup=True, workers=1,
                cancel_event=_AlwaysSet(),
            )
        assert list(tmp_path.glob("corpus_*.json")) == []


# === Deadline serveur (HTTP) ===

class TestDeadline:
    def test_deadline_returns_clean_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)  # frais pour ce loop
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 0.05)

        def slow(name, cls, query, mps, output_dir, run_id, http_client, **kw):
            time.sleep(0.3)            # plus long que la deadline
            return _items(name)

        monkeypatch.setattr("engine.orchestrator.run_source", slow)

        result = _run(mcp_server._tool_moisson_search(
            {"query": "x", "sources": ["hal", "isidore"]}
        ))
        # Résultat d'erreur propre (str → TextContent par le dispatcher).
        assert isinstance(result, str)
        assert "deadline" in result.lower() or "annulée" in result.lower()
        # Pas de corpus final publié.
        assert list(tmp_path.glob("corpus_*.json")) == []


# === Concurrence bornée (sémaphore) ===

class TestSemaphore:
    def test_busy_semaphore_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "MOISSON_ACQUIRE_TIMEOUT", 0.1)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: _items(name))

        async def _go():
            sem = asyncio.Semaphore(1)
            await sem.acquire()                 # sémaphore plein
            monkeypatch.setattr(mcp_server, "_MOISSON_SEM", sem)
            return await mcp_server._tool_moisson_search(
                {"query": "x", "sources": ["hal"]}
            )

        result = _run(_go())
        assert isinstance(result, str)
        assert "occupé" in result.lower()

    def test_semaphore_released_after_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: _items(name))

        async def _go():
            r1 = await mcp_server._tool_moisson_search(
                {"query": "a", "sources": ["hal"]})
            r2 = await mcp_server._tool_moisson_search(
                {"query": "b", "sources": ["hal"]})
            return r1, r2

        r1, r2 = _run(_go())
        # Deux moissons successives réussissent (slot relâché entre les deux).
        assert isinstance(r1, tuple) and isinstance(r2, tuple)


# === Plafond serveur (anti-DoS) ===

class TestServerCap:
    def _capture_max(self, monkeypatch):
        captured = {}

        def fake_sync(query, max_per_source, *a, **k):
            captured["max"] = max_per_source
            return ([], {"total_unique": 0})

        monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
        return captured

    def test_clamped_in_http(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
        monkeypatch.setattr(mcp_server, "MOISSON_MAX_ITEMS_PER_SOURCE", 100)
        captured = self._capture_max(monkeypatch)
        _run(mcp_server._tool_moisson_search(
            {"query": "x", "max_per_source": 999999, "sources": ["hal"]}))
        assert captured["max"] == 100

    def test_not_clamped_in_stdio(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "MOISSON_MAX_ITEMS_PER_SOURCE", 100)
        captured = self._capture_max(monkeypatch)
        _run(mcp_server._tool_moisson_search(
            {"query": "x", "max_per_source": 999999, "sources": ["hal"]}))
        assert captured["max"] == 999999
