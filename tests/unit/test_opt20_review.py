"""
Tests OPT-20 — non-régression des constats de revue (descriptions et prompts).

Hors réseau, sans socket. Chaque classe ancre un texte lu par l'agent sur le
comportement du code qu'il décrit :

- plan de requête : en digest (défaut en local), ``query_plan`` est résumé
  et ne porte ni l'ordre ni les filtres par source ; seuls le plan complet
  (``plan_only=true``, ``inline='full'``, mode distant) et le manifeste les
  donnent. Les descriptions et les prompts ne renvoient plus au plan résumé
  pour l'ordre ;
- filtre de langue : ``corpus_query(filters={"languages": …})`` écarte les
  notices sans langue (CrossRef, Semantic Scholar et Google Scholar n'en
  portent aucune) ; le prompt ``etat-de-l-art`` cherche les sous-thèmes sans
  ce filtre et classe les notices d'après leur champ ``language`` ;
- fenêtre d'Isidore désactivée (``ISIDORE_RESULT_WINDOW = 0``) : aucun texte
  n'annonce « les 0 premiers résultats ».
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
from connectors import capabilities as caps  # noqa: E402

PROMPT_ARGS = {
    "bibliographie-thematique": {"theme": "discours numérique"},
    "profil-chercheur": {"nom": "Patrick Charaudeau"},
    "veille-mensuelle": {"theme": "humanités numériques",
                         "from_date": "2026-09-01", "to_date": "2026-09-30"},
    "etat-de-l-art": {"question": "régulation des plateformes numériques",
                      "question_en": "platform regulation"},
}
HARVEST_PROMPTS = ("bibliographie-thematique", "veille-mensuelle",
                   "etat-de-l-art")


def _tools():
    return {t.name: t for t in asyncio.run(mcp_server.list_tools())}


def _desc(name, prop=None):
    tool = _tools()[name]
    if prop is None:
        return tool.description
    return tool.inputSchema["properties"][prop]["description"]


def _all_tool_texts():
    for tool in _tools().values():
        yield tool.name, tool.description
        for prop, spec in tool.inputSchema["properties"].items():
            yield f"{tool.name}.{prop}", spec.get("description", "")
            for sub, subspec in (spec.get("properties") or {}).items():
                yield f"{tool.name}.{prop}.{sub}", subspec.get(
                    "description", "")


def _prompt(name, args):
    result = asyncio.run(mcp_server.get_prompt(name, args))
    return result.messages[0].content.text


def _plan():
    from utils.query_plan import build_query_plan
    return build_query_plan("platform regulation",
                            sources=["hal", "crossref", "semantic_scholar"],
                            mode="bibliographie")


def _labels_without_language():
    return [c.label for c in caps.CAPABILITIES.values()
            if not c.language_in_records]


# === Plan de requête : ordre par source absent du plan résumé ===

class TestDigestPlanOrdering:
    def test_digest_plan_has_no_ordering_and_the_texts_say_so(self):
        """Le plan résumé du digest n'a pas l'ordre par source : les
        descriptions de moisson_search, mode et inline le disent et
        renvoient au plan complet."""
        plan = _plan().as_dict()
        assert all(e.get("ordering") for e in plan["sources"])
        compact = mcp_server._compact_query_plan(plan, "x.manifest.json")
        assert compact["compact"] is True
        assert not any("ordering" in e for e in compact["sources"])
        assert not any("native_filters" in e or "local_filters" in e
                       for e in compact["sources"])

        mode = _desc("moisson_search", "mode")
        assert "pas dans le plan résumé du digest" in mode
        assert "plan_only=true" in mode and "inline='full'" in mode
        desc = _desc("moisson_search")
        assert "sans l'ordre ni les filtres par source" in desc
        assert "plan_only=true" in desc
        inline = _desc("moisson_search", "inline")
        assert "sans l'ordre ni les filtres par source" in inline
        assert "ordre appliqué" in _desc("moisson_search", "plan_only")

    def test_no_text_points_to_the_digest_plan_for_the_ordering(self):
        old = ("L'ordre appliqué par source figure dans "
               "structuredContent.query_plan.")
        for where, text in _all_tool_texts():
            assert old not in text, where
        for name in HARVEST_PROMPTS:
            text = _prompt(name, PROMPT_ARGS[name])
            assert "chaque source (structuredContent.query_plan)" not in text

    def test_plan_only_returns_the_full_plan_with_ordering(self):
        content, structured = mcp_server._plan_only_result(_plan())
        entries = structured["query_plan"]["sources"]
        assert entries and all(e.get("ordering") for e in entries)
        assert "compact" not in structured["query_plan"]

    @pytest.mark.parametrize("name", HARVEST_PROMPTS)
    def test_harvest_prompts_read_the_ordering_in_the_full_plan(self, name):
        text = _prompt(name, PROMPT_ARGS[name])
        selection = text[text.index("- Sélection :"):]
        selection = selection[:selection.index("\n")]
        assert "ordering" in selection and "plan_only=true" in selection
        assert "plan résumé" in selection

    def test_etat_de_l_art_notes_the_ordering_of_the_plan_only_call(self):
        text = _prompt("etat-de-l-art", PROMPT_ARGS["etat-de-l-art"])
        step2 = text[text.index("2. "):text.index("3. ")]
        assert "plan_only=true" in step2 and "ordering" in step2


# === Filtre de langue : notices sans langue écartées ===

def _write_corpus(path: Path, items):
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _notice(title, source, language="", doi=""):
    from mappers.zotero_schema import append_to_extra, make_empty_item
    item = make_empty_item("journalArticle")
    item["title"] = title
    item["date"] = "2023"
    if language:
        item["language"] = language
    if doi:
        item["DOI"] = doi
    append_to_extra(item, "Sources", source)
    return item


class TestLanguageFilter:
    def test_registry_sources_without_language(self):
        """CrossRef et Semantic Scholar au moins ne portent pas de langue
        (registre des capacités, vérifié en direct le 2026-09-29)."""
        labels = _labels_without_language()
        assert "CrossRef" in labels and "Semantic Scholar" in labels

    def test_corpus_query_language_filter_keeps_notices_without_language(
            self, tmp_path):
        """Comportement décrit par le prompt et la description (PAT-21,
        décision DP-8) : une notice sans langue sort des deux passes fr et
        en ; keep_unknown_language=False l'écarte des deux."""
        from utils.corpus_query import query_corpus
        corpus = _write_corpus(tmp_path / "corpus_lang_20260929.json", [
            _notice("Regulating platforms", "CrossRef", doi="10.1/cr"),
            _notice("Platforms and citations", "Semantic Scholar",
                    doi="10.1/s2"),
            _notice("Régulation des plateformes", "HAL", language="fr",
                    doi="10.1/hal"),
            _notice("Platform governance", "OpenAlex", language="en",
                    doi="10.1/oa"),
        ])

        def titles(filters):
            result = query_corpus(corpus, query="platforms OR plateformes "
                                  "OR platform", filters=filters,
                                  fields=["id", "title", "language"])
            return {row["title"] for row in result.items}

        everything = titles(None)
        in_fr = titles({"languages": ["fr"]})
        in_en = titles({"languages": ["en"]})
        unknown = {"Regulating platforms", "Platforms and citations"}
        assert unknown <= everything
        assert unknown <= in_fr and unknown <= in_en
        assert "Platform governance" not in in_fr
        strict = titles({"languages": ["fr", "en"],
                         "keep_unknown_language": False})
        assert not unknown & strict
        assert strict == {"Régulation des plateformes", "Platform governance"}

    def test_etat_de_l_art_searches_subthemes_without_language_filter(self):
        text = _prompt("etat-de-l-art", PROMPT_ARGS["etat-de-l-art"])
        step5 = text[text.index("5. "):text.index("6. ")]
        assert 'filters={"languages"' not in step5
        assert "SANS filtre de langue" in step5
        assert '"language"' in step5 and "fields=[" in step5
        # Tout usage du filtre de langue porte la réserve.
        if "languages" in step5:
            assert "garde dans chaque passe les notices sans langue" in step5
        for label in _labels_without_language():
            assert label in step5, label
        assert "langue non indiquée" in step5
        step6 = text[text.index("6. "):]
        assert "langue non indiquée" in step6

    def test_etat_de_l_art_fields_exist(self):
        """Les champs demandés à corpus_query sont des champs projetables."""
        import re

        from utils import corpus_query as CQ
        text = _prompt("etat-de-l-art", PROMPT_ARGS["etat-de-l-art"])
        fields = re.search(r"fields=\[([^\]]*)\]", text).group(1)
        names = re.findall(r'"([a-z_]+)"', fields)
        assert "language" in names and set(names) <= set(CQ.FIELDS)

    def test_corpus_query_filters_description(self):
        text = _tools()["corpus_query"].inputSchema["properties"]["filters"][
            "description"]
        assert "les notices sans langue lisible" in text
        assert "sont gardées" in text
        assert "keep_unknown_language: false les écarte" in text
        assert "notices sans année" in text
        for label in _labels_without_language():
            assert label in text, label


# === Fenêtre d'Isidore désactivée ===

class TestIsidoreWindowDisabled:
    def _texts(self):
        texts = [text for _, text in _all_tool_texts()]
        texts += [_prompt(name, args) for name, args in PROMPT_ARGS.items()]
        return "\n".join(texts)

    def test_default_window_is_stated(self):
        text = self._texts()
        assert f"{config.ISIDORE_RESULT_WINDOW} premiers résultats" in text
        assert config.ISIDORE_RESULT_WINDOW > 0

    def test_disabled_window_is_not_announced_as_zero(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "ISIDORE_RESULT_WINDOW", 0)
        text = self._texts()
        assert "0 premiers résultats" not in text
        assert "fenêtre de 0 " not in text
        assert "levée" in text
        assert f"{config.ISIDORE_SCAN_MAX_RECORDS} notices par requête" in text
        # Les quatre prompts gardent la distinction limite / panne.
        for name, args in PROMPT_ARGS.items():
            prompt = _prompt(name, args)
            assert "pas une panne" in prompt and "0 premiers" not in prompt
        assert "0 premiers" not in _desc("moisson_search")
        assert "fenêtre de 0" not in _desc("moisson_search", "max_per_source")
