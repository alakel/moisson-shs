"""
Mapper Google Scholar (via SerpAPI) → item Zotero JSON.

Google Scholar n'expose pas de métadonnées structurées. Un résultat SerpAPI
porte un titre, un lien, un extrait, un compteur de citations, et une *ligne de
notice* unique, ``publication_info.summary``, de la forme :

    "JL Harper - Population biology of plants., 1977 - cabdirect.org"
    "H Lodish, A Berk, CA Kaiser, M Krieger, MP Scott… - 2008 - books.google.com"

Auteurs, revue et année doivent en être extraits par analyse de chaîne. Cette
extraction est heuristique et lossy par construction : Google Scholar tronque
la liste d'auteurs et le nom de revue par des points de suspension (U+2026).
Le mapper ne reconstitue jamais ce qui est tronqué ; il pose un drapeau dans
``extra`` (« Scholar truncation ») pour que la notice soit vérifiable, et
laisse les champs incertains vides plutôt que de les inventer.

Trois conséquences pour l'usage bibliographique :

- le champ ``DOI`` n'est renseigné que depuis une URL qui en porte un de façon
  non ambiguë (hôte ``doi.org`` ou chemin ``/doi/``) ; jamais deviné ;
- le champ ``abstractNote`` contient l'extrait Google Scholar, tronqué et
  parfois recomposé de fragments non contigus, pas le résumé de l'éditeur ;
- ``publicationTitle`` peut être une abréviation tronquée (« Advances in
  neural … ») : utilisable pour le tri, pas pour une référence APA.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from mappers.scholarly_types import scholar_type_to_zotero
from mappers.zotero_schema import (
    append_to_extra, make_empty_item, validate_and_clean,
)
from utils.names import split_display_name

SOURCE_LABEL = "Google Scholar (SerpAPI)"

# Points de suspension de troncature Google Scholar (U+2026 ou trois points).
_ELLIPSIS = "…"

# Année plausible pour une publication indexée par Scholar.
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")

# DOI : préfixe 10.NNNN suivi d'un suffixe. Borné pour ne pas absorber la
# query string ni un fragment d'URL.
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>&?#]+", re.IGNORECASE)

# Ponctuation résiduelle en fin de nom de revue après retrait de l'année.
_TRAILING_PUNCT = " ,.;:-–—"


def _looks_like_domain(part: str) -> bool:
    """Vrai si ``part`` a la forme d'un nom de domaine."""
    p = part.strip()
    return bool(p) and " " not in p and "." in p and not p.endswith(".")


def _has_source_segment(parts: List[str]) -> bool:
    """
    Vrai si le dernier segment de la notice désigne l'hébergeur, non la revue.

    Google Scholar termine ses notices par la provenance, tantôt sous forme de
    domaine (« cabdirect.org »), tantôt sous forme de nom de plateforme
    (« Wiley Online Library », « Taylor & Francis », « Springer »). Le test de
    domaine seul laissait passer la seconde forme dans le nom de revue, qui
    devenait « Learned Publishing, - Wiley Online Library ». La position
    tranche donc avant la forme : à trois segments ou plus, le dernier est
    toujours la provenance, la grammaire étant « auteurs - revue, année -
    provenance ». À deux segments, l'ambiguïté demeure et la forme décide.
    """
    if len(parts) >= 3:
        return True
    return len(parts) == 2 and _looks_like_domain(parts[-1])


def _strip_ellipsis(text: str) -> Tuple[str, bool]:
    """
    Retire les points de suspension de troncature ; signale leur présence.

    La ponctuation résiduelle est retirée à chaque passe : le retrait de
    l'année laisse des formes comme « Advances in neural …, » où les points
    de suspension ne sont plus en position finale.
    """
    t = (text or "").strip(_TRAILING_PUNCT + " ")
    truncated = False
    changed = True
    while changed:
        changed = False
        for mark in (_ELLIPSIS, "..."):
            if t.endswith(mark):
                t = t[: -len(mark)].strip(_TRAILING_PUNCT + " ")
                truncated = changed = True
            if t.startswith(mark):
                t = t[len(mark):].strip(_TRAILING_PUNCT + " ")
                truncated = changed = True
    return t, truncated


def parse_publication_summary(summary: str) -> Dict:
    """
    Décompose la ligne de notice Google Scholar.

    Args:
        summary: Valeur de ``publication_info.summary``.

    Returns:
        Dict ``{authors: [str], venue: str, year: str, domain: str,
        truncated: bool}``. Toute composante non identifiable est vide : le
        mapper ne complète jamais par inférence.
    """
    out = {"authors": [], "venue": "", "year": "", "domain": "",
           "truncated": False}
    raw = (summary or "").strip()
    if not raw:
        return out

    parts = [p.strip() for p in raw.split(" - ") if p.strip()]
    if not parts:
        return out

    # Segment terminal : provenance (domaine ou plateforme), jamais une
    # donnée bibliographique.
    if _has_source_segment(parts):
        out["domain"] = parts.pop()

    if not parts:
        return out

    if len(parts) == 1:
        # Un seul segment : auteurs seuls, ou notice sans auteur identifié.
        # Un millésime tranche en faveur de la notice.
        lone = parts[0]
        if _YEAR_RE.search(lone):
            author_seg, venue_seg = "", lone
        else:
            author_seg, venue_seg = lone, ""
    else:
        author_seg = parts[0]
        venue_seg = " - ".join(parts[1:])

    # --- Auteurs ---
    if author_seg:
        names, trunc = _strip_ellipsis(author_seg)
        out["truncated"] = out["truncated"] or trunc
        for name in names.split(","):
            clean, trunc_n = _strip_ellipsis(name)
            out["truncated"] = out["truncated"] or trunc_n
            if clean:
                out["authors"].append(clean)

    # --- Année puis revue ---
    # Les points de suspension peuvent se trouver ailleurs qu'en bordure (« …
    # Conference on Artificial Intelligence and …, 2025 ») : les détecter sur
    # le segment entier avant tout retrait.
    if _ELLIPSIS in (venue_seg or "") or "..." in (venue_seg or ""):
        out["truncated"] = True
    if venue_seg:
        years = _YEAR_RE.findall(venue_seg)
        if years:
            out["year"] = years[-1]
            # Retrait de la seule dernière occurrence (un titre peut contenir
            # une date : « La crise de 1929 dans … , 2015 »).
            idx = venue_seg.rfind(years[-1])
            venue_seg = venue_seg[:idx] + venue_seg[idx + 4:]
        venue, trunc_v = _strip_ellipsis(venue_seg)
        out["truncated"] = out["truncated"] or trunc_v
        out["venue"] = venue.strip(_TRAILING_PUNCT)

    return out


def extract_doi(*urls: Optional[str]) -> str:
    """
    Extrait un DOI d'une ou plusieurs URL, sans jamais le deviner.

    Seuls deux motifs sont acceptés, parce qu'eux seuls désignent le document
    lui-même : un hôte ``doi.org``/``dx.doi.org``, ou un chemin contenant
    ``/doi/`` (motif des plateformes éditeurs : Taylor & Francis, Wiley, ACM,
    Sage). Un identifiant qui apparaîtrait ailleurs dans une URL (paramètre de
    suivi, DOI du numéro et non de l'article) est écarté.

    Returns:
        Le DOI normalisé en minuscules, ou une chaîne vide.
    """
    for url in urls:
        if not url:
            continue
        decoded = unquote(url)
        try:
            host = (urlparse(decoded).netloc or "").lower()
            path = urlparse(decoded).path or ""
        except ValueError:
            continue
        low = path.lower()
        # Trois motifs seulement, parce qu'eux seuls désignent le document :
        # l'hôte doi.org ; le chemin /doi/ des plateformes (Wiley, Taylor &
        # Francis, Sage, ACM, PNAS, Science) ; et le chemin Springer
        # /article/10. ou /chapter/10., qui porte le DOI en clair sans le
        # segment /doi/. Ce troisième motif a été ajouté après avoir constaté
        # qu'il faisait perdre des DOI parfaitement lisibles dans l'URL.
        eligible = (
            host.endswith("doi.org")
            or "/doi/" in low
            or re.search(r"/(article|chapter|referenceworkentry)/10\.", low)
        )
        if not eligible:
            continue
        m = _DOI_RE.search(decoded)
        if m:
            doi = m.group(0).rstrip(").,;:/")
            # Extensions de fichier collées au suffixe sur les liens directs.
            for ext in (".pdf", ".full", ".epub", ".xml"):
                if doi.lower().endswith(ext):
                    doi = doi[: -len(ext)]
            # Segments de chemin que les plateformes ajoutent APRÈS le DOI :
            # De Gruyter sert `/doi/10.1515/sem-2024-0188/html`, Wiley et
            # Taylor & Francis `/doi/full/...`. Sans ce retrait, le suffixe du
            # DOI absorbe le segment et l'identifiant devient irrésoluble.
            for seg in ("/html", "/full", "/abstract", "/pdf", "/epub",
                        "/meta", "/text"):
                if doi.lower().endswith(seg):
                    doi = doi[: -len(seg)]
            # Identifiant d'article ajouté par la plateforme après le DOI
            # (MIT Press sert /doi/10.1162/qss_a_00337/125096). Un suffixe de
            # DOI ne se termine pas par un segment purement numérique précédé
            # d'une barre, sauf si le DOI n'a qu'un seul segment.
            m2 = re.match(r"^(10\.\d{4,9}/.+)/\d+$", doi)
            if m2:
                doi = m2.group(1)
            return doi.rstrip("/")
    return ""


def map_scholar_item(raw: Dict) -> Dict:
    """Convertit un élément de ``organic_results`` SerpAPI en item Zotero."""
    info = raw.get("publication_info") or {}
    parsed = parse_publication_summary(info.get("summary", ""))
    link = raw.get("link") or ""

    resources = raw.get("resources") or []
    pdf_link = ""
    for res in resources:
        if res.get("link"):
            pdf_link = res["link"]
            break

    item_type = scholar_type_to_zotero(
        raw.get("type"), parsed["venue"], link
    )
    item = make_empty_item(item_type)

    item["title"] = (raw.get("title") or "").strip()

    # --- Auteurs ---
    # La ligne de notice donne la liste la plus complète (SerpAPI ne liste dans
    # `authors` que ceux qui ont un profil Scholar) : elle fait foi, l'autre
    # sert à tracer les identifiants de profil.
    names = parsed["authors"]
    if not names:
        names = [a.get("name", "") for a in (info.get("authors") or [])]
    creators: List[Dict] = []
    for name in names:
        first, last = split_display_name(name)
        if first or last:
            creators.append({
                "creatorType": "author", "firstName": first, "lastName": last,
            })
    item["creators"] = creators

    if raw.get("snippet"):
        item["abstractNote"] = raw["snippet"].strip()

    if parsed["year"]:
        item["date"] = parsed["year"]

    # --- Conteneur ---
    venue = parsed["venue"]
    if venue:
        if item_type == "conferencePaper":
            item["proceedingsTitle"] = venue
        elif item_type == "bookSection":
            item["bookTitle"] = venue
        elif item_type in ("book", "thesis", "report", "preprint", "document"):
            # Pour ces types, la mention tenant lieu de conteneur n'a pas de
            # champ dédié fiable : conservée en 'extra' par validate_and_clean.
            item["publicationTitle"] = venue
        else:
            item["publicationTitle"] = venue

    item["url"] = link or pdf_link

    doi = extract_doi(link, *[r.get("link") for r in resources])
    if doi:
        item["DOI"] = doi

    # === Traçabilité (extra) ===
    append_to_extra(item, "Sources", SOURCE_LABEL)
    if raw.get("result_id"):
        append_to_extra(item, "Scholar result ID", raw["result_id"])

    inline = raw.get("inline_links") or {}
    cited_by = (inline.get("cited_by") or {}).get("total")
    if cited_by is not None:
        # str() pour préserver 0, significatif et sinon écarté par le garde
        # `if not value` de append_to_extra.
        append_to_extra(item, "Cited by", str(cited_by))
    cites_id = (inline.get("cited_by") or {}).get("cites_id")
    if cites_id:
        append_to_extra(item, "Scholar cites ID", cites_id)
    versions = inline.get("versions") or {}
    if versions.get("cluster_id"):
        append_to_extra(item, "Scholar cluster ID", versions["cluster_id"])
    if versions.get("total"):
        append_to_extra(item, "Scholar versions", str(versions["total"]))

    if pdf_link:
        append_to_extra(item, "Full text", pdf_link)
    if parsed["domain"]:
        append_to_extra(item, "Scholar host", parsed["domain"])
    if doi:
        append_to_extra(item, "DOI", doi)

    # Deux avertissements de fiabilité, portés par la notice elle-même : ils
    # signalent au lecteur (et au tri en aval) que ces champs ne sont pas des
    # métadonnées d'éditeur.
    if parsed["truncated"]:
        append_to_extra(
            item, "Scholar truncation",
            "auteurs et/ou revue tronqués par Google Scholar — à vérifier "
            "contre la source primaire avant citation",
        )
    if item.get("abstractNote"):
        append_to_extra(
            item, "Scholar snippet",
            "le champ résumé contient un extrait Google Scholar, non le "
            "résumé de l'éditeur",
        )

    return validate_and_clean(item)
