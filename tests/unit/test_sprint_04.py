"""
Tests de non-régression pour les ajouts du Sprint 04.

Couvre :
- #7 : mcp activé dans requirements.txt
- #23 : packaging via pyproject.toml (entry_points fonctionnels)
- #24 : repo git initialisé
- #25 : workflow GitHub Actions (présence du fichier)
- #26/#27 : tutoriel README et FAQ (présence et structure)
- bonus : moisson-shs setup automatisé (setup_claude_desktop.py)
"""

import json
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


# === #7 : Dépendance MCP active ===

class TestMcpDependency:
    def test_requirements_includes_mcp(self):
        req = (PROJECT_ROOT / "requirements.txt").read_text()
        # mcp doit être présent ET non commenté
        lines = [l.strip() for l in req.splitlines()]
        active_mcp = [
            l for l in lines
            if l.startswith("mcp") and not l.startswith("#")
        ]
        assert active_mcp, "mcp doit figurer comme dépendance active"


# === #23 : pyproject.toml ===

class TestPyprojectToml:
    def test_pyproject_exists(self):
        pyproject = PROJECT_ROOT / "pyproject.toml"
        assert pyproject.exists()

    def test_pyproject_declares_entry_point(self):
        pyproject = PROJECT_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        assert "moisson-shs = " in content
        assert "cli:main" in content

    def test_pyproject_has_mcp_extra(self):
        pyproject = PROJECT_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        assert "[project.optional-dependencies]" in content
        assert "mcp = " in content
        assert "dev = " in content

    def test_pyproject_declares_required_python(self):
        pyproject = PROJECT_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        assert "requires-python" in content


# === #24 : git ===

class TestGitInit:
    def test_git_directory_present(self):
        assert (PROJECT_ROOT / ".git").is_dir()

    def test_gitignore_present(self):
        assert (PROJECT_ROOT / ".gitignore").exists()

    def test_gitignore_covers_output(self):
        content = (PROJECT_ROOT / ".gitignore").read_text()
        assert "output/" in content or "output/raw/" in content
        assert "__pycache__/" in content
        assert "venv/" in content
        assert ".pytest_cache/" in content


# === #25 : CI ===

class TestGitHubActions:
    def test_workflow_file_present(self):
        wf = PROJECT_ROOT / ".github" / "workflows" / "test.yml"
        assert wf.exists()

    def test_workflow_runs_pytest(self):
        wf = PROJECT_ROOT / ".github" / "workflows" / "test.yml"
        content = wf.read_text()
        assert "pytest" in content
        assert "matrix" in content
        assert "python-version" in content

    def test_workflow_tests_cli(self):
        wf = PROJECT_ROOT / ".github" / "workflows" / "test.yml"
        content = wf.read_text()
        assert "moisson-shs" in content


# === #26/#27 : Documentation ===

class TestDocumentation:
    def test_readme_has_tutorial(self):
        readme = (PROJECT_ROOT / "README.md").read_text()
        # Sections clés du tutoriel
        assert "Démarrage rapide" in readme or "Installation" in readme
        assert "moisson-shs setup" in readme
        assert "CSL-JSON" in readme or "csl.json" in readme
        assert "Zotero Desktop" in readme

    def test_readme_lists_all_sources(self):
        readme = (PROJECT_ROOT / "README.md").read_text()
        for source in ("HAL", "Isidore", "OpenEdition", "Cairn",
                       "Persée", "Theses.fr", "Érudit"):
            assert source in readme, f"Source {source} absente du README"

    def test_readme_mentions_mcp_tools(self):
        readme = (PROJECT_ROOT / "README.md").read_text()
        for tool in ("moisson_search", "search_by_author",
                     "fetch_by_doi", "corpus_merge"):
            assert tool in readme, f"Outil MCP {tool} absent du README"

    def test_faq_exists(self):
        assert (PROJECT_ROOT / "docs" / "FAQ.md").exists()

    def test_faq_covers_key_questions(self):
        faq = (PROJECT_ROOT / "docs" / "FAQ.md").read_text()
        # Questions identifiées dans sprint_04.md
        assert "Cairn" in faq
        assert "HAL" in faq
        assert "doublons" in faq.lower() or "déduplication" in faq.lower()
        assert "MCP" in faq

    def test_license_present(self):
        assert (PROJECT_ROOT / "LICENSE").exists()
        license_content = (PROJECT_ROOT / "LICENSE").read_text()
        assert "MIT" in license_content


# === Bonus : moisson-shs setup ===

class TestSetupClaudeDesktop:
    def test_module_imports(self):
        import setup_claude_desktop
        assert hasattr(setup_claude_desktop, "main")
        assert hasattr(setup_claude_desktop, "install")
        assert hasattr(setup_claude_desktop, "uninstall")
        assert hasattr(setup_claude_desktop, "status")

    def test_detect_config_path_per_os(self):
        from setup_claude_desktop import get_claude_config_path
        path = get_claude_config_path()
        assert isinstance(path, Path)
        # Chemin doit contenir Claude dans tous les cas
        assert "Claude" in str(path)

    def test_python_executable_is_absolute(self):
        from setup_claude_desktop import get_python_executable
        exe = get_python_executable()
        assert os.path.isabs(exe)
        assert os.path.exists(exe)

    def test_python_executable_preserves_venv_symlink(self):
        """
        Régression : sys.executable doit être préservé tel quel (pas
        .resolve()) pour conserver le contexte venv. Si un venv est actif,
        le path doit contenir 'venv' ou être identique à sys.executable.
        """
        from setup_claude_desktop import get_python_executable
        exe = get_python_executable()
        # Le chemin doit correspondre à sys.executable (pas au binaire résolu)
        # Sans utiliser .resolve() qui casserait le venv
        assert exe == str(Path(sys.executable).absolute())

    def test_mcp_server_path_resolves(self):
        from setup_claude_desktop import get_mcp_server_path
        path = get_mcp_server_path()
        assert path.exists()
        assert path.name == "mcp_server.py"

    def test_build_server_entry_minimal(self):
        from setup_claude_desktop import build_server_entry
        entry = build_server_entry("/usr/bin/python", Path("/x/mcp_server.py"))
        assert entry["command"] == "/usr/bin/python"
        assert entry["args"] == ["/x/mcp_server.py"]
        assert "env" not in entry  # vide → pas de clé env

    def test_build_server_entry_with_output_dir(self, tmp_path):
        from setup_claude_desktop import build_server_entry
        out_dir = tmp_path / "corpus"
        out_dir.mkdir()
        entry = build_server_entry(
            "/usr/bin/python",
            Path("/x/mcp_server.py"),
            output_dir=out_dir,
            cache_enabled=True,
        )
        assert entry["env"]["MOISSON_OUTPUT_DIR"] == str(out_dir.resolve())
        assert entry["env"]["MOISSON_CACHE"] == "1"

    def test_install_preserves_other_servers(self, tmp_path):
        """L'installation ne doit pas écraser les autres serveurs MCP."""
        from setup_claude_desktop import install
        config_path = tmp_path / "claude.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "other-server": {"command": "node", "args": ["x.js"]},
            }
        }))
        install(
            config_path=config_path,
            python_exe="/usr/bin/python",
            server_path=Path("/x/mcp_server.py"),
            output_dir=None,
            cache_enabled=False,
        )
        config = json.loads(config_path.read_text())
        assert "other-server" in config["mcpServers"]
        assert "moisson-shs" in config["mcpServers"]

    def test_install_creates_backup(self, tmp_path):
        from setup_claude_desktop import install
        config_path = tmp_path / "claude.json"
        config_path.write_text(json.dumps({"mcpServers": {}}))
        install(
            config_path=config_path,
            python_exe="/usr/bin/python",
            server_path=Path("/x/mcp_server.py"),
            output_dir=None,
            cache_enabled=False,
        )
        backup = config_path.with_suffix(".json.bak")
        assert backup.exists()

    def test_dry_run_does_not_modify(self, tmp_path):
        from setup_claude_desktop import install
        config_path = tmp_path / "claude.json"
        original = json.dumps({"mcpServers": {"x": {"command": "y"}}})
        config_path.write_text(original)
        result = install(
            config_path=config_path,
            python_exe="/usr/bin/python",
            server_path=Path("/x/mcp_server.py"),
            output_dir=None,
            cache_enabled=False,
            dry_run=True,
        )
        assert "DRY RUN" in result
        assert config_path.read_text() == original

    def test_uninstall_preserves_other_servers(self, tmp_path):
        from setup_claude_desktop import uninstall
        config_path = tmp_path / "claude.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "other-server": {"command": "node"},
                "moisson-shs": {"command": "python"},
            }
        }))
        result = uninstall(config_path)
        assert "supprimée" in result.lower()
        config = json.loads(config_path.read_text())
        assert "other-server" in config["mcpServers"]
        assert "moisson-shs" not in config["mcpServers"]

    def test_status_reports_installed(self, tmp_path):
        from setup_claude_desktop import status
        config_path = tmp_path / "claude.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "moisson-shs": {
                    "command": "/usr/bin/python",
                    "args": ["/x/mcp_server.py"],
                },
            }
        }))
        result = status(config_path)
        assert "installé" in result.lower()

    def test_status_reports_missing(self, tmp_path):
        from setup_claude_desktop import status
        config_path = tmp_path / "claude.json"
        # Pas de fichier
        result = status(config_path)
        assert "Aucune" in result or "Aucun" in result

    def test_load_corrupt_config_raises(self, tmp_path):
        from setup_claude_desktop import load_existing_config
        config_path = tmp_path / "claude.json"
        config_path.write_text("not json{{{")
        with pytest.raises(ValueError):
            load_existing_config(config_path)


# === cli.py dispatch ===

class TestCliDispatch:
    def test_cli_imports(self):
        import cli
        assert hasattr(cli, "main")

    def test_cli_version(self, capsys):
        import cli
        rc = cli.main(["version"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "moisson-shs" in captured.out

    def test_cli_help_shown_without_args(self, capsys):
        import cli
        rc = cli.main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "setup" in captured.out
        assert "serve" in captured.out
        assert "search" in captured.out

    def test_cli_help_flag(self, capsys):
        import cli
        rc = cli.main(["--help"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Usage" in captured.out
