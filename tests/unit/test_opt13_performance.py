"""
Tests OPT-13 — acquisition et classement mesurés séparément.

Corpus synthétiques de 1 000 et 6 000 notices finales (vraie déduplication,
notices présentes chez une à trois sources, résumés d'une centaine de mots,
rangs natifs par source) : la durée du classement est mesurée à part de
toute acquisition (``duration_s`` : rattachement, fusion native, classement
local, total). Bornes larges (machine chargée par d'autres processus) : le
but est de prouver que le classement reste négligeable devant l'acquisition
et la déduplication, pas de fixer un chiffre. Mesures de référence
consignées dans les notes d'OPT-13 (banc simulé, scénario grand_corpus :
5 864 notices, natif ≈ 0,26 s, local ≈ 0,8 s).
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from engine.models import SourceResult  # noqa: E402
from utils.deduplication import deduplicate_with_report  # noqa: E402
from utils.query_plan import build_query_plan  # noqa: E402
from utils.ranking import rank_records  # noqa: E402

WORDS = ("discours numérique réseaux médias politique écriture archives "
         "plateformes controverse presse institutions mémoire journalisme "
         "données corpus analyse sociologie histoire territoire image").split()

SORTS = {"hal": "score desc,docid asc", "crossref": "score:desc",
         "openalex": "relevance_score:desc", "isidore": "isidore_default",
         "semantic_scholar": "citationCount:desc", "core": None}
LABELS = {"hal": "HAL", "crossref": "CrossRef", "openalex": "OpenAlex",
          "isidore": "Isidore", "semantic_scholar": "Semantic Scholar",
          "core": "CORE"}

# Bornes (secondes) : très au-dessus des mesures (×10 au moins).
LIMITS = {1000: {"native": 3.0, "local": 6.0},
          6000: {"native": 10.0, "local": 20.0}}


def _corpus(n_records: int, seed: int = 13):
    rnd = random.Random(seed)
    per_source = {k: [] for k in SORTS}
    for w in range(n_records):
        title = " ".join(rnd.sample(WORDS, 5)) + f" n{w}"
        abstract = " ".join(rnd.choice(WORDS) for _ in range(110))
        doi = f"10.13/{w}" if w % 4 else None
        keys = rnd.sample(list(SORTS), rnd.choice((1, 1, 2, 3)))
        for key in keys:
            lines = [f"Sources: {LABELS[key]}", f"Cited by: {rnd.randint(0, 900)}"]
            if key == "isidore":
                lines.append(f"Isidore URI: https://isidore.science/document/10670/1.{w:07d}")
            item = {"itemType": "journalArticle", "title": title,
                    "creators": [{"creatorType": "author", "firstName": "Jean",
                                  "lastName": f"Auteur{w}"}],
                    "date": str(1990 + w % 35), "abstractNote": abstract,
                    "tags": [{"tag": t} for t in rnd.sample(WORDS, 3)],
                    "extra": "\n".join(lines)}
            if doi:
                item["DOI"] = doi
            else:
                item["url"] = f"https://example.org/{w}"
            per_source[key].append(item)
    sources = []
    for key, items in per_source.items():
        rnd.shuffle(items)
        result = SourceResult(source=key, display_name=LABELS[key], items=items)
        if SORTS[key]:
            result.native_sort = SORTS[key]
            result.native_ranks = [{"rank": i + 1, "score": None,
                                    "sort": SORTS[key]} for i in range(len(items))]
            result.bind_ranks()
        sources.append(result)
    flat = [it for s in sources for it in s.items]
    records, _ = deduplicate_with_report(flat)
    return records, sources


@pytest.mark.parametrize("n", [1000, pytest.param(6000, marks=pytest.mark.slow)])
def test_ranking_duration_is_measured_apart(n):
    records, sources = _corpus(n)
    assert len(records) == n          # un travail = une notice finale
    plan = build_query_plan("discours numérique", ["hal", "crossref"])
    for mode in ("native", "local"):
        started = time.perf_counter()
        result = rank_records(records, sources, mode=mode, plan=plan)
        wall = time.perf_counter() - started
        durations = result["duration_s"]
        assert set(durations) == {"mapping", "native", "local", "total"}
        assert durations["total"] <= wall + 0.01
        assert durations["total"] < LIMITS[n][mode], (mode, durations)
        if mode == "local":
            assert durations["local"] > 0
        else:
            assert durations["local"] == 0.0
        assert len(result["entries"]) == n
        assert result["mapping"]["unmapped"] == 0
        assert result["mapping"]["ambiguous"] == 0
        # Journal de mesure (visible avec -s) : consigné dans les notes.
        print(f"\n[OPT-13] {n} notices, {mode} : {durations}")
