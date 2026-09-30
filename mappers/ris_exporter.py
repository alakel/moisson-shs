"""
Export des notices Zotero internes au format RIS (OPT-18).

RIS (Research Information Systems) : un enregistrement par notice, une
balise de deux caractères par ligne (``TAG  - valeur``), ouvert par ``TY`` et
fermé par ``ER``. Format d'échange lu par Zotero Desktop (Fichier > Importer),
EndNote, Mendeley, JabRef et la plupart des gestionnaires de références.

Balises produites, dans cet ordre :

| Balise | Contenu |
|--------|---------|
| ``TY`` | type RIS (``JOUR``, ``BOOK``, ``CHAP``, ``CPAPER``, ``THES``, ``RPRT``, ``UNPB``, ``GEN``…) |
| ``AU`` | auteurs, un par ligne (``Nom, Prénom`` ; institutionnel : nom seul) |
| ``A2`` | éditeurs scientifiques ; ``A4`` : traducteurs |
| ``TI`` | titre |
| ``T2`` | titre du conteneur (revue, ouvrage, actes, site) |
| ``T3`` | collection |
| ``PY`` | année ; ``DA`` : date ``AAAA/MM/JJ`` (aussi précise que connue) |
| ``VL``, ``IS`` | volume, numéro |
| ``SP``, ``EP`` | première et dernière page |
| ``ET`` | édition |
| ``PB``, ``CY`` | éditeur (université d'une thèse, institution d'un rapport), lieu |
| ``M3`` | type de thèse ou de rapport |
| ``SN`` | ISSN et ISBN (une ligne chacun) |
| ``DO``, ``UR`` | DOI, URL |
| ``LA`` | langue |
| ``KW`` | mots-clés, un par ligne |
| ``AB`` | résumé (jamais un extrait Google Scholar, invariant 6) |
| ``N1`` | ligne ``Annote:`` d'``extra`` ; avertissement de troncature Google Scholar (toujours) ; provenance de moisson seulement sur demande explicite |
| ``ER`` | fin d'enregistrement |

Comme pour BibTeX (``mappers.bibtex_exporter``, dont les aides de lecture
sont réutilisées) : aucune métadonnée de moisson par défaut (invariant 1),
champs migrés dans ``extra`` relus, DOI de ``extra`` repris. Une notice
marquée ``Scholar truncation`` (auteurs et/ou revue coupés par Google
Scholar) porte par défaut une note ``N1`` d'avertissement (invariant 6) ; RIS
n'a pas d'équivalent de « et al. », aucun auteur fictif n'est ajouté. Valeurs
sur une seule ligne (blancs réduits, contrôles C1 réparés), fin de ligne CRLF
comme dans les exports de référence, encodage UTF-8.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from mappers.bibtex_exporter import (
    SCHOLAR_TRUNCATION_WARNING, annote_text, creator_parts, creators_by_role,
    export_doi, exportable_abstract, field_value, item_tags, normalize_space,
    parse_date, provenance_lines, scholar_truncation, split_pages,
    strip_controls,
)


# itemType Zotero → type RIS (spécification RIS de Research Information
# Systems, liste des « reference types »).
RIS_TYPES: Dict[str, str] = {
    "journalArticle": "JOUR",
    "magazineArticle": "MGZN",
    "newspaperArticle": "NEWS",
    "book": "BOOK",
    "bookSection": "CHAP",
    "conferencePaper": "CPAPER",
    "thesis": "THES",
    "report": "RPRT",
    "preprint": "UNPB",
    "manuscript": "MANSCPT",
    "dataset": "DATA",
    "document": "GEN",
    "webpage": "ELEC",
    "blogPost": "BLOG",
    "presentation": "SLIDE",
    "letter": "PCOMM",
    "interview": "PCOMM",
    "computerProgram": "COMP",
    "encyclopediaArticle": "ENCYC",
    "dictionaryEntry": "DICT",
    "videoRecording": "VIDEO",
    "film": "MPCT",
    "audioRecording": "SOUND",
    "podcast": "SOUND",
    "artwork": "ART",
    "map": "MAP",
    "patent": "PAT",
    "case": "CASE",
    "bill": "BILL",
    "statute": "STAT",
    "hearing": "HEAR",
    "email": "ICOMM",
}

DEFAULT_RIS_TYPE = "GEN"

# Fin de ligne des exports RIS de référence (Zotero, EndNote).
RIS_EOL = "\r\n"


def ris_type(item: Dict) -> str:
    """Type RIS d'une notice Zotero (``GEN`` par défaut)."""
    return RIS_TYPES.get(item.get("itemType") or "document", DEFAULT_RIS_TYPE)


def _ris_name(c: Dict) -> str:
    last, first, institutional = creator_parts(c)
    if not last:
        return ""
    if institutional or not first:
        return last
    return f"{last}, {first}"


def _container(item: Dict) -> str:
    for name in ("publicationTitle", "bookTitle", "proceedingsTitle",
                 "encyclopediaTitle", "dictionaryTitle", "websiteTitle",
                 "blogTitle"):
        value = field_value(item, name)
        if value:
            return value
    if (item.get("itemType") == "conferencePaper"):
        return field_value(item, "conferenceName")
    return ""


def ris_record(
    item: Dict,
    *,
    include_provenance: bool = False,
    stats: Optional[Dict[str, int]] = None,
) -> List[Tuple[str, str]]:
    """
    Balises RIS d'une notice, dans un ordre fixe, ``TY`` en tête et ``ER``
    en fin. Valeurs sur une ligne ; balises sans valeur omises.

    Args:
        item: notice Zotero interne.
        include_provenance: exporter les lignes d'``extra`` (provenance de
            moisson) dans une note ``N1`` ; faux par défaut (invariant 1).
        stats: compteurs incrémentés (``snippet_abstracts_skipped``,
            ``annotes``, ``scholar_truncation_warnings``).
    """
    rec: List[Tuple[str, str]] = []

    def add(tag: str, value) -> None:
        text = normalize_space(value)
        if text:
            rec.append((tag, text))

    item_type = item.get("itemType") or "document"
    add("TY", ris_type(item))

    roles = creators_by_role(item)
    for c in roles.get("author", []):
        add("AU", _ris_name(c))
    for c in roles.get("editor", []):
        add("A2", _ris_name(c))
    for c in roles.get("translator", []):
        add("A4", _ris_name(c))

    add("TI", item.get("title"))
    add("T2", _container(item))
    add("T3", field_value(item, "series") or field_value(item, "seriesTitle"))

    year, month, day = parse_date(item.get("date"))
    if year:
        add("PY", f"{year:04d}")
        da = f"{year:04d}"
        if month:
            da += f"/{month:02d}"
            if day:
                da += f"/{day:02d}"
        add("DA", da)

    add("VL", field_value(item, "volume"))
    if item_type == "report":
        add("IS", field_value(item, "reportNumber"))
    else:
        add("IS", field_value(item, "issue"))
    start, end = split_pages(field_value(item, "pages"))
    add("SP", start)
    add("EP", end)
    add("ET", field_value(item, "edition"))

    if item_type == "thesis":
        add("PB", field_value(item, "university")
            or field_value(item, "institution")
            or field_value(item, "publisher"))
        add("M3", field_value(item, "thesisType"))
    elif item_type == "report":
        add("PB", field_value(item, "institution")
            or field_value(item, "publisher"))
        add("M3", field_value(item, "reportType"))
    elif item_type == "preprint":
        add("PB", field_value(item, "repository")
            or field_value(item, "publisher"))
    else:
        add("PB", field_value(item, "publisher"))
    add("CY", field_value(item, "place"))

    add("SN", field_value(item, "ISSN"))
    add("SN", field_value(item, "ISBN"))
    # Identifiants : contrôles retirés sans réparation (comme en BibTeX).
    add("DO", strip_controls(export_doi(item)))
    add("UR", strip_controls(item.get("url")))

    language = field_value(item, "language")
    if language.lower() not in ("und", "zxx", "mul"):
        add("LA", language)
    for tag in item_tags(item):
        add("KW", tag)

    abstract, skipped = exportable_abstract(item)
    if skipped and stats is not None:
        stats["snippet_abstracts_skipped"] = stats.get(
            "snippet_abstracts_skipped", 0) + 1
    add("AB", abstract)

    annote = annote_text(item)
    if annote:
        if stats is not None:
            stats["annotes"] = stats.get("annotes", 0) + 1
        add("N1", annote)
    if scholar_truncation(item)[0]:
        # Mise en garde de fiabilité (invariant 6), écrite par défaut.
        if stats is not None:
            stats["scholar_truncation_warnings"] = stats.get(
                "scholar_truncation_warnings", 0) + 1
        add("N1", SCHOLAR_TRUNCATION_WARNING)
    if include_provenance:
        add("N1", " ; ".join(provenance_lines(item)))

    rec.append(("ER", ""))
    return rec


def format_record(rec: Sequence[Tuple[str, str]]) -> str:
    """Enregistrement RIS : ``TAG  - valeur`` par ligne, fins CRLF."""
    return RIS_EOL.join(f"{tag}  - {value}" for tag, value in rec)


def export_ris(
    items: Sequence[Dict],
    *,
    include_provenance: bool = False,
    stats: Optional[Dict[str, int]] = None,
) -> str:
    """
    Corpus Zotero interne → texte RIS (UTF-8, fins de ligne CRLF, une ligne
    vide entre deux enregistrements), déterministe.

    Args:
        items: notices Zotero internes, dans l'ordre de sortie.
        include_provenance: exporter la provenance de moisson (``extra``)
            en note ``N1`` ; faux par défaut (invariant 1).
        stats: dictionnaire complété (``entries``,
            ``snippet_abstracts_skipped``, ``annotes``,
            ``scholar_truncation_warnings``).
    """
    stats = stats if stats is not None else {}
    stats["entries"] = len(items)
    records = [format_record(ris_record(
        it, include_provenance=include_provenance, stats=stats))
        for it in items]
    if not records:
        return ""
    return (RIS_EOL + RIS_EOL).join(records) + RIS_EOL


__all__ = ["DEFAULT_RIS_TYPE", "RIS_EOL", "RIS_TYPES", "export_ris",
           "format_record", "ris_record", "ris_type"]
