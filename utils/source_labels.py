"""
Table unique des sources : clé de registre <-> libellé d'affichage.

Les libellés de source circulent sous plusieurs formes dans les notices : la
ligne ``Sources:`` du champ ``extra`` porte le libellé posé par le mapper
(« HAL », « CrossRef », « Google Scholar (SerpAPI) »), les recherches ciblées
écrivent parfois « Crossref », les anciens corpus « Persee » sans accent. Les
statistiques, la priorité de fusion et la matrice de recouvrement doivent
pourtant compter une seule source par fournisseur.

Ce module fixe la correspondance une fois pour toutes :

- :func:`canonical_source_key` ramène un libellé quelconque à la clé du
  registre (``connectors.registry``), sans tenir compte de la casse, des
  accents, de la ponctuation ni d'une mention entre parenthèses ;
- :func:`display_name` donne le libellé d'affichage d'une clé.

Un libellé inconnu n'est pas rejeté : il est ramené à une forme normalisée
stable (minuscules, sans accents, mots séparés par ``_``), de sorte que deux
graphies d'une même source hors table se regroupent quand même.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from unidecode import unidecode


# Clé de registre -> libellé d'affichage. L'ordre est celui du registre
# (sources francophones, puis internationales, puis Scholar).
SOURCE_DISPLAY_NAMES: Dict[str, str] = {
    "hal": "HAL",
    "isidore": "Isidore",
    "openedition": "OpenEdition",
    "cairn": "Cairn",
    "persee": "Persée",
    "theses_fr": "Theses.fr",
    "erudit": "Érudit",
    "crossref": "CrossRef",
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "core": "CORE",
    "google_scholar": "Google Scholar",
}

# Variantes connues, sous leur forme compacte (minuscules, sans accents, sans
# séparateurs ni mention entre parenthèses) -> clé de registre. Les clés et
# libellés de la table principale y sont ajoutés automatiquement.
_ALIASES: Dict[str, str] = {
    "googlescholar": "google_scholar",
    "scholar": "google_scholar",
    "serpapi": "google_scholar",
    "googlescholarserpapi": "google_scholar",
    "semanticscholar": "semantic_scholar",
    "s2": "semantic_scholar",
    "thesesfr": "theses_fr",
    "theses": "theses_fr",
    "these": "theses_fr",
    "thesefr": "theses_fr",
    "openeditionjournals": "openedition",
    "revuesorg": "openedition",
    "cairninfo": "cairn",
    "hal": "hal",
    "halshs": "hal",
    "archivesouvertes": "hal",
    "isidorescience": "isidore",
    "coreacuk": "core",
}

_PARENTHESES = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _compact(label: str) -> str:
    """Forme compacte d'un libellé : sans accents, casse, ponctuation, espaces."""
    base = _PARENTHESES.sub(" ", unidecode(str(label or ""))).lower()
    return _NON_ALNUM.sub("", base)


def _slug(label: str) -> str:
    """Forme normalisée d'un libellé inconnu : mots séparés par ``_``."""
    base = _PARENTHESES.sub(" ", unidecode(str(label or ""))).lower()
    return _NON_ALNUM.sub("_", base).strip("_")


def _build_index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for key, label in SOURCE_DISPLAY_NAMES.items():
        index[_compact(key)] = key
        index[_compact(label)] = key
    index.update(_ALIASES)
    return index


_INDEX = _build_index()


def canonical_source_key(label: Optional[str]) -> str:
    """
    Clé de registre d'un libellé de source.

    Tolère la casse (« Crossref »), les accents (« Persee »), les mentions
    entre parenthèses (« Google Scholar (SerpAPI) ») et les séparateurs
    (« Semantic Scholar », « semantic_scholar », « Theses.fr »).

    Returns:
        La clé du registre si le libellé est connu ; sinon une forme
        normalisée stable du libellé (``"Foo Bar"`` -> ``"foo_bar"``) ; chaîne
        vide pour un libellé vide.
    """
    if not label or not str(label).strip():
        return ""
    key = _INDEX.get(_compact(label))
    if key:
        return key
    return _slug(label)


def is_known_source(label: Optional[str]) -> bool:
    """Vrai si le libellé désigne une source du registre."""
    return canonical_source_key(label) in SOURCE_DISPLAY_NAMES


def display_name(key_or_label: Optional[str]) -> str:
    """
    Libellé d'affichage d'une clé de registre (ou d'un libellé quelconque).

    Une clé ou un libellé inconnu est renvoyé tel quel (sans espaces de
    bordure) : l'affichage ne perd jamais une source hors table.
    """
    if not key_or_label:
        return ""
    key = canonical_source_key(key_or_label)
    if key in SOURCE_DISPLAY_NAMES:
        return SOURCE_DISPLAY_NAMES[key]
    return str(key_or_label).strip()
