"""
Utilitaires de nettoyage et de normalisation des chaînes de caractères.

Les métadonnées issues des API SHS contiennent fréquemment du
HTML résiduel, des entités non échappées, des espaces multiples
et des variations typographiques. Ce module centralise les
opérations de nettoyage afin de produire des chaînes propres,
adaptées à l'import Zotero et à la déduplication par empreinte.
"""

import re
import html
import unicodedata
from typing import Optional, List


# Expression régulière pour détecter les balises HTML
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

# Expression régulière pour les espaces multiples (y compris insécables)
MULTIPLE_WS_PATTERN = re.compile(r"\s+")


def strip_html(text: Optional[str]) -> str:
    """
    Supprime les balises HTML et décode les entités.

    Args:
        text: Chaîne possiblement balisée

    Returns:
        Chaîne nettoyée, ou chaîne vide si l'entrée est None
    """
    if not text:
        return ""
    # Suppression des balises
    cleaned = HTML_TAG_PATTERN.sub(" ", text)
    # Décodage des entités HTML (&amp; → &, &eacute; → é, etc.)
    cleaned = html.unescape(cleaned)
    # Normalisation des espaces
    cleaned = MULTIPLE_WS_PATTERN.sub(" ", cleaned).strip()
    return cleaned


def clean_string(text: Optional[str]) -> str:
    """
    Nettoyage générique d'une chaîne :
    suppression HTML, normalisation des espaces, trim.

    Args:
        text: Chaîne d'entrée

    Returns:
        Chaîne nettoyée
    """
    if not text:
        return ""
    return strip_html(str(text))


def clean_list(items: Optional[List]) -> List[str]:
    """
    Nettoie une liste de chaînes en supprimant les éléments
    vides après normalisation.

    Args:
        items: Liste possible de chaînes

    Returns:
        Liste nettoyée sans éléments vides
    """
    if not items:
        return []
    if isinstance(items, str):
        items = [items]
    return [
        cleaned for cleaned in (clean_string(i) for i in items)
        if cleaned
    ]


def normalize_for_fingerprint(text: str) -> str:
    """
    Normalisation agressive pour la production d'empreintes
    de déduplication.

    Supprime les accents, met en minuscules, retire la
    ponctuation et les espaces. Le résultat n'est pas
    destiné à l'affichage mais à la comparaison.

    Args:
        text: Chaîne à normaliser

    Returns:
        Chaîne normalisée pour comparaison
    """
    if not text:
        return ""
    # Suppression du HTML résiduel
    text = strip_html(text)
    # Décomposition Unicode pour isoler les accents
    nfd = unicodedata.normalize("NFD", text)
    # Suppression des caractères de combinaison (accents)
    no_accents = "".join(
        c for c in nfd if unicodedata.category(c) != "Mn"
    )
    # Minuscules
    lower = no_accents.lower()
    # Suppression de tout ce qui n'est pas alphanumérique
    alphanum = re.sub(r"[^a-z0-9]", "", lower)
    return alphanum


def extract_year(date_str: Optional[str]) -> Optional[str]:
    """
    Extrait une année à 4 chiffres d'une chaîne de date.

    Gère les formats : YYYY, YYYY-MM, YYYY-MM-DD, ainsi
    que les dates partielles ou non standard.

    Args:
        date_str: Chaîne de date

    Returns:
        Année en 4 caractères, ou None si non détectable
    """
    if not date_str:
        return None
    match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", str(date_str))
    return match.group(1) if match else None


def split_authors(raw: Optional[str]) -> List[dict]:
    """
    Convertit une chaîne d'auteurs en liste de dicts Zotero.

    Gère le format 'Nom1, Prénom1; Nom2, Prénom2' fréquent
    en sortie Dublin Core, et le format simple 'Prénom Nom'.

    Args:
        raw: Chaîne d'auteurs concaténés

    Returns:
        Liste de dicts {creatorType, firstName, lastName} ou
        {creatorType, name} si le découpage n'est pas possible
    """
    if not raw:
        return []
    cleaned = clean_string(raw)
    creators = []
    # Sépare par point-virgule ou par 'and' ou par retour à la ligne
    parts = re.split(r"\s*;\s*|\s+and\s+|\s*\n\s*", cleaned)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Tentative de découpage Nom, Prénom
        if "," in part:
            last, _, first = part.partition(",")
            creators.append({
                "creatorType": "author",
                "firstName": first.strip(),
                "lastName": last.strip(),
            })
        else:
            # Découpage Prénom Nom : le dernier mot est le nom de famille
            tokens = part.split()
            if len(tokens) >= 2:
                creators.append({
                    "creatorType": "author",
                    "firstName": " ".join(tokens[:-1]),
                    "lastName": tokens[-1],
                })
            else:
                # Nom unique non décomposable : usage du champ 'name'
                creators.append({
                    "creatorType": "author",
                    "name": part,
                })
    return creators


def parse_hal_author(full_name: str) -> dict:
    """
    Convertit un nom HAL (format 'Prénom Nom') en dict Zotero.

    Args:
        full_name: Chaîne au format 'Prénom Nom'

    Returns:
        Dict creator Zotero
    """
    cleaned = clean_string(full_name)
    if not cleaned:
        return {}
    tokens = cleaned.split()
    if len(tokens) >= 2:
        return {
            "creatorType": "author",
            "firstName": " ".join(tokens[:-1]),
            "lastName": tokens[-1],
        }
    return {
        "creatorType": "author",
        "name": cleaned,
    }
