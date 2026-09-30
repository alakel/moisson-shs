"""
Vérification humaine des règles d'identité et de DOI du sprint patch
(PAT-09, retour d'expérience du 2026-09-30).

Les règles optionnelles (co-diffusion, versions Zenodo, chapitres Google
Books en déduplication ; numéro de chapitre en résolution DOI) restent
désactivées tant que leur vérification humaine n'a pas passé le gate fixé
dans le plan (``.claude/dev/sprint-patch.md``, PAT-09). Ce script produit les
fiches, lit les jugements et rend la décision, règle par règle.

Trois sous-commandes (depuis la racine du projet) ::

    venv/bin/python scripts/eval/rules_check.py generate --date patch-2026-09-30
    venv/bin/python scripts/eval/rules_check.py metrics --date patch-2026-09-30
    venv/bin/python scripts/eval/rules_check.py decide --date patch-2026-09-30

``generate`` (lecture seule des corpus de ``output/``) :

- rejoue la déduplication de chaque corpus, sans règle puis avec chaque
  règle, et retient les paires que la règle réunirait (lignes « positives ») ;
- y mêle des témoins proches que les règles écartent : paires de
  plateformes différentes à titres presque identiques, paires à DOI
  différents de même titre, même année et même premier auteur ;
- ajoute un échantillon (graine fixe) des DOI lus dans les liens Taylor &
  Francis (``identifierValue``) ;
- avec ``--crossref`` (réseau, CrossRef seulement, gratuit), résout les
  notices Google Books numérotées avec la règle du numéro de chapitre et
  ajoute les DOI qu'elle accepterait ;
- écrit, sous ``output/eval/<date>/`` : ``fiches/verification_regles.csv``
  et ``.html`` (tout, ordre aléatoire à graine fixe), le second jugement
  partiel ``fiches/verification_regles_B.csv`` et ``.html`` (environ 20 %
  des lignes, autre ordre), et la clé ``cles/verification_regles_cles.json``
  (règle et nature de chaque ligne), à ne pas ouvrir avant d'avoir jugé.

À l'aveugle : ni nom de règle, ni source, ni score, rien qui dise si la paire
serait fusionnée. Échelle : 1 = même publication (même article du même
numéro, même chapitre, même dépôt en deux versions) ; 0 = publications
distinctes ; ? = indécidable avec les liens. Pour une ligne DOI : 1 = le DOI
désigne ce travail ; 0 = DOI erroné ; ? = indécidable.

``metrics`` lit ``fiches/verification_regles_rempli.csv`` (export de la page
HTML ; à défaut, la colonne ``verdict`` du CSV rempli en place) et le second
jugement, compte les verdicts par règle et mesure l'accord (kappa de Cohen
sur les verdicts 1/0 des lignes jugées deux fois) : ``rapports/metrics_regles.json``.

``decide`` applique le gate fixé d'avance et écrit
``rapports/decision_regles.md`` :

- G1 : aucune paire jugée « 0 » parmi celles qu'une règle fusionne ;
- G2 : au plus 10 % de « ? » parmi ces paires (un « ? » ne compte jamais
  comme correct) ;
- G3 : jeux témoins avec la règle active : 0 fausse fusion sur les paires
  négatives de ``tests/fixtures/dedup_pairs.json``, positives ≥ 52/54 ;
  attendus de ``tests/fixtures/dedup_pairs_patch.json`` tenus ;
- G4 : aucun DOI jugé « 0 » (numéro de chapitre, échantillon Taylor &
  Francis) ; jeu négatif de résolution à 0 DOI écrit, règle active
  (``tests/unit/test_opt10_negative_set.py``) ;
- G5 : kappa et désaccords consignés ; sous 0,6, relire les désaccords.

Une règle n'est déclarée « à activer » que si G1 à G4 sont atteints.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval import common as C  # noqa: E402

SEED = 20260930
SHEET = "verification_regles"
SHEET_B = "verification_regles_B"
KEYS_DIR = "cles"
KEYS_FILE = "verification_regles_cles.json"
METRICS_FILE = "metrics_regles.json"
DECISION_FILE = "decision_regles.md"
SECOND_SHARE = 0.20
TF_SAMPLE = 10
MAX_QUESTION_SHARE = 0.10

RULE_CODIFFUSION = "codiffusion"
RULE_ZENODO = "zenodo_versions"
RULE_CHAPTERS = "scholar_chapters"
RULE_DOI_CHAPTER = "doi_chapter_prefix"
RULE_TF = "taylor_francis_doi"
RULE_LABELS = {
    RULE_CODIFFUSION: "co-diffusion d'un même article (MOISSON_DEDUP_CODIFFUSION)",
    RULE_ZENODO: "versions Zenodo (MOISSON_DEDUP_ZENODO_VERSIONS)",
    RULE_CHAPTERS: "chapitres Google Books en déduplication (MOISSON_DEDUP_SCHOLAR_CHAPTERS)",
    RULE_DOI_CHAPTER: "numéro de chapitre en résolution DOI (MOISSON_DOI_CHAPTER_PREFIX)",
    RULE_TF: "DOI des liens Taylor & Francis (identifierValue)",
}
DEDUP_RULES = (RULE_CODIFFUSION, RULE_ZENODO, RULE_CHAPTERS)

TASK_PAIR = "même publication ?"
TASK_DOI = "DOI juste ?"
COLUMNS = ("ligne_id", "question", "notice_1", "lien_1", "notice_2", "lien_2",
           "verdict", "remarque")
VERDICTS = ("1", "0", "?")

_CORPUS_EXCLUDE = re.compile(r"\.(csl|manifest|doi_report|diff)\.json$")


# === Lecture des corpus ===

def corpus_files(output_dir: Path) -> List[Path]:
    return [p for p in sorted(Path(output_dir).glob("corpus_*.json"))
            if not _CORPUS_EXCLUDE.search(p.name)]


def load_items(path: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []


def notice_text(item: Dict[str, Any]) -> str:
    """Texte d'une notice pour la fiche : sans source, sans score."""
    f = C.display_fields(item, abstract_chars=300)
    parts = [f["titre"]]
    if f["auteurs"]:
        parts.append(f["auteurs"])
    meta = [x for x in (f["annee"], f["type"], f["support"]) if x]
    vol = [f"{label} {C.clean_text(item.get(k))}" for k, label in
           (("volume", "vol."), ("issue", "n°"), ("pages", "p."))
           if C.clean_text(item.get(k))]
    if meta or vol:
        parts.append(" · ".join(meta + vol))
    doi = C.extract_doi(item)
    if doi:
        parts.append(f"DOI {doi}")
    return " | ".join(parts)


def pair_key(a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[str, str]:
    return tuple(sorted((C.canonical_dump(a), C.canonical_dump(b))))


# === Paires fusionnées par une règle ===

def rule_pairs(items: List[Dict[str, Any]], rule: str):
    """Paires (deux notices représentatives) des groupes que ``rule`` seule
    ajoute à la déduplication d'OPT-06."""
    from utils.deduplication import (_identity_doi, dedup_rules,
                                     deduplicate_with_report)
    base = dedup_rules(False, False, False)
    on = dedup_rules(**{rule: True})
    _b, rb = deduplicate_with_report(copy.deepcopy(items), rules=base,
                                     with_groups=True)
    _o, ro = deduplicate_with_report(copy.deepcopy(items), rules=on,
                                     with_groups=True)
    before = {tuple(g) for g in rb["groups"]}
    out = []
    for group in ro["groups"]:
        if tuple(group) in before or len(group) < 2:
            continue
        # Deux représentants : des groupes d'OPT-06 différents.
        owner = {}
        for gi, g in enumerate(rb["groups"]):
            for i in g:
                owner[i] = gi
        reps: Dict[int, int] = {}
        for i in group:
            reps.setdefault(owner[i], i)
        chosen = sorted(reps.values())[:2]
        if len(chosen) == 2:
            a, b = items[chosen[0]], items[chosen[1]]
            # Préférence : une notice par DOI quand il y en a deux.
            dois = {}
            for i in group:
                d = _identity_doi(items[i])
                if d:
                    dois.setdefault(d, i)
            if len(dois) >= 2:
                ia, ib = sorted(dois.values())[:2]
                a, b = items[ia], items[ib]
            out.append((a, b))
    return out


# === Témoins proches ===

PLATFORM_PREFIXES = {"10.3917", "10.4000", "10.3406", "10.7202", "10.4074"}


def control_pairs(items: List[Dict[str, Any]]):
    """Paires que les règles doivent écarter ou dont la fusion se discute :
    plateformes différentes à titres presque identiques ; DOI différents,
    même titre, même année, même premier auteur."""
    from utils.deduplication import _identity_doi
    from utils.doi_resolver import best_title_ratio, normalize_title
    with_doi = [(it, _identity_doi(it)) for it in items]
    with_doi = [(it, d) for it, d in with_doi if d]
    out = []
    by_title = defaultdict(list)
    for it, d in with_doi:
        by_title[normalize_title(it.get("title", ""))].append((it, d))
    for group in by_title.values():
        for x, (a, da) in enumerate(group):
            for b, db in group[x + 1:]:
                if da != db and C.year_of(a) == C.year_of(b) and \
                        C.first_author_lastname(a) == C.first_author_lastname(b):
                    out.append((a, b))
    plat = [(it, d) for it, d in with_doi if d.split("/")[0] in PLATFORM_PREFIXES]
    for x, (a, da) in enumerate(plat):
        for b, db in plat[x + 1:]:
            if da.split("/")[0] == db.split("/")[0]:
                continue
            ratio, _ = best_title_ratio(a.get("title", ""), b.get("title", ""))
            if ratio >= 0.90:
                out.append((a, b))
    return out


# === DOI à vérifier ===

def taylor_francis_rows(items: List[Dict[str, Any]]):
    from utils.identity import doi_from_identifier_query
    out = []
    for it in items:
        if it.get("DOI"):
            continue
        doi = doi_from_identifier_query(it.get("url"))
        if doi:
            out.append((it, doi))
    return out


def chapter_doi_rows(items: List[Dict[str, Any]]):
    """Notices Google Books numérotées résolues avec la règle du numéro de
    chapitre (réseau : CrossRef). Rend les DOI acceptés par cette règle."""
    import utils.doi_resolver as dr
    targets = [it for it in items
               if not it.get("DOI") and dr._CHAPTER_TITLE_RE.match(it.get("title") or "")
               and "books.google." in (it.get("url") or "").lower()]
    if not targets:
        return []
    old = os.environ.get(dr.CHAPTER_PREFIX_ENV)
    os.environ[dr.CHAPTER_PREFIX_ENV] = "1"
    try:
        out_items, report = dr.resolve_dois(copy.deepcopy(targets))
    finally:
        if old is None:
            os.environ.pop(dr.CHAPTER_PREFIX_ENV, None)
        else:
            os.environ[dr.CHAPTER_PREFIX_ENV] = old
    rows = []
    for orig, new in zip(targets, out_items):
        extra = new.get("extra") or ""
        if new.get("DOI") and "numéro de chapitre" in extra:
            rows.append((orig, new["DOI"]))
    return rows


# === Génération ===

def _row_pair(a, b):
    return {"question": TASK_PAIR, "notice_1": notice_text(a),
            "lien_1": C.neutral_link(a), "notice_2": notice_text(b),
            "lien_2": C.neutral_link(b), "verdict": "", "remarque": ""}


def _row_doi(item, doi):
    return {"question": TASK_DOI, "notice_1": notice_text(item),
            "lien_1": C.neutral_link(item) if C.extract_doi(item) else (item.get("url") or ""),
            "notice_2": f"DOI proposé : {doi}", "lien_2": f"https://doi.org/{doi}",
            "verdict": "", "remarque": ""}


def build_rows(output_dir: Path, crossref: bool = False,
               seed: int = SEED) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    entries: Dict[Tuple, Dict[str, Any]] = {}

    def add(kind: str, rule: str, row: Dict[str, str], key: Tuple, corpus: str):
        entry = entries.get(key)
        if entry is None:
            entries[key] = {"row": row, "rules": {rule: kind},
                            "corpora": [corpus]}
        else:
            # Une paire positive pour une règle prime sur un témoin.
            if entry["rules"].get(rule) != "positive":
                entry["rules"][rule] = kind
            if corpus not in entry["corpora"]:
                entry["corpora"].append(corpus)

    tf_rows = []
    chapter_targets = []
    for path in corpus_files(output_dir):
        items = load_items(path)
        if not items:
            continue
        for rule in DEDUP_RULES:
            for a, b in rule_pairs(items, rule):
                add("positive", rule, _row_pair(a, b), ("pair",) + pair_key(a, b),
                    path.name)
        for a, b in control_pairs(items):
            add("temoin", "controle", _row_pair(a, b), ("pair",) + pair_key(a, b),
                path.name)
        for it, doi in taylor_francis_rows(items):
            tf_rows.append((it, doi, path.name))
        chapter_targets.extend(items)
    # Échantillon des DOI Taylor & Francis : un par DOI distinct.
    distinct = {}
    for it, doi, corpus in tf_rows:
        distinct.setdefault(doi, (it, doi, corpus))
    rng = random.Random(seed)
    sample = sorted(distinct)
    rng.shuffle(sample)
    for doi in sorted(sample[:TF_SAMPLE]):
        it, _d, corpus = distinct[doi]
        add("positive", RULE_TF, _row_doi(it, doi), ("doi", doi), corpus)
    if crossref:
        seen = set()
        for it, doi in chapter_doi_rows(chapter_targets):
            if doi in seen:
                continue
            seen.add(doi)
            add("positive", RULE_DOI_CHAPTER, _row_doi(it, doi), ("doi", doi),
                "(résolution)")
    # Ordre aléatoire à graine fixe, identifiants attribués après mélange.
    keys = sorted(entries, key=lambda k: json.dumps(k, ensure_ascii=False))
    random.Random(seed + 1).shuffle(keys)
    rows, key_doc = [], {}
    for n, key in enumerate(keys, 1):
        rid = f"V{n:03d}"
        row = dict(entries[key]["row"], ligne_id=rid)
        rows.append({c: row.get(c, "") for c in COLUMNS})
        key_doc[rid] = {"rules": entries[key]["rules"],
                        "corpora": entries[key]["corpora"],
                        "question": row["question"]}
    meta = {"seed": seed, "crossref": crossref, "lines": key_doc,
            "counts": dict(Counter(k for e in key_doc.values()
                                   for k, v in e["rules"].items() if v == "positive"))}
    return rows, meta


def second_sample(rows: List[Dict[str, str]], seed: int = SEED):
    n = max(1, round(len(rows) * SECOND_SHARE)) if rows else 0
    ids = sorted(r["ligne_id"] for r in rows)
    rng = random.Random(seed + 2)
    chosen = set(rng.sample(ids, n)) if n else set()
    sub = [dict(r) for r in rows if r["ligne_id"] in chosen]
    rng.shuffle(sub)
    return sub


def rows_to_csv(rows: Sequence[Dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, delimiter=";",
                            quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in COLUMNS})
    return buf.getvalue()


def read_sheet(path: Path) -> List[Dict[str, str]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


# === Page HTML autonome ===

_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#5f6670;--line:#d9d6cf;--card:#ffffff;
--acc:#2f5d8a;--ok:#2e7d32;--ko:#b3261e;--unk:#8a6d00}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8e6e3;--muted:#a3a8b0;
--line:#33373d;--card:#1e2126;--acc:#8ab4e0;--ok:#81c784;--ko:#ef9a9a;--unk:#e0c060}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;z-index:2}
h1{font-size:1.1rem;margin:0 0 6px}
.help{color:var(--muted);font-size:.9rem;margin:4px 0}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
button,.file{font:inherit;padding:5px 10px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;cursor:pointer}
main{padding:12px 16px;max-width:1100px;margin:0 auto}
.row{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:10px 0;outline:none}
.row.active{border-color:var(--acc);box-shadow:0 0 0 2px var(--acc)}
.q{font-weight:600;margin-bottom:6px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:700px){.pair{grid-template-columns:1fr}}
.n{border-left:3px solid var(--line);padding-left:8px;overflow-wrap:anywhere}
.n a{color:var(--acc)}
.v{display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap}
.v button.on[data-v="1"]{background:var(--ok);color:#fff}
.v button.on[data-v="0"]{background:var(--ko);color:#fff}
.v button.on[data-v="?"]{background:var(--unk);color:#000}
.v input{flex:1;min-width:160px;font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
</style></head><body>
<header><h1>__TITLE__</h1>
<div class="help">__INTRO__</div>
<div class="help"><b>1</b> = même publication (même article du même numéro, même chapitre, même dépôt en deux versions) ; <b>0</b> = publications distinctes (autre texte, autre numéro ou autre revue, compte rendu, traduction, prépublication face à la version publiée) ; <b>?</b> = indécidable avec les liens. Pour « DOI juste ? » : <b>1</b> = le DOI désigne ce travail ; <b>0</b> = DOI erroné.</div>
<div class="help">Au clavier : cliquer une ligne (ou Tab), puis taper 1, 0 ou ? (u vaut ?) ; la ligne suivante prend la main. Les verdicts restent dans ce navigateur ; « Exporter le CSV » produit le fichier à rendre.</div>
<div class="bar"><button id="next" type="button">Prochaine ligne sans verdict</button>
<button id="export" type="button">Exporter le CSV</button>
<label class="file">Importer un CSV <input id="import" type="file" accept=".csv,text/csv" hidden></label>
<span id="prog" class="help"></span></div></header>
<main id="list"></main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
const D=JSON.parse(document.getElementById('data').textContent);
const KEY='moisson-rules-'+D.sheet_id;
let S={};try{S=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){S={}}
function save(){try{localStorage.setItem(KEY,JSON.stringify(S))}catch(e){}}
const list=document.getElementById('list');
function esc(t){return String(t||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function link(u){return u&&/^https?:\\/\\//.test(u)?' <a href="'+esc(u)+'" target="_blank" rel="noopener noreferrer">ouvrir</a>':''}
D.rows.forEach(function(r,i){
 const el=document.createElement('section');el.className='row';el.tabIndex=0;el.dataset.id=r.ligne_id;
 el.innerHTML='<div class="q">'+esc(r.ligne_id)+' · '+esc(r.question)+'</div><div class="pair"><div class="n">'+esc(r.notice_1)+link(r.lien_1)+'</div><div class="n">'+esc(r.notice_2)+link(r.lien_2)+'</div></div><div class="v"><button type="button" data-v="1">1</button><button type="button" data-v="0">0</button><button type="button" data-v="?">?</button><input type="text" placeholder="remarque"></div>';
 const inp=el.querySelector('input');const st=S[r.ligne_id]||{};inp.value=st.remarque||'';
 function paint(){const v=(S[r.ligne_id]||{}).verdict||'';el.querySelectorAll('.v button').forEach(b=>b.classList.toggle('on',b.dataset.v===v))}
 el.querySelectorAll('.v button').forEach(b=>b.addEventListener('click',function(){set(r.ligne_id,b.dataset.v);paint();progress()}));
 inp.addEventListener('input',function(){S[r.ligne_id]=Object.assign({},S[r.ligne_id],{remarque:inp.value});save()});
 inp.addEventListener('keydown',function(e){e.stopPropagation()});
 el.addEventListener('focus',function(){document.querySelectorAll('.row.active').forEach(x=>x.classList.remove('active'));el.classList.add('active')});
 el.addEventListener('keydown',function(e){let v=null;if(e.key==='1')v='1';else if(e.key==='0')v='0';else if(e.key==='?'||e.key==='u'||e.key==='U')v='?';
  if(v){e.preventDefault();set(r.ligne_id,v);paint();progress();const nx=el.nextElementSibling;if(nx)nx.focus();}});
 paint();list.appendChild(el);
});
function set(id,v){S[id]=Object.assign({},S[id],{verdict:v});save()}
function progress(){const n=D.rows.filter(r=>(S[r.ligne_id]||{}).verdict).length;document.getElementById('prog').textContent=n+' / '+D.rows.length+' lignes jugées'}
progress();
document.getElementById('next').addEventListener('click',function(){const r=D.rows.find(r=>!(S[r.ligne_id]||{}).verdict);if(r){const el=list.querySelector('[data-id="'+r.ligne_id+'"]');el.scrollIntoView({block:'center'});el.focus()}});
function csvCell(t){t=String(t==null?'':t);return /[;"\\n\\r]/.test(t)?'"'+t.replace(/"/g,'""')+'"':t}
document.getElementById('export').addEventListener('click',function(){
 const cols=D.columns;const lines=[cols.join(';')];
 D.rows.forEach(function(r){const st=S[r.ligne_id]||{};const row=Object.assign({},r,{verdict:st.verdict||'',remarque:st.remarque||''});lines.push(cols.map(c=>csvCell(row[c])).join(';'))});
 const blob=new Blob(['\\ufeff'+lines.join('\\n')+'\\n'],{type:'text/csv;charset=utf-8'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=D.file_stem+'_rempli.csv';document.body.appendChild(a);a.click();a.remove();
});
function parseCsv(text){const rows=[];let row=[],cell='',q=false;for(let i=0;i<text.length;i++){const c=text[i];
 if(q){if(c==='"'){if(text[i+1]==='"'){cell+='"';i++}else q=false}else cell+=c}
 else if(c==='"')q=true;else if(c===';'){row.push(cell);cell=''}else if(c==='\\n'){row.push(cell);rows.push(row);row=[];cell=''}else if(c!=='\\r')cell+=c}
 if(cell||row.length){row.push(cell);rows.push(row)}return rows}
document.getElementById('import').addEventListener('change',function(e){const f=e.target.files[0];if(!f)return;
 f.text().then(function(t){const rows=parseCsv(t.replace(/^\\ufeff/,''));const head=rows.shift()||[];const iid=head.indexOf('ligne_id'),iv=head.indexOf('verdict'),ir=head.indexOf('remarque');
 rows.forEach(function(r){const id=r[iid];if(!id)return;S[id]={verdict:(r[iv]||'').trim(),remarque:ir>=0?(r[ir]||''):''}});save();location.reload()})});
})();
</script></body></html>
"""


def render_html(rows: Sequence[Dict[str, str]], sheet_id: str, title: str,
                intro: str, file_stem: str) -> str:
    import html as _html
    payload = json.dumps({"sheet_id": sheet_id, "file_stem": file_stem,
                          "columns": list(COLUMNS), "rows": list(rows)},
                         ensure_ascii=False).replace("</", "<\\/")
    return (_HTML.replace("__TITLE__", _html.escape(title))
            .replace("__INTRO__", intro).replace("__DATA__", payload))


INTRO_A = ("Vérification à l'aveugle des règles d'identité et de DOI du sprint "
           "patch. Chaque ligne présente deux notices (ou une notice et un DOI "
           "proposé) dans un ordre aléatoire ; rien n'indique quelle règle, "
           "ni si la paire serait fusionnée. Ouvrir les liens au besoin.")
INTRO_B = ("Second jugement partiel (environ 20 % des lignes, autre ordre). À "
           "remplir à un autre moment, sans relire la première fiche : il sert "
           "à mesurer l'accord.")


def generate(day: str, output_dir: Path, crossref: bool = False,
             force: bool = False) -> Dict[str, Path]:
    base = C.run_dir(day)
    sheets = base / C.SHEETS
    target = sheets / f"{SHEET}.csv"
    if target.exists() and not force:
        raise C.EvalError(f"fiche déjà générée : {target} (--force pour régénérer ; "
                          f"les verdicts du navigateur restent attachés aux lignes)")
    rows, meta = build_rows(output_dir, crossref=crossref)
    second = second_sample(rows)
    written = {
        "csv": C.write_text(target, rows_to_csv(rows), bom=True),
        "html": C.write_text(sheets / f"{SHEET}.html", render_html(
            rows, f"{day}-A", f"Vérification des règles, {day}", INTRO_A, SHEET)),
        "csv_b": C.write_text(sheets / f"{SHEET_B}.csv", rows_to_csv(second), bom=True),
        "html_b": C.write_text(sheets / f"{SHEET_B}.html", render_html(
            second, f"{day}-B", f"Vérification des règles, second jugement, {day}",
            INTRO_B, SHEET_B)),
        "keys": C.write_json(base / KEYS_DIR / KEYS_FILE, meta),
    }
    return written


# === Métriques ===

def _verdicts(sheet_dir: Path, stem: str) -> Dict[str, str]:
    for name in (f"{stem}_rempli.csv", f"{stem}.csv"):
        path = sheet_dir / name
        if path.exists():
            out = {}
            for row in read_sheet(path):
                v = (row.get("verdict") or "").strip()
                v = "?" if v.lower() in ("u", "?") else v
                if v in VERDICTS:
                    out[row["ligne_id"]] = v
            return out
    return {}


def cohen_kappa(pairs: Sequence[Tuple[str, str]]) -> Optional[float]:
    """Kappa de Cohen sur des verdicts binaires (1/0), « ? » exclus."""
    pairs = [(a, b) for a, b in pairs if a in ("0", "1") and b in ("0", "1")]
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    pa1 = sum(1 for a, _ in pairs if a == "1") / n
    pb1 = sum(1 for _, b in pairs if b == "1") / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe >= 1:
        return 1.0 if po == 1 else None
    return (po - pe) / (1 - pe)


def metrics(day: str) -> Dict[str, Any]:
    base = C.run_dir(day)
    meta = C.read_json(base / KEYS_DIR / KEYS_FILE)
    sheet_dir = base / C.SHEETS
    va = _verdicts(sheet_dir, SHEET)
    vb = _verdicts(sheet_dir, SHEET_B)
    by_rule: Dict[str, Dict[str, Any]] = {}
    for rid, info in meta["lines"].items():
        for rule, kind in info["rules"].items():
            if kind != "positive":
                continue
            stats = by_rule.setdefault(rule, {"lignes": 0, "1": 0, "0": 0, "?": 0,
                                              "sans_verdict": 0, "zero": [],
                                              "questions": []})
            stats["lignes"] += 1
            v = va.get(rid)
            if v is None:
                stats["sans_verdict"] += 1
            else:
                stats[v] += 1
                if v == "0":
                    stats["zero"].append(rid)
                if v == "?":
                    stats["questions"].append(rid)
    controls = {"lignes": 0, "juges_1": [], "sans_verdict": 0}
    for rid, info in meta["lines"].items():
        if all(k != "positive" for k in info["rules"].values()):
            controls["lignes"] += 1
            v = va.get(rid)
            if v is None:
                controls["sans_verdict"] += 1
            elif v == "1":
                controls["juges_1"].append(rid)
    overlap = [(va[r], vb[r]) for r in vb if r in va]
    disagreements = sorted(r for r in vb if r in va and va[r] != vb[r])
    doc = {"date": day, "regles": by_rule, "temoins": controls,
           "accord": {"lignes_doubles": len(overlap),
                      "kappa": cohen_kappa(overlap),
                      "accord_brut": (sum(1 for a, b in overlap if a == b) / len(overlap)
                                      if overlap else None),
                      "desaccords": disagreements}}
    C.write_json(base / C.REPORTS / METRICS_FILE, doc)
    return doc


# === Décision ===

def _reference_sets(rule: str) -> Tuple[bool, str]:
    """G3 : jeux témoins de déduplication, règle active."""
    from utils.deduplication import dedup_rules, deduplicate_with_report
    rules = dedup_rules(**{rule: True})
    fixtures = _ROOT / "tests" / "fixtures"
    data = C.read_json(fixtures / "dedup_pairs.json")

    def merged(p) -> int:
        items = [copy.deepcopy(p["a"]), copy.deepcopy(p["b"])]
        return len(deduplicate_with_report(items, rules=rules)[0])

    false_merges = sum(1 for p in data["negatives"] if merged(p) < 2)
    positives = sum(1 for p in data["positives"] if merged(p) == 1)
    n_pos = len(data["positives"])
    patch = C.read_json(fixtures / "dedup_pairs_patch.json")["pairs"]
    all_on = dedup_rules(True, True, True)
    patch_ok = all(len(deduplicate_with_report(copy.deepcopy(p["items"]),
                                               rules=all_on)[0]) == p["expected_notices"]
                   for p in patch)
    ok = false_merges == 0 and positives >= 52 and patch_ok
    return ok, (f"{false_merges} fausse(s) fusion(s) sur les négatives ; "
                f"{positives}/{n_pos} positives réunies ; jeu du sprint patch "
                f"{'tenu' if patch_ok else 'NON tenu'}")


def _doi_negative_set() -> Tuple[bool, str]:
    """G4 (seconde partie) : jeu négatif de résolution, règle active."""
    env = dict(os.environ, MOISSON_DOI_CHAPTER_PREFIX="1", MOISSON_NO_DOTENV="1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_opt10_negative_set.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=str(_ROOT), env=env, capture_output=True, text=True, check=False)
    tail = (proc.stdout or "").strip().splitlines()[-1:] or ["(aucune sortie)"]
    return proc.returncode == 0, f"test_opt10_negative_set.py, règle active : {tail[0]}"


def decide(day: str, doi_negative_check=None) -> Path:
    base = C.run_dir(day)
    doc = metrics(day)
    doi_negative_check = doi_negative_check or _doi_negative_set
    lines = [f"# Décision sur les règles du sprint patch ({day})", "",
             "Gate fixé dans `.claude/dev/sprint-patch.md` (PAT-09) avant tout "
             "jugement. Une règle n'est à activer que si G1 à G4 sont atteints.", ""]
    acc = doc["accord"]
    kappa = acc["kappa"]
    lines.append(f"Accord (G5) : {acc['lignes_doubles']} ligne(s) jugée(s) deux fois ; "
                 f"kappa {C.fmt(kappa)} ; désaccords : "
                 f"{', '.join(acc['desaccords']) or 'aucun'}"
                 + (" ; kappa sous 0,6 : relire les désaccords avant de conclure."
                    if kappa is not None and kappa < 0.6 else "."))
    lines.append(f"Témoins jugés « 1 » (fusions manquées, consignées sans élargir "
                 f"les règles) : {', '.join(doc['temoins']['juges_1']) or 'aucun'}.")
    lines.append("")
    lines.append("| Règle | Lignes | 1 | 0 | ? | Sans verdict | G1 | G2 | G3/G4 automatiques | Verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for rule in (RULE_CODIFFUSION, RULE_ZENODO, RULE_CHAPTERS, RULE_DOI_CHAPTER, RULE_TF):
        st = doc["regles"].get(rule)
        if not st:
            lines.append(f"| {RULE_LABELS[rule]} | 0 | | | | | | | | non évaluée (aucune ligne) |")
            continue
        judged = st["1"] + st["0"] + st["?"]
        g1 = st["0"] == 0 and judged > 0
        g2 = judged > 0 and st["?"] <= MAX_QUESTION_SHARE * judged
        if rule in DEDUP_RULES:
            g34, g34_text = _reference_sets(rule)
        elif rule == RULE_DOI_CHAPTER:
            g34, g34_text = doi_negative_check()
        else:
            g34, g34_text = True, "extraction littérale du lien (sans jeu négatif)"
        if st["sans_verdict"]:
            verdict = f"indéterminé ({st['sans_verdict']} ligne(s) sans verdict)"
        elif g1 and g2 and g34:
            verdict = "à activer"
        else:
            verdict = "ne pas activer"
        lines.append(f"| {RULE_LABELS[rule]} | {st['lignes']} | {st['1']} | {st['0']} | "
                     f"{st['?']} | {st['sans_verdict']} | {'oui' if g1 else 'non'} | "
                     f"{'oui' if g2 else 'non'} | {g34_text} | {verdict} |")
        if st["zero"]:
            lines.append(f"|  | lignes jugées « 0 » : {', '.join(st['zero'])} | | | | | | | | |")
    lines += ["", "Activer une règle : fixer son drapeau à 1 (`.env`, shell ou bloc "
              "`env` de Claude Desktop), puis relancer Claude Desktop."]
    path = C.write_text(base / C.REPORTS / DECISION_FILE, "\n".join(lines) + "\n")
    return path


# === Ligne de commande ===

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rules_check.py",
        description="Vérification humaine des règles du sprint patch (PAT-09).")
    parser.add_argument("command", choices=("generate", "metrics", "decide"))
    parser.add_argument("--date", default="patch-" + C.today(),
                        help="nom de la campagne (dossier sous output/eval/)")
    parser.add_argument("--output-dir", default=str(_ROOT / "output"),
                        help="dossier des corpus lus (lecture seule)")
    parser.add_argument("--crossref", action="store_true",
                        help="résoudre les chapitres Google Books numérotés "
                             "(réseau : CrossRef, gratuit)")
    parser.add_argument("--force", action="store_true",
                        help="régénérer une fiche existante")
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            written = generate(args.date, Path(args.output_dir),
                               crossref=args.crossref, force=args.force)
            meta = C.read_json(written["keys"])
            n = len(meta["lines"])
            print(f"{n} ligne(s) à juger → {written['html']}")
            print(f"second jugement : {written['html_b']}")
            print("lignes positives par règle : " + ", ".join(
                f"{k} {v}" for k, v in sorted(meta["counts"].items())))
            print(f"durée estimée : environ {max(1, round(n * 18 / 60))} min "
                  f"(15 à 20 s par ligne), plus environ "
                  f"{max(1, round(len(read_sheet(written['csv_b'])) * 18 / 60))} min "
                  f"pour le second jugement")
        elif args.command == "metrics":
            doc = metrics(args.date)
            print(json.dumps(doc, ensure_ascii=False, indent=1))
        else:
            print(f"décision → {decide(args.date)}")
    except C.EvalError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
