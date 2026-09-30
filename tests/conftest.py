"""
Fixtures pytest partagées par tous les tests.

Convention : un fichier `tests/fixtures/<nom>.<ext>` est chargé par une
fixture `<nom>` retournant le contenu parsé.

Isolation de l'environnement (appliquée à l'import de ce fichier, donc avant
que pytest n'importe le moindre module du projet) :

- ``MOISSON_NO_DOTENV=1`` : `utils/secrets.py` ne charge aucun `.env`. Sans
  cette garde, le `.env` de la racine du poste (clé SerpAPI, source facturée)
  entrait dans ``os.environ`` au premier import et les tests dépendaient du
  poste qui les exécutait.
- Les clés API et secrets serveur hérités du shell sont retirés
  (``_SECRET_ENV_VARS``). Un test qui a besoin d'une clé la pose lui-même
  (``monkeypatch.setenv`` ou ``api_key=`` explicite).
- Les réglages du cache HTTP hérités du shell (``MOISSON_CACHE``,
  ``MOISSON_CACHE_DIR``, ``MOISSON_CACHE_TTL``) sont retirés : aucun test ne
  lit ni n'écrit implicitement le cache disque du poste. Depuis OPT-09, le
  cache est actif par défaut dans ``moisson_search`` : ``MOISSON_CACHE_DIR``
  désigne donc un répertoire temporaire propre à la session de tests,
  supprimé à la sortie. De même, ``MOISSON_OUTPUT_DIR`` (répertoire de
  sortie par défaut du serveur MCP) : depuis OPT-16, lister les corpus écrit
  l'index dérivé ``.index.json`` ; aucun test ne touche le ``output/`` du
  projet.
- Les réglages de comportement hérités du shell (``_INHERITED_TUNING_VARS`` :
  politique HTTP, passe floue de déduplication, parcours Isidore, résolveur de
  DOI, recherches ciblées, classement, échéance souple et progression de
  ``moisson_search``, retour digest et pages de consultation) sont retirés :
  les valeurs par défaut s'appliquent.
- ``MOISSON_ZOTERO_SCHEMA_FILE`` pointe vers un schéma Zotero de test stable
  (``tests/fixtures/zotero_schema_test.json``) quand ce fichier existe : la
  validation ne dépend plus du réseau ni du cache disque du poste.

Exception volontaire : ``MOISSON_TEST_REAL_KEYS=1`` conserve les clés des
sources GRATUITES (OpenAlex, Semantic Scholar, CORE) venues du shell, pour les
tests d'intégration réseau (job CI `integration`, ou en local
``MOISSON_TEST_REAL_KEYS=1 pytest tests/integration -m integration``). Ces clés
ne sont visibles que des tests marqués ``integration`` : la fixture autouse
``_free_keys_only_for_integration`` les retire de tout autre test, pour que la
suite unitaire reste indépendante du poste même lancée avec l'opt-in. La clé
SerpAPI est retirée dans tous les cas : aucun test ne consomme de crédit
Scholar implicitement. Sans l'opt-in, si un test ``integration`` est ignoré,
le résumé de fin de session nomme les clés du shell ainsi masquées (le skip
« clé absente » vient alors de ce masquage).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# === Isolation de l'environnement — AVANT tout import du code du projet ===

# Clés des sources gratuites : retirées, sauf opt-in MOISSON_TEST_REAL_KEYS=1.
_FREE_SOURCE_KEYS = (
    "OPENALEX_API_KEY",           # config.ENV_OPENALEX_KEY
    "SEMANTIC_SCHOLAR_API_KEY",   # config.ENV_S2_KEY
    "CORE_API_KEY",               # config.ENV_CORE_KEY
)
# Secrets retirés sans exception : clé de la seule source facturée et secrets
# du serveur distant (auth.py, config.OAUTH_PASSWORD).
_ALWAYS_REMOVED_SECRETS = (
    "SERPAPI_API_KEY",            # config.ENV_SERPAPI_KEY (facturée)
    "MOISSON_API_TOKEN",          # auth.load_tokens
    "MOISSON_API_TOKENS",         # auth.load_tokens
    "MOISSON_OAUTH_PASSWORD",     # config.OAUTH_PASSWORD
)
_SECRET_ENV_VARS = _FREE_SOURCE_KEYS + _ALWAYS_REMOVED_SECRETS

# Réglages du cache HTTP hérités du shell : une option cache absente hérite de
# MOISSON_CACHE (utils/http.py), donc un shell avec MOISSON_CACHE=1 activerait
# un cache disque (~/.cache) dans les tests qui construisent un HTTPClient sans
# option. Un test qui veut le cache le pose lui-même (monkeypatch).
_INHERITED_CACHE_VARS = (
    "MOISSON_CACHE",
    "MOISSON_CACHE_DIR",
    "MOISSON_CACHE_TTL",
)
# Réglages de comportement hérités du shell (politique HTTP du Lot 0, lots A
# et B1) : lus à l'import de config.py ou à l'appel, ils changeraient les
# résultats attendus (passe floue désactivée, taille de page Isidore, pool du
# résolveur de DOI, délai de grâce de fetch_by_doi…). Un test qui veut une
# autre valeur la pose lui-même (monkeypatch).
_INHERITED_TUNING_VARS = (
    "MOISSON_HTTP_RETRY_AFTER_MAX",
    "MOISSON_HTTP_MAX_TOTAL_WAIT",
    "MOISSON_HTTP_REQUEST_BUDGET",
    "MOISSON_HTTP_MAX_IN_FLIGHT_PER_HOST",
    "MOISSON_MCP_CACHE_TTL",
    "MOISSON_DEDUP_FUZZY",
    "MOISSON_ISIDORE_REPLIES_PER_PAGE",
    "MOISSON_ISIDORE_RESULT_WINDOW",
    "MOISSON_ISIDORE_SCAN_MAX_RECORDS",
    "MOISSON_DOI_RESOLVE_WORKERS",
    "MOISSON_DOI_RESOLVE_MAX_SECONDS",
    "MOISSON_DOI_RESOLVE_MAX_SECONDS_CEILING",
    "MOISSON_DOI_QUERY_CACHE_SIZE",
    "MOISSON_DOI_QUERY_CACHE_TTL",
    "MOISSON_LOOKUP_MAX_WORKERS",
    "MOISSON_FETCH_BY_DOI_GRACE",
    "MOISSON_FETCH_BY_DOI_MAX_SECONDS",
    # OPT-09 : défauts MCP, parallélisme, reprise différée Isidore, politesse
    # CrossRef.
    "MOISSON_MCP_MAX_PER_SOURCE",
    "MOISSON_MCP_WORKERS",
    "MOISSON_HARVEST_MAX_WORKERS",
    "MOISSON_ISIDORE_DEFERRED_RETRY_S",
    "MOISSON_REQUEST_DELAY_CROSSREF",
    "MOISSON_REQUEST_DELAY_CROSSREF_LIST",
    "MOISSON_CROSSREF_MAX_IN_FLIGHT",
    # Revue d'OPT-09 : entretien du cache MCP, budget de la reprise différée.
    "MOISSON_MCP_CACHE_PRUNE_INTERVAL",
    "MOISSON_MCP_CACHE_MAX_MB",
    "MOISSON_ISIDORE_DEFERRED_RETRY_BUDGET_S",
    # OPT-12 : lot et borne de la pagination par rang de HAL, tri de
    # Semantic Scholar en mode bibliographie.
    "MOISSON_HAL_ROWS_PER_BATCH",
    "MOISSON_HAL_RANKED_MAX_RESULTS",
    "MOISSON_S2_BIBLIO_SORT",
    # OPT-14 : recherche d'auteur (seuil d'ambiguïté, candidats examinés et
    # détaillés, tailles de page), lus à l'import de utils.lookups.
    "MOISSON_AUTHOR_AMBIGUITY_MIN_WORKS",
    "MOISSON_AUTHOR_CANDIDATES",
    "MOISSON_AUTHOR_CANDIDATE_DETAILS",
    "MOISSON_AUTHOR_OPENALEX_PAGE_SIZE",
    "MOISSON_AUTHOR_S2_PAGE_SIZE",
    # OPT-13 : classement par défaut (config.RANKING_DEFAULT).
    "MOISSON_RANKING",
    # OPT-09 bis : échéance souple de moisson_search en stdio, délai de grâce
    # avant abandon d'une source, intervalle de la progression intermédiaire.
    "MOISSON_STDIO_SOFT_DEADLINE",
    "MOISSON_STDIO_SOFT_DEADLINE_GRACE",
    "MOISSON_PROGRESS_HEARTBEAT",
    # OPT-16 : publication atomique (fsync, âge des temporaires, verrou),
    # corpus_list (limite), audit des réponses natives (activation, bornes).
    "MOISSON_PUBLISH_FSYNC",
    "MOISSON_PUBLISH_TEMP_MAX_AGE",
    "MOISSON_PUBLISH_LOCK_TIMEOUT",
    "MOISSON_CORPUS_LIST_LIMIT",
    "MOISSON_CORPUS_LIST_MAX_LIMIT",
    "MOISSON_AUDIT_NATIVE",
    "MOISSON_AUDIT_NATIVE_MAX_RESPONSE_KB",
    "MOISSON_AUDIT_NATIVE_MAX_RUN_MB",
    "MOISSON_AUDIT_NATIVE_KEEP",
    # OPT-17 : retour des producteurs (mode par défaut, notices et plafond du
    # digest), pages de corpus_query et des ressources, lus à l'import de
    # utils.corpus_query.
    "MOISSON_INLINE_DEFAULT",
    "MOISSON_DIGEST_TOP",
    "MOISSON_DIGEST_MAX_TOP",
    "MOISSON_DIGEST_MAX_BYTES",
    "MOISSON_QUERY_LIMIT",
    "MOISSON_QUERY_MAX_LIMIT",
    "MOISSON_QUERY_MAX_BYTES",
    "MOISSON_RESOURCE_PAGE",
    "MOISSON_RESOURCE_PAGE_MAX",
)

os.environ["MOISSON_NO_DOTENV"] = "1"
_KEEP_FREE_KEYS = os.environ.get("MOISSON_TEST_REAL_KEYS", "").strip() == "1"
# Noms (jamais les valeurs) des clés gratuites du shell masquées faute
# d'opt-in : nommés dans le résumé de fin de session (pytest_terminal_summary).
_MASKED_FREE_KEYS = (
    ()
    if _KEEP_FREE_KEYS
    else tuple(v for v in _FREE_SOURCE_KEYS if os.environ.get(v))
)
for _var in _SECRET_ENV_VARS:
    if _KEEP_FREE_KEYS and _var in _FREE_SOURCE_KEYS:
        continue
    os.environ.pop(_var, None)
for _var in _INHERITED_CACHE_VARS + _INHERITED_TUNING_VARS:
    os.environ.pop(_var, None)

# Cache HTTP implicite (défaut de moisson_search depuis OPT-09) : jamais dans
# ~/.cache du poste, toujours dans un répertoire temporaire de la session.
_TEST_CACHE_ROOT = tempfile.mkdtemp(prefix="moisson-tests-cache-")
os.environ["MOISSON_CACHE_DIR"] = str(Path(_TEST_CACHE_ROOT) / "http")
# OPT-16 : répertoire de sortie par défaut du serveur MCP propre à la session
# de tests. corpus_list et list_resources tiennent désormais un index dérivé
# (OUTPUT_DIR/.index.json, verrou .moisson.lock) : sans cette garde, un test
# qui n'isole pas OUTPUT_DIR (serveur HTTP de test, par exemple) écrirait
# dans le output/ réel du projet.
os.environ["MOISSON_OUTPUT_DIR"] = str(Path(_TEST_CACHE_ROOT) / "output")
atexit.register(shutil.rmtree, _TEST_CACHE_ROOT, True)

# Schéma Zotero de test (fichier fourni avec la prise en charge de la variable
# par mappers/zotero_schema_loader.py). Tant qu'il n'existe pas, on n'hérite
# pas non plus d'une valeur du shell : le chargeur garde son comportement
# historique (cache disque, puis réseau, puis repli embarqué).
ZOTERO_SCHEMA_TEST_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "zotero_schema_test.json"
)
if ZOTERO_SCHEMA_TEST_FILE.is_file():
    os.environ["MOISSON_ZOTERO_SCHEMA_FILE"] = str(ZOTERO_SCHEMA_TEST_FILE)
else:
    os.environ.pop("MOISSON_ZOTERO_SCHEMA_FILE", None)

import pytest  # noqa: E402

# Permet d'importer le code source depuis n'importe quel test
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def pytest_terminal_summary(terminalreporter):
    """Signale, en fin de session, les clés gratuites du shell masquées faute
    d'opt-in quand un test ``integration`` a été ignoré : sans cette ligne, un
    skip « clé absente » laisse croire que la clé n'est pas définie alors
    qu'elle l'est dans le shell. Noms seulement, jamais les valeurs. (Résumé
    plutôt qu'en-tête : pytest -q n'affiche pas l'en-tête.)"""
    if not _MASKED_FREE_KEYS:
        return
    skipped_integration = any(
        "integration" in getattr(rep, "keywords", {})
        for rep in terminalreporter.stats.get("skipped", [])
    )
    if skipped_integration:
        terminalreporter.write_line(
            "clés API du shell masquées par tests/conftest.py : "
            + ", ".join(_MASKED_FREE_KEYS)
            + " (MOISSON_TEST_REAL_KEYS=1 pour les exposer aux tests integration)"
        )


@pytest.fixture(autouse=True)
def _free_keys_only_for_integration(request):
    """Sous l'opt-in MOISSON_TEST_REAL_KEYS=1, retire les clés gratuites de
    tout test non marqué ``integration``.

    L'opt-in vaut pour le processus entier (les ``skipif`` des tests
    d'intégration lisent ``os.environ`` à la collecte) ; sans cette fixture,
    un test unitaire qui suppose l'absence de clé ambiante échouerait dès que
    la suite complète est lancée avec l'opt-in et des clés exportées.
    """
    if not _KEEP_FREE_KEYS or request.node.get_closest_marker("integration"):
        yield
        return
    with pytest.MonkeyPatch.context() as mp:
        for var in _FREE_SOURCE_KEYS:
            mp.delenv(var, raising=False)
        yield


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Chemin absolu vers le répertoire des fixtures."""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def hal_solr_response() -> dict:
    """Réponse Solr HAL capturée — 5 documents réels."""
    return json.loads(
        (FIXTURES_DIR / "hal_solr_response.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def hal_first_doc(hal_solr_response) -> dict:
    """Premier document HAL de la fixture."""
    docs = hal_solr_response["response"]["docs"]
    assert docs, "fixture HAL vide"
    return docs[0]


@pytest.fixture(scope="session")
def isidore_response() -> dict:
    """Réponse Isidore capturée — 1 item réel."""
    return json.loads(
        (FIXTURES_DIR / "isidore_response.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def isidore_first_item(isidore_response) -> dict:
    """Premier item Isidore extrait."""
    replies = isidore_response["response"]["replies"]["content"]["reply"]
    if isinstance(replies, dict):
        return replies
    return replies[0]


@pytest.fixture(scope="session")
def oai_dc_cairn_xml() -> str:
    """Fichier XML OAI Dublin Core Cairn (record réel)."""
    return (FIXTURES_DIR / "oai_dc_record_cairn.xml").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def oai_dc_openedition_xml() -> str:
    """Fichier XML OAI Dublin Core OpenEdition (record réel)."""
    return (FIXTURES_DIR / "oai_oai_dc_record_openedition.xml").read_text(
        encoding="utf-8"
    )


@pytest.fixture(scope="session")
def crossref_work() -> dict:
    """Work CrossRef (un élément de message.items) — fixture réelle réduite."""
    return json.loads(
        (FIXTURES_DIR / "crossref_work.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def openalex_work() -> dict:
    """Work OpenAlex (un élément de results) — fixture réelle réduite."""
    return json.loads(
        (FIXTURES_DIR / "openalex_work.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def s2_paper() -> dict:
    """Paper Semantic Scholar (un élément de data) — fixture réelle réduite."""
    return json.loads(
        (FIXTURES_DIR / "s2_paper.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def core_work() -> dict:
    """Work CORE (un élément de results) — fixture réelle réduite."""
    return json.loads(
        (FIXTURES_DIR / "core_work.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def make_zotero_item():
    """
    Factory pour créer rapidement des items Zotero de test.

    Usage :
        item = make_zotero_item(itemType="journalArticle", title="...", DOI="...")
    """
    def _make(**overrides):
        base = {
            "itemType": "journalArticle",
            "title": "Default title",
            "creators": [
                {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}
            ],
            "abstractNote": "",
            "publicationTitle": "Test Journal",
            "date": "2024",
            "language": "fr",
            "DOI": "",
            "url": "",
            "tags": [],
            "extra": "Sources: Test",
            "collections": [],
            "relations": {},
        }
        base.update(overrides)
        return base
    return _make
