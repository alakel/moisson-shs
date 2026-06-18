"""
Fixtures pytest partagées par tous les tests.

Convention : un fichier `tests/fixtures/<nom>.<ext>` est chargé par une
fixture `<nom>` retournant le contenu parsé.
"""

import json
import sys
from pathlib import Path

import pytest

# Permet d'importer le code source depuis n'importe quel test
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


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
