"""
Séparation heuristique d'un nom complet en (prénom, nom).

OpenAlex, Semantic Scholar et CORE fournissent un nom complet (display name)
sans champ prénom/nom séparé. CrossRef fournit déjà ``given``/``family`` et
n'a donc pas besoin de cette fonction.

Deux formes sont reconnues :

- « Nom, Prénom » (une seule virgule), fréquente chez CORE, qui agrège des
  dépôts OAI (``dc:creator``), et présente chez OpenAlex pour une partie des
  auteurs : coupure sur la virgule, comme ``utils.cleaner.split_authors`` et
  le mapper Isidore. Une chaîne qui ressemble à une liste ou à une
  institution (point-virgule, esperluette, « and », chiffres, parenthèses,
  plusieurs virgules) garde l'heuristique générale ;
- « Prénom Nom » : l'heuristique est volontairement simple — le dernier token
  est traité comme le nom de famille. Les particules (de, van, von, del…) et
  les noms de famille composés ne sont pas traités parfaitement : une
  vérification manuelle reste nécessaire pour ces cas, ce qui est une
  approximation assumée et documentée.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Particules de nom courantes : si l'avant-dernier token est une particule, on
# le rattache au nom de famille (ex : "Pierre van der Berg" -> "van der Berg").
_PARTICLES = {
    "de", "del", "della", "der", "den", "di", "da", "do", "dos", "das",
    "van", "von", "vander", "ter", "ten", "le", "la", "les", "du",
    "el", "al", "bin", "ibn", "abu", "mac", "mc", "o'",
}

# Suffixes générationnels : « John Smith, Jr. » n'est pas « Nom, Prénom ».
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Indices d'une liste d'auteurs ou d'une institution saisie comme un nom :
# la forme « Nom, Prénom » n'est alors pas supposée.
_NOT_A_PERSON_RE = re.compile(r"[;&()\[\]\d]|\s(?:and|et)\s", re.IGNORECASE)


def _split_given_family(full: str) -> Tuple[str, str]:
    """Heuristique « Prénom Nom » : dernier token (et particules) = nom."""
    parts = full.split()
    if len(parts) == 1:
        # Nom mononymique ou organisation : on le place en nom de famille
        return "", parts[0]

    # Rattachement des particules au nom de famille
    last_index = len(parts) - 1
    while last_index > 0 and parts[last_index - 1].lower().strip(".") in _PARTICLES:
        last_index -= 1

    first = " ".join(parts[:last_index])
    last = " ".join(parts[last_index:])
    return first, last


def _split_inverted(full: str) -> Optional[Tuple[str, str]]:
    """
    Forme « Nom, Prénom » (une seule virgule) → ``(prénom, nom)``.

    ``None`` quand la chaîne n'a pas cette forme : aucune ou plusieurs
    virgules, indice de liste ou d'institution, partie vide. Un suffixe
    générationnel après la virgule (« John Smith, Jr. ») est rattaché au
    prénom, comme le fait Zotero (« John, Jr. »).
    """
    if full.count(",") != 1 or _NOT_A_PERSON_RE.search(full):
        return None
    last, _, first = full.partition(",")
    last, first = " ".join(last.split()), " ".join(first.split())
    if not last or not first:
        return None
    if first.lower().strip(".") in _SUFFIXES:
        given, family = _split_given_family(last)
        return (f"{given}, {first}" if given else first), family
    return first, last


def split_display_name(full: str) -> Tuple[str, str]:
    """
    Sépare ``full`` en ``(prénom, nom)``.

    « Nom, Prénom » (une seule virgule) est coupé sur la virgule. Sinon, le
    dernier token est le nom de famille ; les particules (de, van, etc.)
    immédiatement avant le dernier token y sont rattachées. Une virgule
    isolée en début ou en fin de chaîne est ignorée.

    Args:
        full: Nom complet (ex : "Patrick Charaudeau", "Pierre van der Berg",
            "Alayrac, Jean-Baptiste").

    Returns:
        Tuple ``(firstName, lastName)``. ``firstName`` peut être vide.
    """
    full = (full or "").strip().strip(",").strip()
    if not full:
        return "", ""

    inverted = _split_inverted(full)
    if inverted is not None:
        return inverted
    return _split_given_family(full)
