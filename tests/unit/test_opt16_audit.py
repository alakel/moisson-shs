"""
Tests OPT-16 : audit des réponses natives des fournisseurs (C32).

- désactivé par défaut : aucun enregistreur, aucun dossier ;
- activé (``MOISSON_AUDIT_NATIVE=1``) : chaque réponse obtenue par la
  politique HTTP commune (réseau ou cache) est consignée dans
  ``<sortie>/.native_audit/<run_id>.jsonl`` ;
- expurgée : clés d'API, jetons, adresse mailto masqués dans l'URL et le
  corps ; aucun en-tête de requête ; en-têtes de réponse choisis ;
- bornée : corps tronqué par réponse, capture arrêtée au plafond par
  exécution (ligne de signalement) ;
- dossier de service : jamais listé, lu ni téléchargé ; ``raw/`` garde les
  notices normalisées.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from utils import http as http_mod  # noqa: E402
from utils import manifest as mf  # noqa: E402
from utils.http import HTTPClient, attach_native_audit, policy_get  # noqa: E402
from utils.output_files import is_downloadable_file, is_service_path  # noqa: E402

SECRET = "CLE-SECRETE-123456"


class _Resp:
    def __init__(self, body: bytes, url: str, status=200, headers=None):
        self.status_code = status
        self.content = body
        self.url = url
        self.headers = headers or {"content-type": "application/json",
                                   "authorization": "Bearer " + SECRET,
                                   "set-cookie": "session=abc",
                                   "x-rate-limit-limit": "50"}
        self.from_cache = False
        self.reason = ""

    def json(self):
        return json.loads(self.content)


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        return self.responses.pop(0)


def _get(session, host, params=None):
    return policy_get(session, f"https://{host}/works", params=params,
                      min_interval=0)


class TestActivation:
    def test_disabled_by_default(self, tmp_path):
        assert config.AUDIT_NATIVE is False
        assert mf.native_audit_for_run(tmp_path, "run1") is None
        assert not (tmp_path / ".native_audit").exists()

    def test_moisson_without_audit_creates_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(orchestrator, "run_source",
                            lambda name, *a, **k: [])
        mcp_server._run_moisson_sync("q", 5, ["hal"], False, True, workers=1)
        assert not (tmp_path / ".native_audit").exists()

    def test_enabled_attaches_recorder_to_the_run_session(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.setattr(config, "AUDIT_NATIVE", True)
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        seen = {}

        def runner(name, cls, query, mps, output_dir, run_id, http_client,
                   **kw):
            seen["recorder"] = getattr(http_client.session,
                                       http_mod.NATIVE_AUDIT_ATTR, None)
            return [{"itemType": "book", "title": "t"}]

        monkeypatch.setattr(orchestrator, "run_source", runner)
        mcp_server._run_moisson_sync("q", 5, ["hal"], False, True, workers=1)
        assert isinstance(seen["recorder"], mf.NativeAudit)
        assert seen["recorder"].path.parent == tmp_path / ".native_audit"


class TestCapture:
    def test_response_captured_and_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENALEX_API_KEY", SECRET)
        rec = mf.NativeAudit(tmp_path / ".native_audit", "run1",
                             max_response_bytes=10_000, max_run_bytes=100_000)
        body = json.dumps({"results": [{"id": "W1"}],
                           "meta": {"echo": f"api_key={SECRET}"},
                           "next": f"https://api.x/?mailto=moi@exemple.fr&key={SECRET}"
                           }).encode()
        url = (f"https://audit-a.example/works?search=discours&api_key={SECRET}"
               f"&mailto=moi@exemple.fr")
        session = _Session([_Resp(body, url)])
        attach_native_audit(session, rec)
        _get(session, "audit-a.example",
             params={"search": "discours", "api_key": SECRET,
                     "mailto": "moi@exemple.fr"})
        text = rec.path.read_text(encoding="utf-8")
        assert SECRET not in text
        assert "moi@exemple.fr" not in text
        assert "session=abc" not in text and "Bearer" not in text
        (line,) = text.splitlines()
        record = json.loads(line)
        assert record["status"] == 200
        assert "search=discours" in record["url"]
        assert "api_key=%2A%2A%2A" in record["url"] or "api_key=***" in record["url"]
        assert record["headers"] == {"content-type": "application/json",
                                     "x-rate-limit-limit": "50"}
        assert json.loads(record["body"])["results"] == [{"id": "W1"}]
        assert record["body_bytes"] == len(body) and not record["body_truncated"]
        assert rec.summary()["responses"] == 1

    def test_body_truncated_per_response(self, tmp_path):
        rec = mf.NativeAudit(tmp_path, "run2", max_response_bytes=100,
                             max_run_bytes=100_000)
        body = b'{"x": "' + b"a" * 5000 + b'"}'
        session = _Session([_Resp(body, "https://audit-b.example/works")])
        attach_native_audit(session, rec)
        _get(session, "audit-b.example")
        record = json.loads(rec.path.read_text(encoding="utf-8"))
        assert record["body_truncated"] is True
        assert len(record["body"].encode("utf-8")) <= 100
        assert record["body_bytes"] == len(body)

    def test_run_cap_stops_capture(self, tmp_path):
        rec = mf.NativeAudit(tmp_path, "run3", max_response_bytes=2000,
                             max_run_bytes=3000)
        body = b'{"x": "' + b"b" * 1500 + b'"}'
        session = _Session([_Resp(body, "https://audit-c.example/works")
                            for _ in range(4)])
        attach_native_audit(session, rec)
        for _ in range(4):
            _get(session, "audit-c.example")
        lines = [json.loads(l) for l in
                 rec.path.read_text(encoding="utf-8").splitlines()]
        assert lines[-1].get("capture_stopped") is True
        assert rec.summary()["responses"] == 1
        assert rec.summary()["dropped"] == 3
        assert rec.path.stat().st_size < 3000 + 500

    def test_capture_failure_never_breaks_the_request(self, tmp_path):
        class _Broken:
            def capture(self, *a, **k):
                raise RuntimeError("audit en panne")

        session = _Session([_Resp(b"{}", "https://audit-d.example/works")])
        attach_native_audit(session, _Broken())
        response = _get(session, "audit-d.example")
        assert response.status_code == 200

    def test_client_views_share_the_recorder(self, tmp_path):
        client = HTTPClient(use_cache=False)
        rec = mf.NativeAudit(tmp_path, "run4")
        attach_native_audit(client, rec)
        view = client.with_cancel(None)
        assert getattr(view.session, http_mod.NATIVE_AUDIT_ATTR) is rec
        client.close()

    def test_old_run_files_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "AUDIT_NATIVE", True)
        monkeypatch.setattr(config, "AUDIT_NATIVE_KEEP_RUNS", 2)
        audit_dir = tmp_path / ".native_audit"
        audit_dir.mkdir()
        import os
        import time
        for i in range(4):
            p = audit_dir / f"old{i}.jsonl"
            p.write_text("{}\n", encoding="utf-8")
            os.utime(p, (time.time() - 1000 + i, time.time() - 1000 + i))
        rec = mf.native_audit_for_run(tmp_path, "new")
        assert rec is not None
        assert sorted(p.name for p in audit_dir.glob("*.jsonl")) == ["old3.jsonl"]


class TestServiceDirectory:
    def test_never_listed_read_or_downloaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        audit = tmp_path / ".native_audit"
        audit.mkdir()
        (audit / "run.jsonl").write_text('{"body": "secret"}\n', encoding="utf-8")
        assert is_service_path(".native_audit/run.jsonl")
        assert not is_downloadable_file(".native_audit/run.jsonl")
        text = asyncio.run(mcp_server.read_resource(
            "corpus://.native_audit/run.jsonl"))
        assert "error" in json.loads(text) and "secret" not in text
        assert asyncio.run(mcp_server.list_resources()) == []

    def test_raw_keeps_normalized_notices(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "AUDIT_NATIVE", True)
        from engine.outputs import write_raw
        item = {"itemType": "book", "title": "t"}
        path = write_raw(tmp_path, "HAL", "run5", [item])
        assert json.loads(path.read_text(encoding="utf-8")) == [item]
