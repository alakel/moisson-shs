"""
Sprint « clés » (CLE-04, CLE-05, CLE-06) : la déclaration du client porte
toujours les quatre clés.

- Claude Desktop : ``claude_desktop_config.json``, comme avant, mais les
  quatre variables sont toujours écrites (vides si non fournies), les valeurs
  existantes conservées.
- Claude Code : portée utilisateur, par ``claude mcp add-json --scope user``
  (jamais ``.mcp.json``, versionné) ; retrait puis ajout, entrée précédente
  restaurée si l'ajout échoue.
- ``--status`` : état de chaque clé dans la déclaration, réserve du `.env`,
  repli permis ; jamais de valeur affichée.
"""

import json

import pytest

import setup_claude_desktop as S
from utils import secrets


OA = "OPENALEX_API_KEY"
S2 = "SEMANTIC_SCHOLAR_API_KEY"
CORE = "CORE_API_KEY"
SERP = "SERPAPI_API_KEY"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for var in S.API_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(secrets.KEY_FALLBACK_ENV, raising=False)
    monkeypatch.setattr(secrets, "_RESERVE", {})


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "mcp_server.py"
    path.write_text("# stub", encoding="utf-8")
    return path


# === Claude Desktop (CLE-04) ===

class TestDesktopAlwaysWritesFourKeys:
    def test_missing_keys_are_written_empty(self, tmp_path, server):
        cfg = tmp_path / "claude_desktop_config.json"
        S.install(cfg, "/py", server, None, False, extra_env={OA: "OA1"})
        env = json.loads(cfg.read_text())["mcpServers"]["moisson-shs"]["env"]
        assert env == {OA: "OA1", S2: "", CORE: "", SERP: ""}

    def test_no_key_at_all(self, tmp_path, server):
        cfg = tmp_path / "claude_desktop_config.json"
        S.install(cfg, "/py", server, None, False)
        env = json.loads(cfg.read_text())["mcpServers"]["moisson-shs"]["env"]
        assert env == {OA: "", S2: "", CORE: "", SERP: ""}

    def test_reinstall_keeps_values_and_fills_new_ones(self, tmp_path, server):
        cfg = tmp_path / "claude_desktop_config.json"
        S.install(cfg, "/py", server, None, False, extra_env={OA: "OA1"})
        S.install(cfg, "/py", server, None, False, extra_env={CORE: "CO1"})
        env = json.loads(cfg.read_text())["mcpServers"]["moisson-shs"]["env"]
        assert env == {OA: "OA1", S2: "", CORE: "CO1", SERP: ""}

    def test_new_value_replaces_the_old_one(self, tmp_path, server):
        cfg = tmp_path / "claude_desktop_config.json"
        S.install(cfg, "/py", server, None, False, extra_env={OA: "OA1"})
        S.install(cfg, "/py", server, None, False, extra_env={OA: "OA2"})
        env = json.loads(cfg.read_text())["mcpServers"]["moisson-shs"]["env"]
        assert env[OA] == "OA2"

    def test_output_marks_empty_keys_and_masks_values(self, tmp_path, server):
        cfg = tmp_path / "claude_desktop_config.json"
        out = S.install(cfg, "/py", server, None, False,
                        extra_env={OA: "sk-secret-123"})
        assert "sk-secret-123" not in out
        assert "(vide)" in out
        assert "Clés vides" in out and CORE in out and SERP in out

    def test_dry_run_never_shows_other_servers_secrets(self, tmp_path, server):
        # Constat du 2026-10-08 : l'aperçu imprimait tout le fichier, clés
        # des autres serveurs MCP comprises (en clair).
        cfg = tmp_path / "claude_desktop_config.json"
        cfg.write_text(json.dumps({"mcpServers": {"autre": {
            "command": "x", "env": {"OTHER_API_KEY": "sk-autre-secret"}}}}))
        out = S.install(cfg, "/py", server, None, False, dry_run=True)
        assert "sk-autre-secret" not in out
        assert "autre" in out          # nommé comme serveur préservé

    def test_redact_env(self):
        red = S._redact_env({"env": {OA: "abcdef", S2: "", "MOISSON_CACHE": "1"}})
        assert red["env"][OA].startswith("*** masqué")
        assert red["env"][S2] == "(vide)"
        assert red["env"]["MOISSON_CACHE"] == "1"


class TestNonInteractiveNotice:
    def test_notice_names_keys_left_empty(self):
        text = S.missing_keys_notice({OA: "x", S2: "", CORE: "", SERP: ""},
                                     "/chemin/config.json")
        assert S2 in text and CORE in text and SERP in text
        assert OA not in text
        assert "/chemin/config.json" in text
        assert "autres sources" in text

    def test_no_notice_when_every_key_is_set(self):
        assert S.missing_keys_notice({v: "x" for v in S.API_KEY_ENV_VARS},
                                     "/c") == ""


# === État des clés (CLE-06) ===

class TestKeyReport:
    def test_states_reserve_and_policy(self, monkeypatch):
        monkeypatch.setattr(secrets, "_RESERVE", {OA: "R1", SERP: "R2"})
        lines = "\n".join(S.key_report({OA: "x", S2: ""}))
        rows = {line.split()[0]: line for line in lines.splitlines()
                if line.strip().split()[0] in S.API_KEY_ENV_VARS}
        assert "renseignée" in rows[OA] and "présente" in rows[OA]
        assert "vide" in rows[S2] and "absente" in rows[S2]
        assert "absente" in rows[CORE].split("·")[0]      # non déclarée
        assert "non" in rows[SERP].split("·")[2]          # repli non permis
        assert "oui" in rows[OA].split("·")[2]
        assert "R1" not in lines and "R2" not in lines

    def test_policy_declared_by_the_client(self, monkeypatch):
        lines = "\n".join(S.key_report({secrets.KEY_FALLBACK_ENV: "google_scholar"}))
        rows = {line.split()[0]: line for line in lines.splitlines()
                if line.strip().split()[0] in S.API_KEY_ENV_VARS}
        assert "oui" in rows[SERP].split("·")[2]
        assert "non" in rows[OA].split("·")[2]

    def test_desktop_status_includes_the_report(self, tmp_path, server):
        cfg = tmp_path / "claude_desktop_config.json"
        S.install(cfg, "/py", server, None, False, extra_env={OA: "secret-oa"})
        out = S.status(cfg)
        assert "secret-oa" not in out
        assert "réserve" in out.lower() and CORE in out


# === Claude Code (CLE-05) ===

class _Runner:
    """Faux ``subprocess.run`` : commandes notées, codes de retour choisis."""

    def __init__(self, codes=None):
        self.codes = list(codes or [])
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(list(cmd))
        code = self.codes.pop(0) if self.codes else 0

        class _Done:
            returncode = code
            stdout = ""
            stderr = "erreur simulée" if code else ""
        return _Done()


def _code_config(tmp_path, entry=None):
    path = tmp_path / ".claude.json"
    data = {"mcpServers": {"autre": {"command": "x"}}, "projects": {}}
    if entry is not None:
        data["mcpServers"]["moisson-shs"] = entry
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _added_entry(call):
    assert call[:4] == ["claude", "mcp", "add-json", "moisson-shs"]
    assert call[-2:] == ["--scope", "user"]
    return json.loads(call[4])


class TestClaudeCodeInstall:
    def test_add_with_four_keys(self, tmp_path, server):
        runner = _Runner()
        out = S.install_claude_code(
            "/py", server, None, False, extra_env={OA: "OA1"},
            config_path=_code_config(tmp_path), runner=runner,
            claude_bin="claude")
        assert len(runner.calls) == 1
        entry = _added_entry(runner.calls[0])
        assert entry["type"] == "stdio"
        assert entry["command"] == "/py" and entry["args"] == [str(server)]
        assert entry["env"] == {OA: "OA1", S2: "", CORE: "", SERP: ""}
        assert "OA1" not in out

    def test_existing_entry_is_replaced_and_keys_kept(self, tmp_path, server):
        previous = {"type": "stdio", "command": "/old", "args": ["/old.py"],
                    "env": {OA: "OLD-OA", CORE: "OLD-CO"}}
        runner = _Runner()
        S.install_claude_code("/py", server, None, False, extra_env={},
                              config_path=_code_config(tmp_path, previous),
                              runner=runner, claude_bin="claude")
        assert runner.calls[0] == ["claude", "mcp", "remove", "moisson-shs",
                                   "--scope", "user"]
        entry = _added_entry(runner.calls[1])
        assert entry["env"] == {OA: "OLD-OA", S2: "", CORE: "OLD-CO", SERP: ""}
        assert entry["command"] == "/py"

    def test_failed_add_restores_the_previous_entry(self, tmp_path, server):
        previous = {"type": "stdio", "command": "/old", "args": ["/old.py"],
                    "env": {OA: "OLD-OA"}}
        runner = _Runner(codes=[0, 1, 0])
        out = S.install_claude_code("/py", server, None, False,
                                    extra_env={CORE: "NEW-CO"},
                                    config_path=_code_config(tmp_path, previous),
                                    runner=runner, claude_bin="claude")
        assert len(runner.calls) == 3
        assert _added_entry(runner.calls[2]) == previous
        assert "échec" in out.lower()
        assert "NEW-CO" not in out and "OLD-OA" not in out

    def test_dry_run_runs_nothing(self, tmp_path, server):
        runner = _Runner()
        out = S.install_claude_code("/py", server, None, False,
                                    extra_env={OA: "OA-SECRET"}, dry_run=True,
                                    config_path=_code_config(tmp_path),
                                    runner=runner, claude_bin="claude")
        assert runner.calls == []
        assert "OA-SECRET" not in out and "DRY RUN" in out

    def test_missing_cli_is_reported(self, tmp_path, server, monkeypatch):
        monkeypatch.setattr(S, "find_claude_cli", lambda: None)
        runner = _Runner()
        out = S.install_claude_code("/py", server, None, False,
                                    config_path=_code_config(tmp_path),
                                    runner=runner)
        assert runner.calls == [] and "claude" in out and "introuvable" in out

    def test_output_dir_and_cache_go_to_env(self, tmp_path, server):
        runner = _Runner()
        S.install_claude_code("/py", server, tmp_path, True,
                              config_path=_code_config(tmp_path),
                              runner=runner, claude_bin="claude")
        env = _added_entry(runner.calls[0])["env"]
        assert env["MOISSON_OUTPUT_DIR"] == str(tmp_path.resolve())
        assert env["MOISSON_CACHE"] == "1"


class TestClaudeCodeStatusAndUninstall:
    def test_status_masks_and_reports(self, tmp_path):
        path = _code_config(tmp_path, {"type": "stdio", "command": "/py",
                                       "args": ["/s.py"],
                                       "env": {OA: "SECRET-OA", CORE: ""}})
        out = S.status_claude_code(path)
        assert "SECRET-OA" not in out
        assert "/py" in out and CORE in out and "(vide)" in out

    def test_status_without_entry(self, tmp_path):
        out = S.status_claude_code(_code_config(tmp_path))
        assert "aucune entrée" in out.lower()

    def test_uninstall(self, tmp_path):
        runner = _Runner()
        path = _code_config(tmp_path, {"command": "/py", "args": []})
        S.uninstall_claude_code(path, runner=runner, claude_bin="claude")
        assert runner.calls == [["claude", "mcp", "remove", "moisson-shs",
                                 "--scope", "user"]]

    def test_uninstall_without_entry(self, tmp_path):
        runner = _Runner()
        out = S.uninstall_claude_code(_code_config(tmp_path), runner=runner,
                                      claude_bin="claude")
        assert runner.calls == [] and "aucune entrée" in out.lower()


class TestMainClientOption:
    """Sprint « universel » : main() passe par les adaptateurs (et non plus
    par install/install_claude_code). Dossier personnel factice, CLI claude
    simulée : jamais les vraies configurations ni la vraie CLI."""

    @pytest.fixture(autouse=True)
    def _no_real_client(self, fake_home, monkeypatch):
        monkeypatch.setattr(S, "find_claude_cli", lambda: None)
        monkeypatch.setattr(S, "_CLI_RUNNER", None)

    def _desktop(self, home):
        return S.get_claude_config_path()

    def test_client_code_routes_to_claude_code(self, server, monkeypatch, fake_home):
        runner = _Runner()
        monkeypatch.setattr(S, "find_claude_cli", lambda: "claude")
        monkeypatch.setattr(S, "_CLI_RUNNER", runner)
        rc = S.main(["--client", "code", "--no-prompt", "--skip-deps-check",
                     "--server-path", str(server), "--python", "/py"])
        assert rc == 0
        assert _added_entry(runner.calls[-1])["command"] == "/py"
        assert not self._desktop(fake_home).exists()

    def test_client_all_installs_detected_clients(self, server, fake_home):
        desktop = self._desktop(fake_home)
        desktop.parent.mkdir(parents=True)
        gemini = fake_home / ".gemini" / "settings.json"
        gemini.parent.mkdir(parents=True)
        gemini.write_text('{"mcpServers": {}}', encoding="utf-8")
        rc = S.main(["--client", "all", "--no-prompt", "--skip-deps-check",
                     "--server-path", str(server), "--python", "/py"])
        assert rc == 0
        for path in (desktop, gemini):
            assert "moisson-shs" in json.loads(path.read_text())["mcpServers"]
        # Ni Codex ni Hermes, non détectés.
        assert not (fake_home / ".codex").exists()
        assert not (fake_home / ".hermes").exists()

    def test_default_client_is_desktop(self, server, fake_home):
        gemini = fake_home / ".gemini" / "settings.json"
        gemini.parent.mkdir(parents=True)
        gemini.write_text('{"mcpServers": {}}', encoding="utf-8")
        S.main(["--no-prompt", "--skip-deps-check", "--server-path",
                str(server), "--python", "/py"])
        assert "moisson-shs" in json.loads(
            self._desktop(fake_home).read_text())["mcpServers"]
        assert json.loads(gemini.read_text()) == {"mcpServers": {}}
