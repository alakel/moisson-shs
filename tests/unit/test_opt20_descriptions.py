"""
Tests OPT-20 — descriptions des outils MCP et de leurs arguments.

Hors réseau, sans socket : appel direct de ``list_tools``.

- Chaque argument déclaré est décrit ; chaque identifiant cité dans une
  description (``nom_en_snake_case``) est un argument de l'outil, un outil,
  une source, une valeur d'énumération ou un champ de retour réellement
  produit par le code.
- Défauts annoncés = défauts du code (``config.py``, ``utils.corpus_query``).
- Régimes de requête, filtres de dates et de langues, ordres par mode :
  cohérents avec le registre ``connectors/capabilities.py`` ; l'affirmation
  « Scholar honore les guillemets que les quatre sources internationales
  ignorent » a disparu.
- Statuts nommés = ``engine.models.STATUS_LABELS`` ; enrichissement OAI =
  ``engine.orchestrator.ENRICH_OAI_SOURCES`` ; résolveurs de DOI ; libellés
  de sources de ``corpus_filter`` = ``utils.source_labels``.
- Docstring de tête de ``connectors/google_scholar.py`` (C34).
"""

import ast
import asyncio
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from connectors import capabilities as caps  # noqa: E402
from connectors.registry import SOURCE_GROUPS, list_source_tokens  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402

HISTORICAL_TOOLS = {
    "moisson_search", "corpus_analyze", "corpus_list", "fetch_by_doi",
    "search_by_author", "corpus_merge", "corpus_to_csl", "corpus_filter",
    "corpus_resolve_dois",
}
LOT_D_TOOLS = {"corpus_export", "corpus_query", "corpus_diff"}

# Identifiants cités par les descriptions sans être des arguments : champs
# de retour (structuredContent), valeurs de statut, suffixe de fichier. Chacun
# doit exister dans le code (vérifié plus bas).
RETURN_FIELDS = {
    "query_plan", "top_items", "top_ranked", "harvest_status",
    "production_complete", "profiles_to_confirm", "rejected_not_author",
    "report_compact", "report_detail", "byte_cap", "non_trouve",
    "doi_report",
}
# Noms extérieurs au code : outil d'un serveur Zotero tiers, format OAI.
EXTERNAL_NAMES = {"zotero_add_by_doi", "oai_openaire", "fulltext_t"}

SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _tools():
    return {t.name: t for t in asyncio.run(mcp_server.list_tools())}


def _props(name):
    return _tools()[name].inputSchema["properties"]


def _desc(name, prop=None):
    tool = _tools()[name]
    if prop is None:
        return tool.description
    return tool.inputSchema["properties"][prop]["description"]


def _texts(tool):
    yield tool.description
    for spec in tool.inputSchema["properties"].values():
        yield spec.get("description", "")


_CODE_FILES = ("mcp_server.py", "utils/lookups.py", "utils/corpus_query.py",
               "utils/output_files.py", "utils/doi_resolver.py")
# Fonctions et constantes qui produisent les textes lus par l'agent
# (descriptions d'outils, prompts) : exclues du « code » où l'on cherche les
# champs de retour, sans quoi un champ cité par une description se
# trouverait dans sa propre description (revue d'OPT-20).
_TEXT_PRODUCERS = {"list_tools", "_digest_arguments", "_soft_deadline_schema",
                   "list_prompts", "get_prompt", "_labels_without_language"}


def _is_text_producer(node) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name in _TEXT_PRODUCERS or node.name.startswith("_prompt_")
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id.startswith("_PROMPT_")
                   for t in node.targets)
    return False


def _code_string_constants() -> set:
    """Chaînes littérales du code (clés et valeurs rendues), hors textes
    destinés à l'agent."""
    found = set()
    for name in _CODE_FILES:
        tree = ast.parse((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if _is_text_producer(node):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    found.add(sub.value)
    return found


# === Arguments et identifiants ===

class TestArgumentsAreDescribed:
    def test_all_expected_tools_are_listed(self):
        assert HISTORICAL_TOOLS | LOT_D_TOOLS <= set(_tools())

    def test_every_argument_has_a_description(self):
        for name, tool in _tools().items():
            assert tool.description and len(tool.description) > 80, name
            for prop, spec in tool.inputSchema["properties"].items():
                text = spec.get("description", "")
                assert len(text) >= 15, (name, prop)

    def test_required_arguments_unchanged(self):
        tools = _tools()
        expected = {
            "moisson_search": ["query"], "corpus_analyze": ["corpus_path"],
            "fetch_by_doi": ["doi"], "search_by_author": ["name"],
            "corpus_merge": ["corpus_paths"], "corpus_to_csl": ["corpus_path"],
            "corpus_filter": ["corpus_path"],
            "corpus_resolve_dois": ["corpus_path"],
            "corpus_export": ["corpus_path"], "corpus_query": ["corpus"],
            "corpus_diff": ["corpus_a", "corpus_b"],
        }
        for name, required in expected.items():
            assert tools[name].inputSchema.get("required") == required, name
        assert "required" not in tools["corpus_list"].inputSchema

    def test_nested_filters_are_named_in_their_description(self):
        spec = _props("corpus_query")["filters"]
        nested = set(spec["properties"])
        listed = set(SNAKE.findall(spec["description"])) | set(
            re.findall(r"\b[a-z]+(?=[ ,)])", spec["description"]))
        assert nested <= listed, nested - listed

    def test_cited_identifiers_exist(self):
        """Un identifiant cité est un argument de l'outil (ou d'un filtre
        imbriqué), une valeur d'énumération, un outil, une source ou un
        groupe, un champ de retour produit par le code, ou un nom extérieur
        déclaré."""
        tools = _tools()
        known = set(tools) | set(list_source_tokens()) | RETURN_FIELDS \
            | EXTERNAL_NAMES
        for name, tool in tools.items():
            allowed = set(known) | set(tool.inputSchema["properties"])
            for spec in tool.inputSchema["properties"].values():
                allowed |= set(spec.get("properties") or {})
                allowed |= {v for v in spec.get("enum") or []
                            if isinstance(v, str)}
            for text in _texts(tool):
                unknown = set(SNAKE.findall(text)) - allowed
                assert not unknown, (name, unknown)

    def test_return_fields_exist_in_the_code(self):
        """Chaque champ de retour cité est une chaîne littérale du code
        (clé de structuredContent, statut, rôle de fichier), hors des
        descriptions et des prompts qui le citent."""
        constants = _code_string_constants()
        for field in RETURN_FIELDS:
            assert field in constants, field

    def test_description_texts_are_excluded_from_the_code(self):
        """Garde-fou du test précédent : les textes des descriptions et des
        prompts ne comptent pas comme du code."""
        constants = _code_string_constants()
        for fragment in ("Moisson bibliographique multi-sources",
                         "Règles de restitution",
                         "Sélection : explique comment"):
            assert not any(fragment in c for c in constants), fragment


# === moisson_search : défauts, sources, groupes ===

class TestMoissonSearchDefaults:
    def test_defaults_come_from_the_code(self):
        desc = _desc("moisson_search")
        assert f"{config.MCP_DEFAULT_MAX_PER_SOURCE} notices PAR source" in desc
        assert f"({config.MCP_DEFAULT_WORKERS} à la fois" in desc
        assert f"pendant {config.MCP_CACHE_TTL // 3600} h" in desc
        assert f"{config.ISIDORE_RESULT_WINDOW} premiers résultats" in desc
        assert "mode 'bibliographie'" in desc

    def test_statuses_are_the_engine_labels(self):
        from engine.models import STATUS_LABELS
        desc = _desc("moisson_search")
        for label in STATUS_LABELS.values():
            assert label in desc, label
        assert "sans résultat" not in desc

    def test_scholar_is_named_and_outside_the_groups(self):
        desc = _desc("moisson_search")
        assert "Google Scholar" in desc and "facturé" in desc
        assert "'google_scholar'" in desc and "'scholar'" in desc
        assert "absent des groupes 'international' et 'all'" in desc
        sources = _desc("moisson_search", "sources")
        for group in SOURCE_GROUPS:
            assert f"'{group}'" in sources, group
        assert "google_scholar" in sources

    def test_keys(self):
        desc = _desc("moisson_search")
        assert "OpenAlex, CORE et Google Scholar les exigent" in desc
        assert "Semantic Scholar la rend facultative" in desc
        assert "3 d'entre elles" not in desc

    def test_scholar_page_cap(self):
        text = _desc("moisson_search", "max_per_source")
        assert f"{config.SERPAPI_MAX_PAGES} pages de " \
            f"{config.SERPAPI_PAGE_SIZE} notices" in text
        assert "MOISSON_SERPAPI_MAX_PAGES" in text

    def test_digest_arguments(self):
        inline = _desc("moisson_search", "inline")
        assert f"'{CQ.DEFAULT_INLINE}'" in inline
        assert f"{CQ.DIGEST_MAX_BYTES // 1000} Ko" in inline
        assert "offset=k" in inline and "ids" in inline
        top = _desc("moisson_search", "top")
        assert f"défaut {CQ.DIGEST_DEFAULT_TOP}" in top

    def test_ranking(self):
        text = _desc("moisson_search", "ranking")
        assert "'native' (défaut)" in text
        assert "désactivé par défaut tant que son évaluation" in text
        assert "citations n'entrent jamais dans le score" in text
        assert config.RANKING_DEFAULT == "native"

    def test_deadline_seconds_default(self):
        text = _desc("moisson_search", "deadline_seconds")
        assert f"{config.STDIO_SOFT_DEADLINE_S:g} s" in text
        assert f"{config.STDIO_SOFT_DEADLINE_MAX_S} s" in text

    def test_cache_default(self):
        text = _desc("moisson_search", "cache")
        assert "ACTIVÉ par défaut" in text
        assert f"{config.MCP_CACHE_TTL // 3600} h" in text


# === Régimes de requête et filtres : registre des capacités ===

def _native_sources():
    return {k: c for k, c in caps.CAPABILITIES.items() if not c.proxy_of}


class TestQueryRegimes:
    def test_no_blanket_claim(self):
        for tool in _tools().values():
            for text in _texts(tool):
                assert "ignorent" not in text
                assert "quatre sources internationales" not in text

    def test_every_source_is_described(self):
        text = _desc("moisson_search", "query")
        for key, cap in _native_sources().items():
            assert cap.label in text, key
        assert caps.VERIFIED_ON in text

    def test_bag_of_words_sources(self):
        text = _desc("moisson_search", "query")
        sentence = next(s for s in text.split(";") if "sac de mots" in s)
        for cap in _native_sources().values():
            if cap.syntax == caps.SYNTAX_BAG:
                assert cap.label in sentence

    def test_documented_sources_are_flagged(self):
        text = _desc("moisson_search", "query")
        before, _, documented = text.partition(
            "D'après leur documentation seulement")
        verified = before.partition("régimes vérifiés")[2]
        assert documented and verified
        for cap in _native_sources().values():
            if cap.status == caps.STATUS_DOCUMENTED:
                assert cap.label in documented, cap.label
                assert cap.label not in verified, cap.label
            else:
                assert cap.label in verified, cap.label

    def test_missing_not_operator_is_said(self):
        text = _desc("moisson_search", "query")
        for cap in _native_sources().values():
            if cap.not_op is None and cap.syntax != caps.SYNTAX_BAG:
                i = text.index(cap.label + " honore")
                assert "pas NOT" in text[i:i + 120], cap.label

    def test_operators_named_per_source(self):
        text = _desc("moisson_search", "query")
        assert "ET/OU/SAUF" in text and "+ | -" in text
        # Les opérateurs anglais sont lus comme des mots chez Isidore, ignorés
        # chez Semantic Scholar : ils sont traduits.
        assert text.count("traduits") >= 2

    def test_accents(self):
        text = _desc("moisson_search", "query")
        significant = [c.label for c in _native_sources().values()
                       if c.accents == "significant"]
        segment = text[text.index("Accents significatifs"):]
        segment = segment[:segment.index(")") + 1]
        for label in significant:
            assert label in segment, label

    def test_dates_native_versus_local(self):
        text = _desc("moisson_search", "from_date")
        native = text[text.index("Filtre natif"):text.index("filtre local")]
        local = text[text.index("filtre local"):]
        for key, cap in _native_sources().items():
            if cap.dates == caps.FILTER_NATIVE:
                assert cap.label in native, key
            else:
                assert cap.label in local and cap.label not in native, key

    def test_languages_native_local_impossible(self):
        text = _desc("moisson_search", "languages")
        native = text[text.index("Filtre natif"):text.index("filtre local")]
        local = text[text.index("filtre local"):text.index("impossible")]
        impossible = text[text.index("impossible"):]
        for key, cap in _native_sources().items():
            part = {caps.FILTER_NATIVE: native, caps.FILTER_LOCAL: local,
                    caps.FILTER_NONE: impossible}[cap.languages]
            assert cap.label in part, key

    def test_mode_orders(self):
        text = _desc("moisson_search", "mode")
        biblio, _, collecte = text.partition("'collecte'")
        for key, cap in _native_sources().items():
            if key == "isidore":
                continue      # présumé : nommé, ordre non vérifié
            assert cap.label in biblio, key
        assert "citations" in biblio and "Semantic Scholar" in biblio
        assert "pas les plus pertinents" in collecte

    def test_enrich_oai_sources(self):
        from engine.orchestrator import ENRICH_OAI_SOURCES
        text = _desc("moisson_search", "enrich_oai")
        for key in ENRICH_OAI_SOURCES:
            assert caps.CAPABILITIES[key].label in text, key
        for key, cap in caps.CAPABILITIES.items():
            if cap.proxy_of and key not in ENRICH_OAI_SOURCES:
                assert cap.label not in text, key

    def test_dedup_thresholds(self):
        from utils.deduplication import FUZZY_TITLE_THRESHOLD
        text = _desc("moisson_search", "dedup")
        assert f"{FUZZY_TITLE_THRESHOLD:.2f}".replace(".", ",") in text
        assert "jamais fusionner deux DOI différents" in text


# === Autres outils ===

class TestOtherTools:
    def test_fetch_by_doi_resolvers(self):
        desc = _desc("fetch_by_doi")
        for label in ("CrossRef", "HAL", "Isidore", "OpenAlex",
                      "Semantic Scholar", "CORE"):
            assert label in desc, label
        assert "'non_trouve'" in desc and "'ignore'" in desc
        from utils import lookups
        assert lookups.LOOKUP_SKIPPED == "ignore"
        assert lookups.LOOKUP_NOT_FOUND == "non_trouve"

    def test_corpus_filter_source_labels(self, hal_first_doc,
                                         isidore_first_item, crossref_work,
                                         openalex_work, s2_paper, core_work,
                                         tmp_path):
        """Les libellés annoncés sont ceux que les mappers écrivent
        réellement dans la ligne « Sources: », déduplication comprise, et le
        filtre (comparaison exacte) garde la notice de chaque source avec le
        libellé annoncé. Revue d'OPT-20 : les notices Scholar portent
        « Google Scholar (SerpAPI) » ; « Google Scholar » ne gardait rien."""
        import copy
        import json

        from connectors.isidore_scan import annotate_proxy_source
        from connectors.registry import CONNECTORS
        from mappers.core_mapper import map_core_item
        from mappers.crossref_mapper import map_crossref_item
        from mappers.google_scholar_mapper import map_scholar_item
        from mappers.hal_mapper import map_hal_to_zotero
        from mappers.isidore_mapper import map_isidore_to_zotero
        from mappers.openalex_mapper import map_openalex_item
        from mappers.semantic_scholar_mapper import map_s2_item
        from utils.deduplication import deduplicate_with_report

        isidore = map_isidore_to_zotero(isidore_first_item)
        items = [map_hal_to_zotero(hal_first_doc), isidore,
                 map_crossref_item(crossref_work),
                 map_openalex_item(openalex_work), map_s2_item(s2_paper),
                 map_core_item(core_work),
                 map_scholar_item({
                     "title": "Platform regulation in Europe",
                     "link": "https://example.org/a", "result_id": "r1",
                     "publication_info": {
                         "summary": "J Doe - Revue, 2021 - example.org"}})]
        for key, cap in caps.CAPABILITIES.items():
            if cap.proxy_of:
                proxy = copy.deepcopy(isidore)
                annotate_proxy_source([proxy], CONNECTORS[key].SOURCE_NAME)
                items.append(proxy)
        deduped = deduplicate_with_report(items)
        deduped = deduped[0] if isinstance(deduped, tuple) else deduped
        written = set()
        for item in deduped:
            for line in (item.get("extra") or "").split("\n"):
                if line.startswith("Sources:"):
                    written |= {s.strip() for s in
                                line[len("Sources:"):].split(",")}
        assert "Google Scholar (SerpAPI)" in written

        text = _desc("corpus_filter", "sources")
        listed = text.split("Libellés écrits par ce serveur : ")[1]
        listed = listed.split(". Un corpus ancien")[0]
        assert set(listed.split(", ")) == written

        corpus = tmp_path / "corpus_labels_20260929.json"
        corpus.write_text(json.dumps(deduped, ensure_ascii=False),
                          encoding="utf-8")
        for i, label in enumerate(sorted(written)):
            result = mcp_server._corpus_filter_sync({
                "corpus_path": str(corpus), "sources": [label],
                "output_suffix": f"_l{i}", "inline": "none"})
            assert isinstance(result, tuple), result
            assert result[1]["filtered_count"] >= 1, label

    def test_suffix_replacement_is_said(self):
        assert "remplace" in _desc("corpus_filter", "output_suffix")
        assert "remplace" in _desc("corpus_resolve_dois", "output_suffix")

    def test_doi_report_name(self):
        desc = _desc("corpus_resolve_dois")
        assert "<radical><output_suffix>.doi_report.json" in desc
        assert "'<corpus>.doi_report.json'" not in desc

    @pytest.mark.parametrize("name", [
        "corpus_analyze", "corpus_filter", "corpus_to_csl", "corpus_merge",
        "corpus_resolve_dois", "corpus_export", "corpus_query",
        "corpus_diff"])
    def test_zotero_input_required(self, name):
        desc = _desc(name)
        assert re.search(r"CSL-JSON", desc), name
        assert re.search(r"Refuse|pas sa version CSL-JSON", desc), name

    def test_search_by_author(self):
        desc = _desc("search_by_author")
        assert "production complète" not in desc
        for fragment in ("production_complete", "Sans nom", "orcid",
                         "rejected_not_author", "'ambiguous'",
                         "profiles_to_confirm", "report_compact"):
            assert fragment in desc, fragment
        save = _desc("search_by_author", "save_corpus")
        assert "<uuid8>" in save and ".manifest.json" in save

    def test_corpus_diff_points_to_corpus_query(self):
        assert "corpus_query" in _desc("corpus_diff")


# === Docstring de tête du connecteur Google Scholar (C34) ===

def _config_default(env_name: str) -> int:
    """Défaut littéral d'une constante de ``config.py``
    (``os.environ.get("<env_name>", "<défaut>")``), indépendant d'une
    surcharge par l'environnement du shell ou de la CI."""
    source = (PROJECT_ROOT / "config.py").read_text(encoding="utf-8")
    match = re.search(r'os\.environ\.get\(\s*"' + re.escape(env_name)
                      + r'",\s*"([^"]+)"\s*\)', source)
    assert match, env_name
    return int(match.group(1))


class TestScholarDocstring:
    def _doc(self):
        import connectors.google_scholar as gs
        return gs.__doc__

    def test_credits_and_page_cap(self):
        """La docstring (statique) donne les défauts de ``config.py`` ; elle
        est comparée aux défauts littéraux, non aux valeurs effectives, que
        MOISSON_SERPAPI_* peut surcharger (revue d'OPT-20)."""
        doc = self._doc()
        pages = _config_default("MOISSON_SERPAPI_MAX_PAGES")
        size = _config_default("MOISSON_SERPAPI_PAGE_SIZE")
        floor = _config_default("MOISSON_SERPAPI_MIN_CREDITS")
        assert "5 000 requêtes par mois" in doc
        assert "100 crédits par mois" not in doc
        assert f"défaut {pages}" in doc
        assert "défaut 5," not in doc
        assert f"{pages * size} notices pour {pages} crédits" in doc
        assert f"défaut {floor}" in doc

    def test_operator_claim_corrected(self):
        doc = self._doc()
        assert "ignorent" not in doc
        assert "seul CrossRef traite la requête en sac de mots" in doc
        assert "non vérifié en direct" in doc
        assert "'scholar'" in doc or "``scholar``" in doc
