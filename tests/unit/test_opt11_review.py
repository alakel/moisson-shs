"""
OPT-11, revue : non-régression des constats corrigés.

- champ non pris en charge autour d'un groupe ou d'une exclusion (parenthèses
  perdues, exclusion inversée, date CORE mal appliquée, « title_t:NOT x ») ;
- précédence : exclusion rattachée à toute l'alternative, lectures ambiguës
  signalées avec la lecture retenue ;
- syntaxe Solr de HAL (plages, proximité, échappements) conservée pour HAL et
  adaptée ailleurs, avec avertissement ;
- branche d'alternative faite d'exclusions seules (« a OR (-b -c) ») ;
- requête entière entre « » signalée ;
- plan de requête joint au retour MCP d'une moisson annulée ou à échéance ;
- journal daté des sondes versionné (docs/API_SPECS.md).

Aucun accès réseau.
"""

import asyncio
import json
import random
import re
from pathlib import Path

import pytest

import mcp_server
from connectors import capabilities as caps_module
from connectors.registry import resolve_sources
from engine import orchestrator
from engine.models import HarvestResult, SourceResult
from utils import query_plan as Q
from utils.query_plan import QueryPlanError, build_query_plan, parse_query

ROOT = Path(__file__).resolve().parents[2]
EVERY = resolve_sources(["all", "google_scholar"])
ISIDORE_FAMILY = ["isidore", "openedition", "cairn", "persee", "theses_fr",
                  "erudit"]


def sent(plan, key):
    return plan.for_source(key).query_sent


def plan_of(query, sources=EVERY, **kw):
    return build_query_plan(query, sources, **kw)


def precedence_warnings(plan):
    return [w for w in plan.warnings if "précédence" in w]


# === Champ autour d'un groupe ou d'une exclusion ===

class TestFieldAroundGroup:
    def test_excluded_group_keeps_its_parentheses_everywhere(self):
        """Avant : Isidore « discours SAUF twitter OU facebook », OpenAlex
        « discours NOT twitter OR facebook » : « facebook » devenait une
        alternative positive (l'inverse de l'intention)."""
        plan = plan_of("discours -title_t:(twitter OR facebook)")
        assert sent(plan, "hal") == "discours NOT title_t:(twitter OR facebook)"
        for key in ISIDORE_FAMILY:
            assert sent(plan, key) == "discours SAUF (twitter OU facebook)"
        assert sent(plan, "openalex") == "discours NOT (twitter OR facebook)"
        assert sent(plan, "semantic_scholar") == "discours -(twitter | facebook)"
        assert sent(plan, "google_scholar") == "discours -(twitter OR facebook)"
        assert sent(plan, "crossref") == "discours"

    def test_positive_group_under_unsupported_field_keeps_parentheses(self):
        plan = plan_of("title_t:(numérique OR digital) discours")
        assert sent(plan, "isidore") == "(numérique OU digital) discours"
        assert sent(plan, "openalex") == "(numérique OR digital) discours"
        assert sent(plan, "semantic_scholar") == "(numérique | digital) discours"

    def test_core_wraps_a_top_level_or_hidden_in_a_field(self):
        """Le connecteur CORE ajoute « AND yearPublished>=… » : sans
        parenthèses, la date ne portait que sur la dernière branche."""
        plan = plan_of("title_t:(numérique OR digital)", from_date="2020")
        assert sent(plan, "core") == "(numérique OR digital)"

    def test_or_not_rule_sees_through_a_field(self):
        plan = plan_of("discours OR title_t:(-politique)")
        assert {e.query_sent for e in plan.sources} == {"discours"}
        assert any("OR NOT" in w for w in plan.warnings)

    def test_hal_never_receives_field_colon_not(self):
        """« title_t:NOT x » n'est pas une requête Lucene valide."""
        assert sent(plan_of("discours title_t:(-politique)"), "hal") == \
            "discours NOT title_t:politique"
        assert sent(plan_of("discours title_t:(-a -b)"), "hal") == \
            "discours NOT title_t:a NOT title_t:b"
        for q in ["discours title_t:(-politique)", "discours title_t:(NOT a)",
                  "a OR b title_t:(-c)"]:
            assert ":NOT" not in sent(plan_of(q), "hal"), q

    def test_nested_field_keeps_the_innermost(self):
        assert sent(plan_of('d title_t:(abstract_t:"p q")'), "hal") == \
            'd abstract_t:"p q"'

    def test_operator_valued_field_is_not_sent_as_a_bare_operator(self):
        plan = plan_of("discours title_t:OR")
        assert sent(plan, "openalex") == 'discours "OR"'
        assert sent(plan, "isidore") == 'discours "OR"'

    def test_render_then_reparse_is_faithful(self):
        """Aller-retour rendu → ré-analyse : même arbre (champs ramenés à la
        valeur). Avant la correction : 233 divergences sur 30 000 requêtes à
        champ (revue) ; 3 000 requêtes ici."""
        toks = ["a", "b", "c", "d", '"p q"', "(", ")", "AND", "OR", "NOT",
                "-a", "-b", "title_t:a", "title_t:(", 'title_t:"p q"']
        rng = random.Random(7)
        mismatches = []
        for _ in range(3000):
            q = " ".join(rng.choice(toks) for _ in range(rng.randint(2, 9)))
            try:
                plan = build_query_plan(q, ["openalex", "hal"])
            except QueryPlanError:
                continue
            for key in ("openalex", "hal"):
                back = parse_query(plan.for_source(key).query_sent)
                strip = key == "openalex"
                if (_norm(plan.parsed["original"].tree, strip)
                        != _norm(back.tree, strip)):
                    mismatches.append((key, q))
        assert mismatches == []


def _strip_fields(node):
    if isinstance(node, Q.FieldQuery):
        return _strip_fields(node.value)
    if isinstance(node, Q.Not):
        return Q.Not(_strip_fields(node.child))
    if isinstance(node, Q.And):
        return Q.And(tuple(_strip_fields(c) for c in node.children))
    if isinstance(node, Q.Or):
        return Q.Or(tuple(_strip_fields(c) for c in node.children))
    return node


def _norm(node, strip):
    node = Q._simplify(_strip_fields(node) if strip else node)

    def key(n):
        if isinstance(n, Q.Term):
            return ("T", n.text)
        if isinstance(n, Q.Phrase):
            return ("P", n.text)
        if isinstance(n, Q.FieldQuery):
            return ("F", n.name, key(n.value))
        if isinstance(n, Q.Not):
            return ("N", key(n.child))
        kind = "A" if isinstance(n, Q.And) else "O"
        return (kind, tuple(sorted(repr(key(c)) for c in n.children)))
    return key(node)


# === Précédence ===

class TestPrecedence:
    @pytest.mark.parametrize("query", ["twitter OR facebook NOT publicité",
                                       "twitter OR facebook -publicité"])
    def test_exclusion_after_an_alternative_applies_to_all_of_it(self, query):
        """Lecture de Lucene (HAL avant OPT-11) et de Google ; avant la
        correction, « twitter OR (facebook NOT publicité) » sans
        avertissement."""
        plan = plan_of(query)
        assert sent(plan, "hal") == "(twitter OR facebook) NOT publicité"
        assert sent(plan, "isidore") == "(twitter OU facebook) SAUF publicité"
        assert sent(plan, "semantic_scholar") == "(twitter | facebook) -publicité"
        assert sent(plan, "google_scholar") == "(twitter OR facebook) -publicité"
        assert sent(plan, "core") == "(twitter OR facebook)"
        assert "publicité" not in sent(plan, "crossref")
        warns = precedence_warnings(plan)
        assert len(warns) == 1
        assert "« (twitter OR facebook) NOT publicité »" in warns[0]
        assert "toute l'alternative" in warns[0]

    def test_parenthesised_exclusion_stays_local_without_warning(self):
        plan = plan_of("twitter OR (facebook NOT publicité)")
        assert sent(plan, "hal") == "twitter OR (facebook NOT publicité)"
        assert precedence_warnings(plan) == []

    def test_juxtaposition_mixed_with_or_is_reported(self):
        plan = plan_of("discours numérique OR digital")
        assert sent(plan, "hal") == "(discours numérique) OR digital"
        warns = precedence_warnings(plan)
        assert len(warns) == 1
        assert "« (discours numérique) OR digital »" in warns[0]
        assert "AND prioritaire sur OR" in warns[0]

    def test_explicit_and_mixed_with_or_is_reported(self):
        assert precedence_warnings(plan_of("a AND b OR c"))

    def test_both_ambiguities_give_one_warning(self):
        plan = plan_of("a b -c OR d")
        assert sent(plan, "hal") == "((a b) OR d) NOT c"
        assert len(precedence_warnings(plan)) == 1

    @pytest.mark.parametrize("query", [
        "(discours numérique) OR digital",
        "discours (numérique OR digital)",
        '"discours numérique" AND (twitter OR facebook) NOT publicité',
        "discours OR récit OR histoire",
        "discours numérique -politique",
    ])
    def test_unambiguous_queries_have_no_precedence_warning(self, query):
        assert precedence_warnings(plan_of(query)) == []

    def test_precedence_is_documented_in_the_tool_schema(self):
        tools = asyncio.run(mcp_server.list_tools())
        schema = next(t for t in tools if t.name == "moisson_search")
        desc = schema.inputSchema["properties"]["query"]["description"]
        assert "Précédence" in desc and "(a OR b) -c" in desc
        assert "sans guillemets autour du thème" in desc


# === Branche d'alternative faite d'exclusions seules ===

class TestNegativeOnlyBranch:
    @pytest.mark.parametrize("query", [
        "discours OR (NOT numérique AND NOT politique)",
        "discours OR (-numérique -politique)",
        "(-numérique -politique) OR discours",
    ])
    def test_dropped_with_a_warning_everywhere(self, query):
        """Avant : Isidore « discours OU (SAUF numérique SAUF politique) »,
        forme sans équivalent valide, sans avertissement."""
        plan = plan_of(query)
        assert {e.query_sent for e in plan.sources} == {"discours"}
        assert any("OR NOT" in w for w in plan.warnings)

    def test_branch_emptied_for_one_source_is_dropped_there(self):
        # La plage n'existe que pour HAL : ailleurs, la branche ne garde que
        # l'exclusion et ne doit pas partir en « OU SAUF x ».
        plan = plan_of("a OR (producedDateY_i:[2020 TO 2024] -x)")
        assert sent(plan, "hal") == \
            "a OR (producedDateY_i:[2020 TO 2024] NOT x)"
        assert sent(plan, "isidore") == "a"
        entry = plan.for_source("isidore")
        assert any("OR NOT" in w for w in entry.warnings)
        assert any("plage" in w for w in entry.warnings)


# === Syntaxe Solr de HAL ===

class TestSolrSyntax:
    @pytest.mark.parametrize("query", [
        # Exemples de docs/FAQ.md et du README (syntaxe HAL documentée).
        '"discours numérique"~5',
        "producedDateY_i:[2020 TO 2024]",
        "producedDateY_i:[2020 TO 2024] AND discours",
        '"discours numérique" AND producedDateY_i:[2020 TO 2024]',
        "discours AND producedDateY_i:[2018 TO *]",
        "producedDate_tdate:{2018-01-01T00:00:00Z TO *] discours",
        '"discours numérique"~3 AND twitter',
        '"discours numérique"^2 twitter',
        "doiId_s:10.3917\\/rfsic.001 AND discours",
        '"discours" AND C\\+\\+',
        "title_t:C\\+\\+ discours",
        'authFullName_s:"Patrick Charaudeau"',
        'docType_s:ART AND "discours numérique"',
        '(IA OR "intelligence artificielle") AND société',
    ])
    def test_hal_receives_documented_solr_syntax_verbatim(self, query):
        """Avant : « [2020 AND TO AND 2024] », « "…" AND ~3 », antislash
        doublé (terme littéral, aucune correspondance)."""
        entry = plan_of(query, ["hal"]).for_source("hal")
        assert entry.query_sent == query
        assert entry.warnings == []

    def test_lone_backslash_is_still_escaped_for_hal(self):
        assert sent(plan_of("a\\b OR c", ["hal"]), "hal") == "a\\\\b OR c"

    def test_range_is_removed_elsewhere_with_a_warning(self):
        plan = plan_of("producedDateY_i:[2020 TO 2024] AND discours")
        for key in ["isidore", "openalex", "semantic_scholar", "core",
                    "google_scholar", "crossref"]:
            entry = plan.for_source(key)
            assert entry.query_sent == "discours", key
            assert any("producedDateY_i:[2020 TO 2024]" in w
                       for w in entry.warnings), key
            assert "TO" not in entry.query_sent.split()

    def test_proximity_becomes_words_together_elsewhere(self):
        plan = plan_of('"discours numérique"~3 OR twitter')
        assert sent(plan, "isidore") == "(discours numérique) OU twitter"
        assert sent(plan, "openalex") == "(discours numérique) OR twitter"
        assert sent(plan, "core") == "((discours numérique) OR twitter)"
        entry = plan.for_source("openalex")
        assert any("proximité" in w and "~3" in w for w in entry.warnings)
        for e in plan.sources:
            assert "~3" not in e.query_sent or e.source == "hal"

    def test_boost_is_dropped_elsewhere_with_a_note(self):
        entry = plan_of('"discours numérique"^2 twitter').for_source("isidore")
        assert entry.query_sent == '"discours numérique" twitter'
        assert any("^2" in n for n in entry.notes)

    def test_user_escapes_are_removed_outside_hal(self):
        plan = plan_of("title_t:C\\+\\+ discours")
        assert sent(plan, "openalex") == "C++ discours"
        assert sent(plan, "crossref") == "C++ discours"

    def test_range_only_query(self):
        # HAL seul : transmise telle quelle.
        assert sent(plan_of("producedDateY_i:[2020 TO 2024]", ["hal"]),
                    "hal") == "producedDateY_i:[2020 TO 2024]"
        # Une source qui ne prend pas la plage n'aurait plus rien à chercher.
        with pytest.raises(QueryPlanError, match="Isidore.*plage"):
            plan_of("producedDateY_i:[2020 TO 2024]", ["hal", "isidore"])
        with pytest.raises(QueryPlanError, match="CrossRef"):
            plan_of("producedDateY_i:[2020 TO 2024]", ["crossref"])

    def test_unclosed_range_is_reported(self):
        plan = plan_of("producedDateY_i:[2020 TO discours", ["hal"])
        assert any("non fermée" in w for w in plan.warnings)

    def test_group_suffix_is_reported(self):
        plan = plan_of("(a b)^2 c", ["hal"])
        assert sent(plan, "hal") == "a b c"
        assert any("^2" in w for w in plan.warnings)

    def test_simple_queries_still_sent_unchanged(self):
        for q in ["discours^2 numérique", "numéri~1 discours", "a\\b c"]:
            plan = plan_of(q)
            assert {e.query_sent for e in plan.sources} == {q}


# === Requête entière entre « » ===

class TestWholeQueryInGuillemets:
    def test_whole_query_in_guillemets_is_reported(self):
        plan = plan_of("« discours numérique »")
        assert sent(plan, "hal") == '"discours numérique"'
        assert any("requête entière entre guillemets" in w
                   for w in plan.warnings)

    @pytest.mark.parametrize("query", [
        '"discours numérique"',           # guillemets droits : voulu
        "« discours numérique » twitter",
        "« discours »",
    ])
    def test_no_warning_otherwise(self, query):
        assert not any("requête entière" in w for w in plan_of(query).warnings)


# === Imbrication ===

def test_deep_nesting_is_a_clear_error():
    """Avant : RecursionError (erreur interne) ; désormais un refus clair."""
    for q in ["(" * 300 + "a" + ")" * 300, "NOT " * 1200 + "a b"]:
        with pytest.raises(QueryPlanError, match="trop imbriquée"):
            build_query_plan(q, ["hal"])
    # Imbrication d'usage courant : acceptée.
    assert sent(build_query_plan("(" * 10 + "a OR b" + ")" * 10 + " c",
                                 ["hal"]), "hal") == "(a OR b) c"


# === Plan de requête après une annulation (MCP) ===

def _cancelling_run_source(reason):
    def runner(name, cls, *a, source_key=None, **k):
        return SourceResult(source=source_key, display_name=name,
                            status="cancelled", reason=reason)
    return runner


class TestCancelledHarvestKeepsThePlan:
    ARGS = {"query": '"discours numérique" NOT politique',
            "sources": ["hal", "crossref"], "cache": False}

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "cache"))
        self.tmp = tmp_path

    @pytest.mark.parametrize("reason", ["deadline", "cancelled"])
    def test_stdio(self, monkeypatch, reason):
        """Avant : ToolError sans structuredContent, plan perdu.

        Cas ``deadline`` : échéance souple désactivée (``deadline_seconds``
        à 0). Depuis OPT-09 bis, une source arrêtée pour le motif
        ``deadline`` en stdio, sans que le signal de l'exécution soit levé,
        relève de l'échéance souple et n'annule plus la moisson : ce cas est
        couvert par ``test_stdio_soft_deadline_keeps_the_plan``.
        """
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(orchestrator, "run_source",
                            _cancelling_run_source(reason))
        args = dict(self.ARGS)
        if reason == "deadline":
            args["deadline_seconds"] = 0
        result = asyncio.run(mcp_server._tool_moisson_search(args))
        assert isinstance(result, mcp_server.ToolError)
        assert result.cancelled
        structured = result.structured
        plan = structured["query_plan"]
        by = {s["source"]: s for s in plan["sources"]}
        assert by["hal"]["query_sent"] == '"discours numérique" NOT politique'
        assert by["crossref"]["translation"] == "bag_of_words"
        assert structured["harvest_status"] == "cancelled"
        assert structured["item_count"] == 0
        assert "by_source_status" in structured
        assert not list(self.tmp.glob("corpus_*"))

    def test_stdio_soft_deadline_keeps_the_plan(self, monkeypatch):
        """Échéance souple par défaut (OPT-09 bis) : toutes les sources
        arrêtées pour le motif ``deadline`` donnent un échec (``isError``),
        pas une annulation, et le plan accompagne toujours l'erreur."""
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(orchestrator, "run_source",
                            _cancelling_run_source("deadline"))
        result = asyncio.run(mcp_server._tool_moisson_search(dict(self.ARGS)))
        assert isinstance(result, mcp_server.ToolError)
        assert not result.cancelled
        assert mcp_server._error_result(result).isError is True
        structured = result.structured
        by = {s["source"]: s for s in structured["query_plan"]["sources"]}
        assert by["hal"]["query_sent"] == '"discours numérique" NOT politique'
        assert by["crossref"]["translation"] == "bag_of_words"
        assert structured["harvest_status"] == "error"
        assert structured["item_count"] == 0
        assert {info["reason"] for info in
                structured["by_source_status"].values()} == {"deadline"}
        assert structured["soft_deadline"]["stopped_sources"] == [
            "HAL", "CrossRef"]
        assert not list(self.tmp.glob("corpus_*"))

    def test_http_without_disk_path(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(orchestrator, "run_source",
                            _cancelling_run_source("deadline"))
        result = asyncio.run(mcp_server._tool_moisson_search(dict(self.ARGS)))
        assert isinstance(result, mcp_server.ToolError) and result.cancelled
        call = mcp_server._error_result(result)
        assert call.isError is True
        assert call.structuredContent["query_plan"]["sources"][0]["source"] \
            == "hal"
        assert call.structuredContent["transport"] == "http"
        blob = json.dumps(call.structuredContent, ensure_ascii=False)
        assert str(self.tmp) not in blob

    def test_cancellation_after_harvest_carries_the_result(self, monkeypatch):
        """Annulation tombée entre la fin de la moisson et l'écriture : le
        résultat (statuts, plan appliqué) accompagne l'exception."""
        from utils.http import OperationCancelled

        def fake_harvest(request, ctx):
            plan = request.query_plan()
            ctx.cancel()
            return HarvestResult(
                run_id=ctx.run_id, request=request,
                sources=[SourceResult(source="hal", display_name="HAL")],
                items=[], total_before_dedup=0,
                query_plan=plan.as_dict())
        monkeypatch.setattr(mcp_server, "harvest", fake_harvest)
        import threading
        with pytest.raises(OperationCancelled) as exc:
            mcp_server._run_moisson_sync(
                "discours OR récit", 10, ["hal"], False, False, cache=False,
                cancel_event=threading.Event())
        assert exc.value.result.query_plan["sources"][0]["source"] == "hal"


# === Journal des sondes versionné ===

class TestProbeLogIsVersioned:
    DOC = (ROOT / "docs" / "API_SPECS.md").read_text(encoding="utf-8")

    def _probe_rows(self):
        return {int(m.group(1)): line for line in self.DOC.splitlines()
                for m in [re.match(r"^\| (\d+) \| \d\d:\d\d:\d\d \|", line)]
                if m}

    def test_annex_lists_the_65_dated_probes(self):
        rows = self._probe_rows()
        assert sorted(rows) == list(range(1, 66))
        assert "### Journal des sondes du 2026-09-29" in self.DOC

    def test_no_contact_address_in_the_annex(self):
        assert "@" not in "".join(self._probe_rows().values())

    def test_registry_points_to_the_versioned_log(self):
        source = (ROOT / "connectors" / "capabilities.py").read_text(
            encoding="utf-8")
        assert "scratchpad" not in source and "probes.jsonl" not in source
        assert "docs/API_SPECS.md" in caps_module.PROBE_LOG

    def test_every_cited_probe_number_exists_in_the_annex(self):
        rows = self._probe_rows()
        cited = set()
        for caps in caps_module.CAPABILITIES.values():
            for ev in caps.evidence:
                for m in re.finditer(r"n° (\d+)((?: et \d+| à \d+)?)", ev):
                    cited.add(int(m.group(1)))
                    for extra in re.findall(r"\d+", m.group(2)):
                        cited.add(int(extra))
        assert cited and cited <= set(rows)
