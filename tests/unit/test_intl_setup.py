"""
Tests de l'injection des clés API dans le bloc env de claude_desktop_config
par setup_claude_desktop.
"""

import json

import pytest

import setup_claude_desktop as S


class _Args:
    def __init__(self, openalex=None, s2=None, core=None):
        self.openalex_key = openalex
        self.semantic_scholar_key = s2
        self.core_key = core


class TestCollectApiKeyEnv:
    def test_from_flags(self, monkeypatch):
        for v in S.API_KEY_ENV_VARS:
            monkeypatch.delenv(v, raising=False)
        env = S.collect_api_key_env(_Args(openalex="OA", core="CO"))
        assert env == {"OPENALEX_API_KEY": "OA", "CORE_API_KEY": "CO"}

    def test_from_environment(self, monkeypatch):
        monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "S2KEY")
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        monkeypatch.delenv("CORE_API_KEY", raising=False)
        env = S.collect_api_key_env(_Args())
        assert env == {"SEMANTIC_SCHOLAR_API_KEY": "S2KEY"}

    def test_flag_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_API_KEY", "FROM_ENV")
        env = S.collect_api_key_env(_Args(openalex="FROM_FLAG"))
        assert env["OPENALEX_API_KEY"] == "FROM_FLAG"

    def test_empty_not_injected(self, monkeypatch):
        for v in S.API_KEY_ENV_VARS:
            monkeypatch.delenv(v, raising=False)
        assert S.collect_api_key_env(_Args()) == {}


class TestPromptMissingApiKeys:
    def test_non_interactive_returns_empty(self):
        # Hors terminal interactif (ex. CI) : aucun prompt, jamais bloquant
        assert S.prompt_missing_api_keys({}, interactive=False) == {}

    def test_no_prompt_flag_returns_empty(self):
        assert S.prompt_missing_api_keys(
            {}, no_prompt=True, interactive=True
        ) == {}

    def test_prompts_only_missing_keys(self):
        # OpenAlex déjà fourni → on ne le redemande pas ; S2 et CORE saisis
        asked = []

        def fake(prompt):
            asked.append(prompt)
            return "S2VAL" if "SEMANTIC" in prompt else "COREVAL"

        out = S.prompt_missing_api_keys(
            {"OPENALEX_API_KEY": "OA"}, interactive=True, prompt_func=fake,
        )
        assert out == {"SEMANTIC_SCHOLAR_API_KEY": "S2VAL",
                       "CORE_API_KEY": "COREVAL"}
        # OpenAlex jamais redemandé
        assert not any("OPENALEX" in p for p in asked)

    def test_blank_input_skips_key(self):
        # Entrée vide = passer (clé non collectée → install conserve l'existante)
        def fake(prompt):
            return "   " if "CORE" in prompt else "VALUE"

        out = S.prompt_missing_api_keys(
            {}, interactive=True, prompt_func=fake,
        )
        assert "CORE_API_KEY" not in out
        assert out["OPENALEX_API_KEY"] == "VALUE"

    def test_all_present_no_prompt(self):
        existing = {v: "x" for v in S.API_KEY_ENV_VARS}

        def fake(prompt):  # ne doit jamais être appelé
            raise AssertionError("should not prompt when nothing is missing")

        assert S.prompt_missing_api_keys(
            existing, interactive=True, prompt_func=fake,
        ) == {}


class TestBuildServerEntryEnv:
    def test_extra_env_merged(self, tmp_path):
        entry = S.build_server_entry(
            "/py", tmp_path / "mcp_server.py",
            output_dir=tmp_path,
            extra_env={"OPENALEX_API_KEY": "OA"},
        )
        assert entry["env"]["OPENALEX_API_KEY"] == "OA"
        assert "MOISSON_OUTPUT_DIR" in entry["env"]


class TestInstallPreservesKeys:
    def test_existing_keys_preserved_on_reinstall(self, tmp_path):
        config_path = tmp_path / "claude_desktop_config.json"
        server_path = tmp_path / "mcp_server.py"
        server_path.write_text("# stub")

        # Première install avec une clé
        S.install(config_path, "/py", server_path, tmp_path, False,
                  extra_env={"OPENALEX_API_KEY": "OA1"})
        cfg = json.loads(config_path.read_text())
        assert cfg["mcpServers"]["moisson-shs"]["env"]["OPENALEX_API_KEY"] == "OA1"

        # Réinstall SANS re-fournir la clé : elle doit être préservée
        S.install(config_path, "/py", server_path, tmp_path, False,
                  extra_env={})
        cfg2 = json.loads(config_path.read_text())
        assert cfg2["mcpServers"]["moisson-shs"]["env"]["OPENALEX_API_KEY"] == "OA1"

    def test_install_output_masks_key_values(self, tmp_path):
        # Régression sécurité : la sortie console ne doit JAMAIS contenir la
        # valeur littérale d'une clé API (le fichier sur disque la conserve).
        config_path = tmp_path / "claude_desktop_config.json"
        server_path = tmp_path / "mcp_server.py"
        server_path.write_text("# stub")
        secret = "sk-supersecret-value-123456"
        out = S.install(config_path, "/py", server_path, tmp_path, False,
                        extra_env={"OPENALEX_API_KEY": secret})
        assert secret not in out          # masqué à l'affichage
        assert "masqué" in out
        # …mais présent en clair sur le disque
        assert secret in config_path.read_text()

    def test_dry_run_masks_key_values(self, tmp_path):
        config_path = tmp_path / "claude_desktop_config.json"
        server_path = tmp_path / "mcp_server.py"
        server_path.write_text("# stub")
        secret = "core-key-abcdef"
        out = S.install(config_path, "/py", server_path, tmp_path,
                        cache_enabled=False, dry_run=True,
                        extra_env={"CORE_API_KEY": secret})
        assert secret not in out
        # dry-run n'écrit rien
        assert not config_path.exists()

    def test_status_masks_key_values(self, tmp_path):
        config_path = tmp_path / "claude_desktop_config.json"
        server_path = tmp_path / "mcp_server.py"
        server_path.write_text("# stub")
        secret = "s2-key-zzz"
        S.install(config_path, "/py", server_path, tmp_path, False,
                  extra_env={"SEMANTIC_SCHOLAR_API_KEY": secret})
        out = S.status(config_path)
        assert secret not in out

    def test_preserves_other_servers(self, tmp_path):
        config_path = tmp_path / "claude_desktop_config.json"
        config_path.write_text(json.dumps({
            "mcpServers": {"other": {"command": "x", "args": []}}
        }))
        server_path = tmp_path / "mcp_server.py"
        server_path.write_text("# stub")
        S.install(config_path, "/py", server_path, tmp_path, False)
        cfg = json.loads(config_path.read_text())
        assert "other" in cfg["mcpServers"]
        assert "moisson-shs" in cfg["mcpServers"]
