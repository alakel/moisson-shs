"""
Sprint « universel », lots 4 et 8 : déroulé de ``moisson-shs setup``
(menu, ``--client``, ``all``, état, retrait, clés saisies une fois et mises
en commun, application ouverte, code de retour). Dossier personnel factice :
jamais les vraies configurations ni les vraies CLI.
"""

import json
import subprocess

import pytest

import setup_claude_desktop as S
from mcp_clients import procs
from mcp_clients.common import API_KEY_ENV_VARS, SERVER_KEY

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (matrice CI)
    import tomli as tomllib

OA, S2, CORE, SERP = API_KEY_ENV_VARS


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "mcp_server.py"
    path.write_text("# stub", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated(fake_home, monkeypatch):
    for var in API_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(S, "find_claude_cli", lambda: None)
    monkeypatch.setattr(S, "_CLI_RUNNER", None)
    monkeypatch.setattr(procs, "is_running", lambda names, **k: False)


def _args(server, *extra):
    args = S._parse_args(["--skip-deps-check", "--server-path", str(server),
                          "--python", "/py", *extra])
    args.server_path_resolved = server
    return args


def _answers(*values):
    values = list(values)

    def fake(prompt=""):
        if not values:
            raise EOFError
        return values.pop(0)
    return fake


def _gemini(home, servers=None):
    path = home / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers or {}}), encoding="utf-8")
    return path


def _desktop(home, servers=None):
    path = S.get_claude_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if servers is not None:
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def _ours(path):
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"].get(SERVER_KEY)


class TestSelection:
    def test_menu_preselects_detected_clients(self, server, fake_home):
        gemini, desktop = _gemini(fake_home), _desktop(fake_home)
        printed = []
        rc = S._run(_args(server, "--no-prompt"), interactive=True,
                    input_func=_answers("", "o"), print_func=printed.append)
        assert rc == 0
        assert _ours(gemini) and _ours(desktop)
        menu_text = "\n".join(p for p in printed if "Clients MCP" in p)
        assert "[x]" in menu_text and "non détecté" in menu_text

    def test_menu_quit_writes_nothing(self, server, fake_home):
        gemini = _gemini(fake_home)
        rc = S._run(_args(server, "--no-prompt"), interactive=True,
                    input_func=_answers("q"), print_func=lambda *a: None)
        assert rc == 0 and _ours(gemini) is None

    def test_named_clients(self, server, fake_home):
        gemini = _gemini(fake_home)
        rc = S._run(_args(server, "--no-prompt", "--client", "codex,gemini"),
                    interactive=False, print_func=lambda *a: None)
        assert rc == 0 and _ours(gemini)
        codex = tomllib.loads((fake_home / ".codex" / "config.toml").read_text())
        assert SERVER_KEY in codex["mcp_servers"]

    def test_unknown_client_is_an_argparse_error(self, server):
        with pytest.raises(SystemExit) as err:
            S._parse_args(["--client", "cursor"])
        assert err.value.code == 2

    def test_client_config_override(self, server, fake_home, tmp_path):
        target = tmp_path / "ailleurs" / "config.yaml"
        rc = S._run(_args(server, "--no-prompt", "--client", "hermes",
                          "--client-config", f"hermes={target}"),
                    interactive=False, print_func=lambda *a: None)
        assert rc == 0 and target.is_file()
        assert not (fake_home / ".hermes").exists()


class TestStatusAndUninstall:
    def test_status_covers_every_client(self, server, fake_home):
        gemini = _gemini(fake_home, {SERVER_KEY: {"command": "/py", "args": [],
                                                  "env": {OA: "secret-oa"}}})
        before = gemini.read_text()
        printed = []
        assert S._run(_args(server, "--status"), interactive=False,
                      print_func=printed.append) == 0
        text = "\n".join(printed)
        for label in ("Claude Desktop", "Claude Code", "Codex", "Gemini CLI",
                      "Antigravity CLI", "Antigravity IDE", "Hermes"):
            assert label in text
        assert "secret-oa" not in text and gemini.read_text() == before

    def test_uninstall_menu_lists_declared_clients(self, server, fake_home):
        gemini = _gemini(fake_home, {SERVER_KEY: {"command": "/p"}, "autre": {}})
        desktop = _desktop(fake_home, {"x": {"command": "y"}})
        printed = []
        # Rien n'est coché d'avance pour un retrait : cocher 1, valider.
        rc = S._run(_args(server, "--uninstall"), interactive=True,
                    input_func=_answers("1", "", "o"), print_func=printed.append)
        assert rc == 0 and _ours(gemini) is None
        assert json.loads(gemini.read_text())["mcpServers"] == {"autre": {}}
        menu_text = "\n".join(p for p in printed if "Clients MCP" in p)
        assert "Gemini CLI" in menu_text and "Claude Desktop" not in menu_text
        assert json.loads(desktop.read_text()) == {"mcpServers": {"x": {"command": "y"}}}


class TestKeys:
    def test_keys_asked_once_for_all_clients(self, server, fake_home):
        gemini, desktop = _gemini(fake_home), _desktop(fake_home)
        asked = []

        def secret(prompt):
            asked.append(prompt)
            return "k-" + prompt.split()[0].lower()
        rc = S._run(_args(server, "--client", "desktop,gemini"), interactive=True,
                    print_func=lambda *a: None, secret_input=secret)
        assert rc == 0 and len(asked) == 4
        for path in (gemini, desktop):
            env = _ours(path)["env"]
            assert env[OA] == "k-openalex_api_key"

    def test_common_key_completes_other_client(self, server, fake_home):
        _desktop(fake_home, {SERVER_KEY: {"command": "/p", "env": {OA: "oa-commune"}}})
        printed = []
        S._run(_args(server, "--no-prompt", "--client", "codex"), interactive=False,
               print_func=printed.append)
        codex = tomllib.loads((fake_home / ".codex" / "config.toml").read_text())
        assert codex["mcp_servers"][SERVER_KEY]["env"][OA] == "oa-commune"
        assert "oa-commune" not in "\n".join(printed)

    def test_conflicting_keys_are_not_pooled(self, server, fake_home):
        _desktop(fake_home, {SERVER_KEY: {"command": "/p", "env": {OA: "a-1"}}})
        _gemini(fake_home, {SERVER_KEY: {"command": "/p", "env": {OA: "b-2"}}})
        printed = []
        S._run(_args(server, "--no-prompt", "--client", "codex"), interactive=False,
               print_func=printed.append)
        codex = tomllib.loads((fake_home / ".codex" / "config.toml").read_text())
        assert codex["mcp_servers"][SERVER_KEY]["env"][OA] == ""
        text = "\n".join(printed)
        assert "OPENALEX_API_KEY diffère" in text
        assert "a-1" not in text and "b-2" not in text

    def test_unmanaged_env_variables_are_kept(self, server, fake_home):
        gemini = _gemini(fake_home, {SERVER_KEY: {
            "command": "/vieux", "env": {"MOISSON_KEY_FALLBACK": "openalex",
                                         "MOISSON_OUTPUT_DIR": "/corpus"}}})
        S._run(_args(server, "--no-prompt", "--client", "gemini"), interactive=False,
               print_func=lambda *a: None)
        env = _ours(gemini)["env"]
        assert env["MOISSON_KEY_FALLBACK"] == "openalex"
        assert env["MOISSON_OUTPUT_DIR"] == "/corpus"


class TestRunningApplication:
    def test_skipped_outside_terminal(self, server, fake_home, monkeypatch):
        monkeypatch.setattr(procs, "is_running", lambda names, **k: True)
        desktop = _desktop(fake_home, {})
        printed = []
        rc = S._run(_args(server, "--no-prompt", "--client", "desktop"),
                    interactive=False, print_func=printed.append)
        assert rc == 1 and _ours(desktop) is None
        assert "client sauté" in "\n".join(printed)

    def test_force_writes(self, server, fake_home, monkeypatch):
        monkeypatch.setattr(procs, "is_running", lambda names, **k: True)
        desktop = _desktop(fake_home, {})
        rc = S._run(_args(server, "--no-prompt", "--client", "desktop", "--force"),
                    interactive=False, print_func=lambda *a: None)
        assert rc == 0 and _ours(desktop)

    @pytest.mark.parametrize("answer,written", [("c", True), ("s", False)])
    def test_terminal_asks(self, server, fake_home, monkeypatch, answer, written):
        monkeypatch.setattr(procs, "is_running", lambda names, **k: True)
        desktop = _desktop(fake_home, {})
        S._run(_args(server, "--no-prompt", "--client", "desktop"), interactive=True,
               input_func=_answers(answer), print_func=lambda *a: None)
        assert bool(_ours(desktop)) is written

    def test_closed_after_prompt(self, server, fake_home, monkeypatch):
        states = iter([True, False])
        monkeypatch.setattr(procs, "is_running", lambda names, **k: next(states))
        desktop = _desktop(fake_home, {})
        S._run(_args(server, "--no-prompt", "--client", "desktop"), interactive=True,
               input_func=_answers(""), print_func=lambda *a: None)
        assert _ours(desktop)

    def test_unknown_state_only_warns(self, server, fake_home, monkeypatch):
        monkeypatch.setattr(procs, "is_running", lambda names, **k: None)
        desktop = _desktop(fake_home, {})
        printed = []
        rc = S._run(_args(server, "--no-prompt", "--client", "desktop"),
                    interactive=False, print_func=printed.append)
        assert rc == 0 and _ours(desktop)
        assert "impossible de vérifier" in "\n".join(printed)

    def test_dry_run_does_not_check(self, server, fake_home, monkeypatch):
        monkeypatch.setattr(procs, "is_running",
                            lambda names, **k: pytest.fail("pas de contrôle"))
        _desktop(fake_home, {})
        assert S._run(_args(server, "--no-prompt", "--client", "desktop",
                            "--dry-run"), interactive=False,
                      print_func=lambda *a: None) == 0


class TestOutcome:
    def test_error_gives_exit_code_1(self, server, fake_home):
        # Claude Code sans CLI : erreur, les autres clients sont traités.
        gemini = _gemini(fake_home)
        printed = []
        rc = S._run(_args(server, "--no-prompt", "--client", "code,gemini"),
                    interactive=False, print_func=printed.append)
        assert rc == 1 and _ours(gemini)
        text = "\n".join(printed)
        assert "Récapitulatif" in text and "erreur" in text

    def test_restart_hints_for_written_clients(self, server, fake_home):
        _gemini(fake_home)
        printed = []
        S._run(_args(server, "--no-prompt", "--client", "gemini,hermes"),
               interactive=False, print_func=printed.append)
        text = "\n".join(printed)
        assert "gemini trust" in text and "/reload-mcp" in text

    def test_claude_code_with_fake_cli(self, server, fake_home, monkeypatch):
        calls = []

        def runner(cmd, capture_output=True, text=True, timeout=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        monkeypatch.setattr(S, "find_claude_cli", lambda: "claude")
        monkeypatch.setattr(S, "_CLI_RUNNER", runner)
        rc = S._run(_args(server, "--no-prompt", "--client", "code"),
                    interactive=False, print_func=lambda *a: None)
        assert rc == 0 and calls[-1][:3] == ["claude", "mcp", "add-json"]
