"""
Sprint « universel », lot 7 : aucune sortie de ``moisson-shs setup`` ne
contient une valeur secrète, ni d'un autre serveur ni des nôtres
(installation, aperçu, état, retrait, extrait, erreur), pour chacun des
sept clients. Leçon de la fuite du 2026-10-08 (l'aperçu imprimait tout
``claude_desktop_config.json``).
"""

import json
import subprocess

import pytest

import setup_claude_desktop as S
from mcp_clients import procs
from mcp_clients.common import API_KEY_ENV_VARS

OTHER = ("sk-autre-secret", "tok-autre")
OURS = "oa-notre-cle-123"
IDS = ["claude-desktop", "claude-code", "codex", "gemini-cli", "antigravity",
       "antigravity-ide", "hermes"]


def _write_config(client_id, home):
    """Configuration du client avec deux autres serveurs porteurs de secrets."""
    if client_id == "claude-code":
        path = home / ".claude.json"
        doc = {"projects": {}, "mcpServers": {
            "autre": {"command": "x", "env": {"API_KEY": OTHER[0]}},
            "web": {"type": "http", "url": "u",
                    "headers": {"Authorization": f"Bearer {OTHER[1]}"}}}}
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path
    if client_id == "codex":
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            '[mcp_servers.autre]\ncommand = "x"\n\n[mcp_servers.autre.env]\n'
            f'API_KEY = "{OTHER[0]}"\n\n[mcp_servers.web]\nurl = "u"\n\n'
            f'[mcp_servers.web.headers]\nAuthorization = "Bearer {OTHER[1]}"\n',
            encoding="utf-8")
        return path
    if client_id == "hermes":
        path = home / ".hermes" / "config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "mcp_servers:\n  autre:\n    command: x\n    env:\n"
            f"      API_KEY: {OTHER[0]}\n  web:\n    url: u\n    headers:\n"
            f"      Authorization: Bearer {OTHER[1]}\n", encoding="utf-8")
        return path
    adapter = {a.id: a for a in S.build_registry()}[client_id]
    path = adapter.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"preferences": {"x": 1}, "mcpServers": {
        "autre": {"command": "x", "env": {"API_KEY": OTHER[0]}},
        "web": {"url": "u", "headers": {"Authorization": f"Bearer {OTHER[1]}"}}}}),
        encoding="utf-8")
    return path


@pytest.fixture(params=IDS)
def client(request, fake_home, monkeypatch, tmp_path):
    for var in API_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(procs, "is_running", lambda names, **k: False)
    calls = []

    def runner(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        # Une CLI qui recopierait ses arguments dans son message d'erreur.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=" ".join(cmd))
    monkeypatch.setattr(S, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(S, "_CLI_RUNNER", runner)
    server = tmp_path / "mcp_server.py"
    server.write_text("# stub", encoding="utf-8")
    _write_config(request.param, fake_home)
    return request.param, server


def _run(client_id, server, *extra, interactive=False):
    printed = []
    args = S._parse_args(["--skip-deps-check", "--no-prompt", "--server-path",
                          str(server), "--python", "/py", "--client", client_id,
                          "--openalex-key", OURS, *extra])
    args.server_path_resolved = server
    rc = S._run(args, interactive=interactive, print_func=printed.append)
    return rc, "\n".join(str(p) for p in printed)


def _clean(text):
    for secret in OTHER + (OURS,):
        assert secret not in text, secret


class TestNoSecretInOutputs:
    def test_install_dry_run_status_uninstall(self, client):
        client_id, server = client
        for extra in (("--dry-run",), (), ("--status",), ("--uninstall",)):
            rc, text = _run(client_id, server, *extra)
            assert rc == 0, text
            _clean(text)

    def test_error_path(self, client, monkeypatch):
        client_id, server = client

        def failing(cmd, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=" ".join(cmd))
        monkeypatch.setattr(S, "_CLI_RUNNER", failing)
        _rc, text = _run(client_id, server)
        _clean(text)

    def test_four_keys_and_backup_after_install(self, client, fake_home):
        client_id, server = client
        rc, _text = _run(client_id, server)
        assert rc == 0
        adapter = {a.id: a for a in S.build_registry()}[client_id]
        if client_id == "claude-code":
            return                      # écrit par la CLI (simulée ici)
        entry = adapter.read_entry()
        assert set(API_KEY_ENV_VARS) <= set(entry["env"])
        assert entry["env"]["OPENALEX_API_KEY"] == OURS
        backups = list(adapter.config_path().parent.glob("*.moisson-bak-*"))
        assert len(backups) == 1
