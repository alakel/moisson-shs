"""
Séparation heuristique d'un nom complet en (prénom, nom).

OpenAlex, Semantic Scholar et CORE fournissent un nom complet (display name)
sans champ prénom/nom séparé. CrossRef fournit déjà ``given``/``family`` et
n'a donc pas besoin de cette fonction.

L'heuristique est volontairement simple — le dernier token est traité comme
le nom de famille. Les particules (de, van, von, del…) et les noms de famille
composés ne sont pas traités parfaitement : une vérification manuelle reste
nécessaire pour ces cas, ce qui est une approximation assumée et documentée.
"""

from __future__ import annotations

from typing import Tuple

# Particules de nom courantes : si l'avant-dernier token est une particule, on
# le rattache au nom de famille (ex : "Pierre van der Berg" -> "van der Berg").
_PARTICLES = {
    "de", "del", "della", "der", "den", "di", "da", "do", "dos", "das",
    "van", "von", "vander", "ter", "ten", "le", "la", "les", "du",
    "el", "al", "bin", "ibn", "abu", "mac", "mc", "o'",
}


def split_display_name(full: str) -> Tuple[str, str]:
    """
    Sépare ``full`` en ``(prénom, nom)``.

    Hypothèse : le dernier token est le nom de famille. Les particules
    (de, van, etc.) immédiatement avant le dernier token sont rattachées
    au nom de famille.

    Args:
        full: Nom complet (ex : "Patrick Charaudeau", "Pierre van der Berg").

    Returns:
        Tuple ``(firstName, lastName)``. ``firstName`` peut être vide.
    """
    full = (full or "").strip()
    if not full:
        return "", ""

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
