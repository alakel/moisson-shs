"""
Sprint « clés » (CLE-01, CLE-02) : réserve du `.env` et résolution des clés.

La déclaration du client (bloc ``env`` de Claude Desktop ou de Claude Code)
fait autorité ; le `.env` du projet sert de réserve, lue à part et jamais
injectée dans ``os.environ`` pour les quatre clés d'API :

- clé du client renseignée : elle sert ;
- variable non déclarée (CLI, ancienne déclaration) : la réserve sert ;
- variable déclarée vide : la réserve sert si la source figure dans
  ``MOISSON_KEY_FALLBACK`` (défaut : openalex, semantic_scholar, core ;
  jamais SerpAPI, source facturée) ;
- clé du client refusée (401, 403) : la réserve la remplace, aux mêmes
  conditions, pour le reste du processus.
"""

import types

import pytest

from utils import secrets


OA = "OPENALEX_API_KEY"
S2 = "SEMANTIC_SCHOLAR_API_KEY"
CORE = "CORE_API_KEY"
SERP = "SERPAPI_API_KEY"


@pytest.fixture(autouse=True)
def _clean_key_state(monkeypatch):
    """Ni clé du shell, ni réserve, ni refus mémorisé, politique par défaut."""
    for var in secrets.API_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(secrets.KEY_FALLBACK_ENV, raising=False)
    monkeypatch.setattr(secrets, "_RESERVE", {})
    secrets.reset_key_state()
    yield
    secrets.reset_key_state()


def _reserve(monkeypatch, **values):
    monkeypatch.setattr(secrets, "_RESERVE", dict(values))


# === Registre ===

class TestRegistry:
    def test_four_key_variables(self):
        assert set(secrets.API_KEY_VARS) == {OA, S2, CORE, SERP}

    def test_source_of_each_key(self):
        assert secrets.SOURCE_KEY_VARS == {
            "openalex": OA, "semantic_scholar": S2, "core": CORE,
            "google_scholar": SERP,
        }

    def test_default_policy_excludes_the_metered_source(self):
        assert secrets.fallback_allowed(OA)
        assert secrets.fallback_allowed(S2)
        assert secrets.fallback_allowed(CORE)
        assert not secrets.fallback_allowed(SERP)

    def test_policy_from_environment(self, monkeypatch):
        monkeypatch.setenv(secrets.KEY_FALLBACK_ENV, "openalex, google_scholar")
        assert secrets.fallback_allowed(OA)
        assert secrets.fallback_allowed(SERP)
        assert not secrets.fallback_allowed(S2)
        assert not secrets.fallback_allowed(CORE)

    def test_empty_policy_allows_nothing(self, monkeypatch):
        monkeypatch.setenv(secrets.KEY_FALLBACK_ENV, "")
        assert not any(secrets.fallback_allowed(v) for v in secrets.API_KEY_VARS)

    def test_unknown_variable_is_never_allowed(self):
        assert not secrets.fallback_allowed("MOISSON_TEST_KEY")


# === Résolution ===

class TestResolution:
    def test_client_key_wins(self, monkeypatch):
        monkeypatch.setenv(OA, " CLIENT ")
        _reserve(monkeypatch, **{OA: "RESERVE"})
        assert secrets.resolve_api_key(OA) == ("CLIENT", secrets.ORIGIN_CLIENT)
        assert secrets.get_api_key(OA) == "CLIENT"

    def test_undeclared_variable_uses_the_reserve(self, monkeypatch):
        _reserve(monkeypatch, **{CORE: "RESERVE"})
        assert secrets.resolve_api_key(CORE) == ("RESERVE", secrets.ORIGIN_ENV_FILE)

    def test_undeclared_metered_key_uses_the_reserve(self, monkeypatch):
        # CLI : sans déclaration de client, le .env est la configuration.
        _reserve(monkeypatch, **{SERP: "RESERVE"})
        assert secrets.get_api_key(SERP, required=True) == "RESERVE"

    def test_declared_empty_uses_the_reserve_when_allowed(self, monkeypatch):
        monkeypatch.setenv(OA, "")
        _reserve(monkeypatch, **{OA: "RESERVE"})
        assert secrets.resolve_api_key(OA) == ("RESERVE", secrets.ORIGIN_ENV_FILE)

    def test_declared_blank_counts_as_empty(self, monkeypatch):
        monkeypatch.setenv(S2, "   ")
        _reserve(monkeypatch, **{S2: "RESERVE"})
        assert secrets.get_api_key(S2) == "RESERVE"

    def test_declared_empty_metered_key_is_never_completed(self, monkeypatch):
        monkeypatch.setenv(SERP, "")
        _reserve(monkeypatch, **{SERP: "RESERVE"})
        assert secrets.resolve_api_key(SERP) == (None, None)
        with pytest.raises(secrets.MissingAPIKey) as err:
            secrets.get_api_key(SERP, required=True)
        assert err.value.env_var == SERP
        assert secrets.KEY_FALLBACK_ENV in str(err.value)
        assert "RESERVE" not in str(err.value)

    def test_declared_empty_without_reserve_is_absent(self, monkeypatch):
        monkeypatch.setenv(CORE, "")
        assert secrets.get_api_key(CORE) is None
        assert not secrets.has_api_key(CORE)

    def test_policy_can_open_the_metered_key(self, monkeypatch):
        monkeypatch.setenv(secrets.KEY_FALLBACK_ENV, "google_scholar")
        monkeypatch.setenv(SERP, "")
        _reserve(monkeypatch, **{SERP: "RESERVE"})
        assert secrets.get_api_key(SERP) == "RESERVE"

    def test_other_variables_are_unchanged(self, monkeypatch):
        monkeypatch.setenv("MOISSON_TEST_KEY", "abc")
        assert secrets.resolve_api_key("MOISSON_TEST_KEY") == (
            "abc", secrets.ORIGIN_CLIENT)


# === Refus de la clé du client (CLE-02) ===

class TestRefusal:
    def test_refused_client_key_is_replaced_by_the_reserve(self, monkeypatch):
        monkeypatch.setenv(OA, "CLIENT")
        _reserve(monkeypatch, **{OA: "RESERVE"})
        assert secrets.substitute_credential("CLIENT", on_refusal=True,
                                             status=401) == (OA, "RESERVE")
        assert secrets.refused_status(OA) == 401
        assert secrets.resolve_api_key(OA) == (
            "RESERVE", secrets.ORIGIN_ENV_FILE_AFTER_REFUSAL)

    def test_bearer_credential(self, monkeypatch):
        monkeypatch.setenv(CORE, "CLIENT")
        _reserve(monkeypatch, **{CORE: "RESERVE"})
        assert secrets.substitute_credential(
            "Bearer CLIENT", on_refusal=True, status=403) == (CORE, "Bearer RESERVE")

    def test_no_substitution_before_a_refusal(self, monkeypatch):
        monkeypatch.setenv(OA, "CLIENT")
        _reserve(monkeypatch, **{OA: "RESERVE"})
        assert secrets.substitute_credential("CLIENT") is None
        secrets.substitute_credential("CLIENT", on_refusal=True, status=401)
        # Refus mémorisé : les requêtes suivantes partent avec la réserve.
        assert secrets.substitute_credential("CLIENT") == (OA, "RESERVE")

    def test_metered_key_is_not_replaced(self, monkeypatch):
        monkeypatch.setenv(SERP, "CLIENT")
        _reserve(monkeypatch, **{SERP: "RESERVE"})
        assert secrets.substitute_credential("CLIENT", on_refusal=True,
                                             status=401) is None
        assert secrets.refused_status(SERP) is None

    def test_identical_reserve_is_not_a_fallback(self, monkeypatch):
        monkeypatch.setenv(OA, "SAME")
        _reserve(monkeypatch, **{OA: "SAME"})
        assert secrets.substitute_credential("SAME", on_refusal=True,
                                             status=401) is None
        assert secrets.refused_status(OA) is None

    def test_no_reserve_no_fallback(self, monkeypatch):
        monkeypatch.setenv(OA, "CLIENT")
        assert secrets.substitute_credential("CLIENT", on_refusal=True,
                                             status=401) is None

    def test_reserve_key_itself_is_never_substituted(self, monkeypatch):
        # La clé de réserve refusée à son tour : pas de boucle.
        monkeypatch.setenv(OA, "CLIENT")
        _reserve(monkeypatch, **{OA: "RESERVE"})
        assert secrets.substitute_credential("RESERVE", on_refusal=True,
                                             status=401) is None

    def test_unrelated_credential(self, monkeypatch):
        monkeypatch.setenv(OA, "CLIENT")
        _reserve(monkeypatch, **{OA: "RESERVE"})
        assert secrets.substitute_credential("AUTRE", on_refusal=True,
                                             status=401) is None
        assert secrets.substitute_credential("", on_refusal=True,
                                             status=401) is None

    def test_reset_forgets_refusals(self, monkeypatch):
        monkeypatch.setenv(OA, "CLIENT")
        _reserve(monkeypatch, **{OA: "RESERVE"})
        secrets.substitute_credential("CLIENT", on_refusal=True, status=401)
        secrets.reset_key_state()
        assert secrets.resolve_api_key(OA) == ("CLIENT", secrets.ORIGIN_CLIENT)


# === Chargeur du .env (CLE-01) ===

def _fake_dotenv(monkeypatch, files):
    """Faux module dotenv : ``files`` associe un chemin à ses variables."""
    fake = types.ModuleType("dotenv")
    calls = []

    def dotenv_values(path):
        calls.append(path)
        value = files[str(path)]
        if isinstance(value, BaseException):
            raise value
        return dict(value)

    fake.dotenv_values = dotenv_values
    fake.find_dotenv = lambda *a, **k: ""
    monkeypatch.setitem(__import__("sys").modules, "dotenv", fake)
    return calls


class TestLoader:
    def test_keys_go_to_the_reserve_not_the_environment(self, monkeypatch,
                                                        tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("x", encoding="utf-8")
        _fake_dotenv(monkeypatch, {str(env_file): {
            OA: "RESERVE", "MOISSON_CLES_SENTINEL": "chargee"}})
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        monkeypatch.delenv("MOISSON_CLES_SENTINEL", raising=False)
        assert secrets._load_dotenv_files([env_file]) is True
        assert secrets._RESERVE == {OA: "RESERVE"}
        assert OA not in __import__("os").environ
        # Les autres variables sont chargées comme avant, sans écrasement.
        assert __import__("os").environ["MOISSON_CLES_SENTINEL"] == "chargee"
        monkeypatch.delenv("MOISSON_CLES_SENTINEL", raising=False)

    def test_defined_variable_is_not_overwritten(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("x", encoding="utf-8")
        _fake_dotenv(monkeypatch, {str(env_file): {"MOISSON_CLES_SENTINEL": "fichier"}})
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        monkeypatch.setenv("MOISSON_CLES_SENTINEL", "client")
        secrets._load_dotenv_files([env_file])
        assert __import__("os").environ["MOISSON_CLES_SENTINEL"] == "client"

    def test_first_file_wins_for_the_reserve(self, monkeypatch, tmp_path):
        first, second = tmp_path / "a.env", tmp_path / "b.env"
        first.write_text("x", encoding="utf-8")
        second.write_text("x", encoding="utf-8")
        _fake_dotenv(monkeypatch, {str(first): {OA: "PROJET"},
                                   str(second): {OA: "COURANT", CORE: "C"}})
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        secrets._load_dotenv_files([first, second])
        assert secrets._RESERVE == {OA: "PROJET", CORE: "C"}

    def test_empty_values_are_not_reserve_keys(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("x", encoding="utf-8")
        _fake_dotenv(monkeypatch, {str(env_file): {OA: "", S2: None, CORE: " K "}})
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        secrets._load_dotenv_files([env_file])
        assert secrets._RESERVE == {CORE: "K"}

    def test_unreadable_file_is_skipped(self, monkeypatch, tmp_path):
        # Fichier évincé du disque (Google Drive), droits refusés (sandbox).
        bad, good = tmp_path / "bad.env", tmp_path / "good.env"
        bad.write_text("x", encoding="utf-8")
        good.write_text("x", encoding="utf-8")
        _fake_dotenv(monkeypatch, {str(bad): PermissionError("refus"),
                                   str(good): {CORE: "K"}})
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        assert secrets._load_dotenv_files([bad, good]) is True
        assert secrets._RESERVE == {CORE: "K"}

    def test_missing_file_is_skipped(self, monkeypatch, tmp_path):
        calls = _fake_dotenv(monkeypatch, {})
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        assert secrets._load_dotenv_files([tmp_path / "absent.env"]) is True
        assert calls == [] and secrets._RESERVE == {}

    def test_real_dotenv_and_declared_empty_variable(self, monkeypatch, tmp_path):
        # Vrai python-dotenv : la variable déclarée vide par le client reste
        # vide dans l'environnement, la clé du fichier sert de réserve.
        pytest.importorskip("dotenv")
        env_file = tmp_path / ".env"
        env_file.write_text(f"{OA}=reserve-fichier\nMOISSON_CLES_SENTINEL=ok\n",
                            encoding="utf-8")
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        monkeypatch.delenv("MOISSON_CLES_SENTINEL", raising=False)
        monkeypatch.setenv(OA, "")
        secrets._load_dotenv_files([env_file])
        assert __import__("os").environ[OA] == ""
        assert secrets.get_api_key(OA) == "reserve-fichier"
        assert __import__("os").environ["MOISSON_CLES_SENTINEL"] == "ok"
        monkeypatch.delenv("MOISSON_CLES_SENTINEL", raising=False)

    def test_guard_skips_everything(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("x", encoding="utf-8")
        calls = _fake_dotenv(monkeypatch, {str(env_file): {OA: "RESERVE"}})
        monkeypatch.setenv("MOISSON_NO_DOTENV", "1")
        assert secrets._load_dotenv_files([env_file]) is False
        assert calls == [] and secrets._RESERVE == {}
