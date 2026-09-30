"""
Tests OPT-01 / OPT-00 — isolation des tests vis-à-vis des secrets et du `.env`.

Couvre :
- `tests/conftest.py` pose ``MOISSON_NO_DOTENV=1`` et retire du processus de
  test les clés API et secrets serveur hérités du shell ou d'un `.env` ;
- `utils/secrets.py` ne charge aucun `.env` quand ``MOISSON_NO_DOTENV=1``
  (vérifié en processus, avec un faux module dotenv, et de bout en bout dans
  un sous-processus avec un vrai `.env` sentinelle) ;
- le schéma Zotero de test est désigné quand son fichier existe ;
- les listes de secrets du conftest RÉEL couvrent toute constante
  ``ENV_*_KEY`` de config.py, et une mini-session pytest lancée dans un
  sous-processus avec une copie du conftest réel prouve qu'aucune de ces
  variables n'atteint un test, sauf les clés gratuites sous l'opt-in
  ``MOISSON_TEST_REAL_KEYS=1``, et alors pour les seuls tests ``integration``.

Aucun appel réseau, aucune socket.
"""

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

CONFTEST_PATH = PROJECT_ROOT / "tests" / "conftest.py"

# Contrat attendu (spécification, confrontée plus bas au conftest réel) :
# secrets retirés sans exception (clé facturée + serveur distant)...
ALWAYS_REMOVED = (
    "SERPAPI_API_KEY",
    "MOISSON_API_TOKEN",
    "MOISSON_API_TOKENS",
    "MOISSON_OAUTH_PASSWORD",
)
# ... et clés des sources gratuites, retirées sauf opt-in MOISSON_TEST_REAL_KEYS=1.
FREE_SOURCE_KEYS = (
    "OPENALEX_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "CORE_API_KEY",
)
# Secrets serveur lus en littéral par auth.py / config.py (pas de constante).
SERVER_SECRETS = (
    "MOISSON_API_TOKEN",
    "MOISSON_API_TOKENS",
    "MOISSON_OAUTH_PASSWORD",
)


def _config_key_vars() -> set:
    """Variables de clé API déclarées dans config.py (constantes ENV_*_KEY)."""
    import config

    return {
        getattr(config, name)
        for name in dir(config)
        if name.startswith("ENV_") and name.endswith("_KEY")
    }


@pytest.fixture(scope="module")
def conftest_module(request):
    """Le module tests/conftest.py tel que pytest l'a chargé (pas une copie)."""
    target = CONFTEST_PATH.resolve()
    for plugin in request.config.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and Path(path).resolve() == target:
            return plugin
    pytest.fail("tests/conftest.py introuvable parmi les plugins pytest")


# === Environnement posé par tests/conftest.py ===

class TestConftestIsolation:
    def test_no_dotenv_flag_is_set(self):
        assert os.environ.get("MOISSON_NO_DOTENV") == "1"

    def test_billed_key_and_server_secrets_absent(self):
        # Même si le poste a un .env (clé SerpAPI) ou des jetons exportés,
        # aucun ne doit être visible des tests — y compris après l'import de
        # utils.secrets, qui chargeait le .env de la racine.
        import utils.secrets  # noqa: F401

        present = [v for v in ALWAYS_REMOVED if v in os.environ]
        assert present == []

    def test_free_source_keys_absent_from_unit_tests(self):
        # Test non marqué integration : aucune clé gratuite visible, même si la
        # session a été lancée avec l'opt-in MOISSON_TEST_REAL_KEYS=1.
        present = [v for v in FREE_SOURCE_KEYS if v in os.environ]
        assert present == []

    def test_conftest_lists_every_key_read_by_config(self, conftest_module):
        """Toute variable de clé déclarée dans config.py figure dans les listes
        du conftest réel (et non dans une copie locale à ce fichier)."""
        declared = _config_key_vars()
        assert declared, "aucune constante ENV_*_KEY trouvée dans config.py"
        assert declared <= set(conftest_module._SECRET_ENV_VARS)

    def test_conftest_lists_match_contract(self, conftest_module):
        # Seules les clés gratuites bénéficient de l'opt-in ; la clé facturée
        # et les secrets serveur sont retirés sans exception.
        assert set(conftest_module._FREE_SOURCE_KEYS) == set(FREE_SOURCE_KEYS)
        assert set(ALWAYS_REMOVED) <= set(conftest_module._ALWAYS_REMOVED_SECRETS)
        assert "SERPAPI_API_KEY" not in conftest_module._FREE_SOURCE_KEYS

    def test_zotero_schema_test_file_designated_when_present(self):
        fixture = PROJECT_ROOT / "tests" / "fixtures" / "zotero_schema_test.json"
        if fixture.is_file():
            assert os.environ.get("MOISSON_ZOTERO_SCHEMA_FILE") == str(fixture)
        else:
            assert "MOISSON_ZOTERO_SCHEMA_FILE" not in os.environ


# === Garde MOISSON_NO_DOTENV de utils/secrets.py ===

def _fake_dotenv(monkeypatch):
    """Installe un faux module `dotenv` qui enregistre ses appels."""
    calls = []
    fake = types.ModuleType("dotenv")
    fake.load_dotenv = lambda *a, **k: calls.append((a, k)) or True
    monkeypatch.setitem(sys.modules, "dotenv", fake)
    return calls


class TestSecretsDotenvGuard:
    def test_guard_skips_every_dotenv_load(self, monkeypatch):
        from utils import secrets

        calls = _fake_dotenv(monkeypatch)
        monkeypatch.setenv("MOISSON_NO_DOTENV", "1")
        assert secrets._load_dotenv_files() is False
        assert calls == []

    def test_loads_dotenv_when_guard_absent(self, monkeypatch):
        from utils import secrets

        calls = _fake_dotenv(monkeypatch)
        monkeypatch.delenv("MOISSON_NO_DOTENV", raising=False)
        assert secrets._load_dotenv_files() is True
        # Au moins la recherche par défaut ; le .env de la racine en plus s'il
        # existe sur ce poste.
        assert calls
        project_env = PROJECT_ROOT / ".env"
        if project_env.is_file():
            assert calls[0][0][0] == project_env

    def test_other_values_do_not_disable_loading(self, monkeypatch):
        from utils import secrets

        calls = _fake_dotenv(monkeypatch)
        monkeypatch.setenv("MOISSON_NO_DOTENV", "0")
        assert secrets._load_dotenv_files() is True
        assert calls


def _import_secrets_in_subprocess(cwd: Path, no_dotenv: bool) -> str:
    """Importe utils.secrets dans un processus neuf lancé depuis ``cwd`` et
    renvoie la valeur vue de la variable sentinelle (ou <absente>)."""
    env = {k: v for k, v in os.environ.items() if k != "MOISSON_NO_DOTENV"}
    env.pop("MOISSON_OPT01_SENTINEL", None)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    if no_dotenv:
        env["MOISSON_NO_DOTENV"] = "1"
    code = (
        "import os, utils.secrets; "
        "print(os.environ.get('MOISSON_OPT01_SENTINEL', '<absente>'))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TestSecretsDotenvEndToEnd:
    """Vrai python-dotenv, vrai fichier `.env` sentinelle dans le répertoire
    courant d'un sous-processus (recherche par défaut de dotenv)."""

    @pytest.fixture
    def sentinel_dir(self, tmp_path):
        pytest.importorskip("dotenv")
        (tmp_path / ".env").write_text(
            "MOISSON_OPT01_SENTINEL=chargee\n", encoding="utf-8"
        )
        return tmp_path

    def test_control_dotenv_is_loaded_without_guard(self, sentinel_dir):
        # Témoin : sans la garde, le .env du répertoire courant est bien lu —
        # sinon le test suivant ne prouverait rien.
        assert _import_secrets_in_subprocess(sentinel_dir, no_dotenv=False) == "chargee"

    def test_guard_prevents_loading(self, sentinel_dir):
        assert _import_secrets_in_subprocess(sentinel_dir, no_dotenv=True) == "<absente>"


# === Mini-session pytest avec le conftest réel (sous-processus) ===

_SENTINEL_VALUE = "valeur-secrete-opt01"

_MINI_PYTEST_INI = """\
[pytest]
addopts = --strict-markers
markers =
    integration: tests nécessitant le réseau
"""

# Test ordinaire (unitaire), test marqué au niveau du module (comme
# tests/integration/, avec un skipif « clé absente » lu à la collecte), et
# module mixte (marqueur lu test par test). Chaque test écrit les variables
# surveillées qu'il voit dans <nom>.json.
_MINI_TESTS = {
    "test_unit.py": """\
        from probe import dump


        def test_unit():
            dump("unit")
        """,
    "test_integ_module.py": """\
        import os

        import pytest

        from probe import dump

        pytestmark = pytest.mark.integration


        def test_integ_module():
            dump("integ_module")


        @pytest.mark.skipif(not os.environ.get("OPENALEX_API_KEY"),
                            reason="OPENALEX_API_KEY absente")
        def test_integ_needs_key():
            dump("integ_needs_key")
        """,
    "test_mixed.py": """\
        import pytest

        from probe import dump


        def test_mixed_unit():
            dump("mixed_unit")


        @pytest.mark.integration
        def test_mixed_integ():
            dump("mixed_integ")
        """,
}


def _run_mini_session(tmp_path: Path, opt_in: bool):
    """Lance une session pytest neuve avec une COPIE EXACTE de tests/conftest.py,
    toutes les variables surveillées définies dans l'environnement, et renvoie
    (sortie, {nom de test: variables visibles pendant le test})."""
    watched = sorted(_config_key_vars() | set(SERVER_SECRETS))
    session = tmp_path / "mini_session"
    session.mkdir()
    (session / "conftest.py").write_bytes(CONFTEST_PATH.read_bytes())
    (session / "pytest.ini").write_text(_MINI_PYTEST_INI, encoding="utf-8")
    (session / "probe.py").write_text(textwrap.dedent(f"""\
        import json
        import os
        from pathlib import Path

        WATCHED = {watched!r}


        def dump(name):
            visible = sorted(v for v in WATCHED if v in os.environ)
            (Path(__file__).parent / f"{{name}}.json").write_text(json.dumps(visible))
        """), encoding="utf-8")
    for filename, body in _MINI_TESTS.items():
        (session / filename).write_text(textwrap.dedent(body), encoding="utf-8")

    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COV_CORE_"))
        and k not in ("MOISSON_TEST_REAL_KEYS", "MOISSON_NO_DOTENV")
    }
    for var in watched:
        env[var] = _SENTINEL_VALUE
    if opt_in:
        env["MOISSON_TEST_REAL_KEYS"] = "1"
    env["PYTHONPATH"] = str(session)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(session), str(session)],
        cwd=str(session), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    results = {
        f.stem: json.loads(f.read_text()) for f in session.glob("*.json")
    }
    return proc.stdout + proc.stderr, results


class TestConftestMiniSession:
    """Éprouve le conftest réel : si une variable disparaît de ses listes, ou
    si l'opt-in fuit vers les tests unitaires, ces tests échouent — y compris
    en CI, où aucune clé n'est présente dans l'environnement du job."""

    def test_without_opt_in_no_secret_reaches_any_test(self, tmp_path):
        output, results = _run_mini_session(tmp_path, opt_in=False)
        # Le test à clé est ignoré (skipif lu à la collecte) ; tous les autres
        # tournent et ne voient aucune des variables surveillées.
        assert results == {
            "unit": [], "integ_module": [], "mixed_unit": [], "mixed_integ": [],
        }
        # Le résumé explique ce skip : clés du shell masquées, nommées par
        # leur nom seulement, jamais par leur valeur.
        assert "clés API du shell masquées" in output
        assert "MOISSON_TEST_REAL_KEYS=1" in output
        for var in FREE_SOURCE_KEYS:
            assert var in output
        assert _SENTINEL_VALUE not in output

    def test_opt_in_exposes_free_keys_to_integration_tests_only(self, tmp_path):
        output, results = _run_mini_session(tmp_path, opt_in=True)
        # Les tests ordinaires ne voient toujours rien.
        assert results["unit"] == []
        assert results["mixed_unit"] == []
        # Les tests integration voient les clés gratuites, et elles seules :
        # ni la clé SerpAPI (facturée) ni les secrets serveur.
        free = sorted(FREE_SOURCE_KEYS)
        assert results["integ_module"] == free
        assert results["integ_needs_key"] == free
        assert results["mixed_integ"] == free
        assert "clés API du shell masquées" not in output
        assert _SENTINEL_VALUE not in output
