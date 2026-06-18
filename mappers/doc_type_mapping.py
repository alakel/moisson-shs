"""
Tables de correspondance entre types de documents sources et itemType Zotero.

Chaque source utilise une nomenclature distincte :
- HAL : codes courts (ART, COMM, OUV, COUV, THESE, HDR, REPORT, etc.)
- Dublin Core (Cairn, OpenEdition) : valeurs dcterms:type littérales
- Isidore : utilise généralement la nomenclature Dublin Core enrichie

Ce module unifie ces nomenclatures vers le vocabulaire Zotero
officiel décrit par https://api.zotero.org/schema.

Les itemType Zotero supportés (extrait pertinent SHS) :
- journalArticle : article de revue
- bookSection : chapitre d'ouvrage collectif
- book : monographie
- conferencePaper : communication de colloque
- thesis : thèse, HDR, mémoire
- report : rapport de recherche, working paper
- preprint : preprint
- blogPost : billet de carnet
- webpage : page web générique
- magazineArticle : article de magazine
- newspaperArticle : article de presse
- encyclopediaArticle : entrée d'encyclopédie
- document : type générique (fallback)
"""

# === HAL : codes docType_s ===
# Référence : https://api.archives-ouvertes.fr/docs/ref/resource/doctype

HAL_DOCTYPE_MAP = {
    # Articles de revue
    "ART": "journalArticle",
    "ARTHDR": "journalArticle",
    # Communications
    "COMM": "conferencePaper",
    "POSTER": "conferencePaper",
    "PRESCONF": "presentation",
    "UNDEFINED": "document",
    # Ouvrages
    "OUV": "book",
    "DOUV": "book",
    "COUV": "bookSection",
    # Thèses
    "THESE": "thesis",
    "HDR": "thesis",
    "ETABTHESE": "thesis",
    "MEM": "thesis",
    # Rapports et autres
    "REPORT": "report",
    "REPACT": "report",
    "OTHERREPORT": "report",
    "NOTE": "report",
    "PATENT": "patent",
    "WORKINGPAPER": "report",
    # Médias et productions
    "VIDEO": "videoRecording",
    "SON": "audioRecording",
    "IMG": "artwork",
    "MAP": "map",
    "SOFTWARE": "computerProgram",
    # Blogs et productions web
    "BLOG": "blogPost",
    # Préprints (HAL utilise UNDEFINED ou ART selon les cas)
    "PREPRINT": "preprint",
    # Notes de lecture et critiques
    "CRITIQUE": "journalArticle",
    "NOTICE": "encyclopediaArticle",
    "ISSUE": "document",
    "TRAD": "book",
    "MANUAL": "book",
    "CHAPTER": "bookSection",
    "LECTURE": "presentation",
    "MEMLIC": "thesis",
}


# === Dublin Core : valeurs dcterms:type ===
# Référence : DCMI Type Vocabulary + variantes des éditeurs

DUBLIN_CORE_TYPE_MAP = {
    # Variantes courantes pour articles
    "article": "journalArticle",
    "articles": "journalArticle",
    "journalarticle": "journalArticle",
    "journal articles": "journalArticle",
    "journal article": "journalArticle",
    "research article": "journalArticle",
    "article de revue": "journalArticle",
    "text.serial.journal": "journalArticle",
    "info:eu-repo/semantics/article": "journalArticle",
    "review": "journalArticle",
    "info:eu-repo/semantics/review": "journalArticle",
    "peerreviewed": "journalArticle",
    "publishedversion": "journalArticle",
    "info:eu-repo/semantics/publishedversion": "journalArticle",
    # Communications
    "conferenceobject": "conferencePaper",
    "conference object": "conferencePaper",
    "conference paper": "conferencePaper",
    "conference papers": "conferencePaper",
    "info:eu-repo/semantics/conferenceobject": "conferencePaper",
    "info:eu-repo/semantics/conferencepaper": "conferencePaper",
    "communication": "conferencePaper",
    # Chapitres et ouvrages
    "bookpart": "bookSection",
    "book part": "bookSection",
    "book section": "bookSection",
    "book sections": "bookSection",
    "chapter": "bookSection",
    "chapitre": "bookSection",
    "info:eu-repo/semantics/bookpart": "bookSection",
    "book": "book",
    "books": "book",
    "livre": "book",
    "ouvrage": "book",
    "info:eu-repo/semantics/book": "book",
    # Thèses
    "thesis": "thesis",
    "theses": "thesis",
    "doctoralthesis": "thesis",
    "doctoral thesis": "thesis",
    "info:eu-repo/semantics/doctoralthesis": "thesis",
    "thèse ou essai doctoral accepté": "thesis",
    "masterthesis": "thesis",
    "master thesis": "thesis",
    "info:eu-repo/semantics/masterthesis": "thesis",
    "habilitation": "thesis",
    # Rapports
    "report": "report",
    "reports": "report",
    "info:eu-repo/semantics/report": "report",
    "workingpaper": "report",
    "working paper": "report",
    "info:eu-repo/semantics/workingpaper": "report",
    "preprint": "preprint",
    "preprints": "preprint",
    "info:eu-repo/semantics/preprint": "preprint",
    # Web
    "blogpost": "blogPost",
    "blog post": "blogPost",
    "billet": "blogPost",
    "webpage": "webpage",
    # Médias
    "image": "artwork",
    "video": "videoRecording",
    "sound": "audioRecording",
    # Numéros de revues et compilations
    "issue": "document",
    "compilation": "document",
    # Génériques DC
    "text": "document",
    "other publications": "document",
    "info:eu-repo/semantics/other": "document",
    "nonpeerreviewed": "document",
}


def map_hal_doctype(doctype: str) -> str:
    """
    Convertit un code HAL docType_s en itemType Zotero.

    Args:
        doctype: Code HAL (ex : "ART", "COMM", "OUV")

    Returns:
        itemType Zotero correspondant ('document' en fallback)
    """
    if not doctype:
        return "document"
    return HAL_DOCTYPE_MAP.get(doctype.upper(), "document")


def map_dublin_core_type(dc_type: str) -> str:
    """
    Convertit un type Dublin Core en itemType Zotero.

    Args:
        dc_type: Valeur du champ dc:type ou dcterms:type

    Returns:
        itemType Zotero correspondant ('document' en fallback)
    """
    if not dc_type:
        return "document"
    return DUBLIN_CORE_TYPE_MAP.get(dc_type.lower().strip(), "document")


def map_dublin_core_types_list(dc_types) -> str:
    """
    Comme map_dublin_core_type mais sur une liste de valeurs.

    Renvoie le premier type reconnu non générique. Les sources
    OAI fournissent souvent plusieurs valeurs dc:type, dont
    certaines sont des qualifiants génériques (text, image)
    que l'on souhaite ignorer en présence d'une valeur plus
    précise (journalArticle, bookSection).

    Args:
        dc_types: Liste de valeurs dc:type ou chaîne unique

    Returns:
        itemType Zotero
    """
    if not dc_types:
        return "document"
    if isinstance(dc_types, str):
        return map_dublin_core_type(dc_types)
    # On parcourt en cherchant la première correspondance non générique
    fallback = "document"
    for t in dc_types:
        mapped = map_dublin_core_type(t)
        if mapped != "document":
            return mapped
    return fallback
