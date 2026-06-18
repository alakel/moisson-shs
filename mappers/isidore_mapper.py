"""
Mapper Isidore JSON → Zotero JSON.

L'API Isidore renvoie en 2026 une structure différente de l'historique :
les champs métier sont imbriqués sous une clé `isidore` au sein de chaque
record, et la plupart des champs sont multilingues (listes d'objets avec
@xml:lang et $).

Structure typique d'un item Isidore :
{
    "@uri": "20.500.13089/hzcj",
    "isidore": {
        "title": [
            {"@xml:lang": "fr", "$": "...", "@added": "true"},
            {"@xml:lang": "en", "$": "...", "@added": "true"},
            "..."   # peut aussi contenir des chaînes brutes
        ],
        "url": "https://doi.org/10.4000/lidil.6033",
        "enrichedCreators": {
            "creator": {   # ou liste
                "firstname": "...",
                "lastname": "...",
                "@normalizedAuthor": "..."
            }
        },
        "date": {
            "@startDate": "2019-01-01",
            "normalizedDate": "2019-01-01",
            "year": "2019"
        },
        "abstract": [
            {"@xml:lang": "fr", "$": "..."},
            ...
        ],
        "subject": [   # mots-clés enrichis ou natifs
            {"@xml:lang": "fr", "$": "..."},
            {"@origin": "rameau", "$": "..."},
        ],
        "language": "fr",
        "source": {"@handle": "...", "name": [{"@xml:lang": "fr", "$": "Lidil"}]},
        "ore": {
            "aggregates": [
                {"@crawl": "true", "$": "https://doi.org/10.4000/lidil.6033"},
                {"$": "https://journals.openedition.org/lidil/6033"}
            ]
        },
        "types": {"type": ["info:eu-repo/semantics/article", "Journal articles"]}
    }
}
"""

from typing import Dict, Any, List, Optional

from mappers.zotero_schema import (
    make_empty_item, validate_and_clean, make_tags, append_to_extra,
)
from mappers.doc_type_mapping import map_dublin_core_types_list
from utils.cleaner import clean_string, clean_list


def _pick_multilang(field: Any, prefer_lang: str = "fr") -> str:
    """
    Extrait une valeur textuelle d'un champ Isidore multilingue.

    Le format est typiquement [{"@xml:lang": "fr", "$": "..."}, ...] ou
    parfois une chaîne brute, ou un dict unique.

    Args:
        field: Champ Isidore à extraire
        prefer_lang: Code de langue privilégié

    Returns:
        Première valeur non vide dans la langue préférée, sinon
        première valeur disponible, sinon chaîne vide.
    """
    if field is None:
        return ""
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        if "$" in field:
            return str(field["$"])
        # Récursion sur valeurs
        for v in field.values():
            r = _pick_multilang(v, prefer_lang)
            if r:
                return r
        return ""
    if isinstance(field, list):
        # Priorité à la langue préférée
        for item in field:
            if (
                isinstance(item, dict)
                and item.get("@xml:lang") == prefer_lang
                and item.get("$")
            ):
                return str(item["$"])
        # Sinon première valeur dispo
        for item in field:
            if isinstance(item, str) and item.strip():
                return item
            if isinstance(item, dict) and item.get("$"):
                return str(item["$"])
        return ""
    return str(field)


def _all_multilang(field: Any) -> List[str]:
    """
    Extrait toutes les variantes textuelles d'un champ multilingue Isidore.

    Args:
        field: Champ Isidore

    Returns:
        Liste de chaînes (chaque langue représentée 1 fois)
    """
    if field is None:
        return []
    if isinstance(field, str):
        return [field] if field.strip() else []
    if isinstance(field, dict):
        if "$" in field:
            return [str(field["$"])]
        return []
    if isinstance(field, list):
        result = []
        seen = set()
        for item in field:
            value = None
            if isinstance(item, str):
                value = item.strip() or None
            elif isinstance(item, dict) and item.get("$"):
                value = str(item["$"]).strip() or None
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result
    return []


def _extract_creators(enriched: Any) -> List[Dict]:
    """
    Extrait les auteurs depuis isidore.enrichedCreators.

    Le champ peut contenir :
        - {"creator": {dict unique}}
        - {"creator": [liste de dicts]}
        - dict ou liste directs (selon agrégateur)

    Args:
        enriched: Valeur de isidore.enrichedCreators

    Returns:
        Liste de creators Zotero
    """
    if not enriched:
        return []

    # Déballer creator
    if isinstance(enriched, dict):
        creators_raw = enriched.get("creator", enriched)
    else:
        creators_raw = enriched

    if isinstance(creators_raw, dict):
        creators_raw = [creators_raw]
    elif not isinstance(creators_raw, list):
        return []

    result = []
    for c in creators_raw:
        if not isinstance(c, dict):
            continue
        first = c.get("firstname", "") or ""
        last = c.get("lastname", "") or ""
        normalized = c.get("@normalizedAuthor") or c.get("@origin") or ""

        if first or last:
            result.append({
                "creatorType": "author",
                "firstName": clean_string(first),
                "lastName": clean_string(last),
            })
        elif normalized:
            # Format normalized : "Nom, Prénom" ou nom brut
            normalized = clean_string(normalized)
            if "," in normalized:
                last, _, first = normalized.partition(",")
                result.append({
                    "creatorType": "author",
                    "firstName": first.strip(),
                    "lastName": last.strip(),
                })
            else:
                tokens = normalized.split()
                if len(tokens) >= 2:
                    result.append({
                        "creatorType": "author",
                        "firstName": " ".join(tokens[:-1]),
                        "lastName": tokens[-1],
                    })
                elif tokens:
                    result.append({
                        "creatorType": "author",
                        "name": normalized,
                    })
    return result


def _extract_date(date_field: Any) -> str:
    """
    Extrait une chaîne de date depuis isidore.date.

    Args:
        date_field: Objet date Isidore

    Returns:
        Date au format ISO ou année à 4 chiffres
    """
    if not date_field:
        return ""
    if isinstance(date_field, str):
        return date_field
    if isinstance(date_field, dict):
        # Priorité : normalizedDate, @startDate, year
        for key in ("normalizedDate", "@startDate", "year", "@year"):
            v = date_field.get(key)
            if v:
                return str(v)
        # Fallback : première valeur scalaire
        for v in date_field.values():
            if isinstance(v, str) and v:
                return v
    return ""


def _extract_types(types_field: Any) -> List[str]:
    """
    Extrait la liste des types Dublin Core depuis isidore.types.

    Args:
        types_field: Objet types Isidore

    Returns:
        Liste de chaînes de types
    """
    if not types_field:
        return []
    if isinstance(types_field, str):
        return [types_field]
    if isinstance(types_field, list):
        result = []
        for t in types_field:
            if isinstance(t, str):
                result.append(t)
            elif isinstance(t, dict) and t.get("$"):
                result.append(str(t["$"]))
        return result
    if isinstance(types_field, dict):
        t = types_field.get("type")
        if t is None:
            return []
        if isinstance(t, str):
            return [t]
        if isinstance(t, list):
            return [str(x) for x in t if x]
    return []


def _extract_all_urls(iso: Dict) -> List[str]:
    """
    Extrait l'URL primaire et toutes les URLs des aggregates ORE.

    Args:
        iso: bloc isidore.* d'un record

    Returns:
        Liste d'URLs (sans doublons, primaire en premier)
    """
    urls = []
    seen = set()

    primary = iso.get("url", "")
    if isinstance(primary, str) and primary and primary not in seen:
        urls.append(primary)
        seen.add(primary)

    ore = iso.get("ore", {})
    aggregates = ore.get("aggregates", []) if isinstance(ore, dict) else []
    if isinstance(aggregates, dict):
        aggregates = [aggregates]
    for agg in aggregates:
        if isinstance(agg, dict):
            u = agg.get("$", "")
        else:
            u = str(agg)
        if u and u not in seen:
            urls.append(u)
            seen.add(u)

    return urls


def map_isidore_to_zotero(record: Dict) -> Dict:
    """
    Convertit un enregistrement Isidore JSON en item Zotero.

    Args:
        record: Dict tel que renvoyé par l'API Isidore (un élément de
            response.replies.content.reply)

    Returns:
        Item Zotero validé
    """
    # === Extraction du bloc isidore ===
    iso = record.get("isidore", {})
    if not isinstance(iso, dict):
        iso = {}

    # === Détermination de l'itemType ===
    types_list = _extract_types(iso.get("types"))
    item_type = map_dublin_core_types_list(types_list) if types_list else "document"
    item = make_empty_item(item_type)

    # === Titre ===
    title = _pick_multilang(iso.get("title"))
    if title:
        item["title"] = clean_string(title)

    # === Auteurs ===
    item["creators"] = _extract_creators(iso.get("enrichedCreators"))

    # === Résumé ===
    abstract = _pick_multilang(iso.get("abstract"))
    if abstract:
        item["abstractNote"] = clean_string(abstract)

    # === Date ===
    date_str = _extract_date(iso.get("date"))
    if date_str:
        item["date"] = date_str

    # === Langue ===
    lang = iso.get("language") or iso.get("lang")
    if isinstance(lang, list) and lang:
        lang = lang[0]
    if isinstance(lang, dict):
        lang = lang.get("$", "")
    if lang:
        item["language"] = clean_string(str(lang))

    # === Mots-clés enrichis ===
    subjects = _all_multilang(iso.get("subject"))
    if subjects:
        item["tags"] = make_tags(clean_list(subjects))

    # === URLs (primaire + aggregates) ===
    urls = _extract_all_urls(iso)
    if urls:
        item["url"] = clean_string(urls[0])

    # Extraction DOI depuis les URLs (priorité doi.org)
    for u in urls:
        if "doi.org" in u:
            doi = u.split("doi.org/", 1)[-1].rstrip("/")
            if doi.startswith("10."):
                item["DOI"] = clean_string(doi)
                break

    # === Champs spécifiques par type ===
    if item_type == "journalArticle":
        # Le nom de la revue est dans isidore.source.name
        source = iso.get("source")
        if isinstance(source, dict):
            journal = _pick_multilang(source.get("name"))
            if journal:
                item["publicationTitle"] = clean_string(journal)

    # === Enrichissement de 'extra' ===

    # Source canonique
    append_to_extra(item, "Sources", "Isidore")

    # URI Isidore (clé pour récupération de la fiche)
    isidore_uri = record.get("@uri")
    if isidore_uri:
        append_to_extra(item, "Isidore URI", f"https://isidore.science/resource/{isidore_uri}")

    # Source originelle (nom de revue ou agrégateur)
    source = iso.get("source")
    if isinstance(source, dict):
        source_name = _pick_multilang(source.get("name"))
        if source_name:
            append_to_extra(item, "Original Source", source_name)

    # Types DC originaux conservés
    if types_list:
        types_str = ", ".join(types_list)
        append_to_extra(item, "DC Types", types_str)

    # URLs alternatives (aggregates) si différentes de l'URL primaire
    if len(urls) > 1:
        append_to_extra(item, "Alternative URLs", " | ".join(urls[1:]))

    # === Validation finale ===
    return validate_and_clean(item)
