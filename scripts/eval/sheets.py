"""
Fiches de jugement en aveugle du protocole d'évaluation SHS (OPT-15).

``generate`` écrit, dans ``output/eval/<date>/fiches/`` :

- ``jugement_A.csv`` et ``jugement_A.html`` : tous les candidats de chaque
  requête (ensemble commun construit par ``pool.py build``), dans un ordre
  aléatoire à graine fixe, SANS source, score ni rang : titre, auteurs, année,
  type, support, résumé tronqué proprement, lien neutre (DOI, sinon dépôt ou
  éditeur, sinon recherche web sur le titre). L'ordre ne dépend que de la
  graine, de la requête et de l'ensemble des candidats : ni l'ordre des
  classeurs, ni la position d'un candidat dans l'un d'eux n'y transparaît ;
- ``jugement_B_echantillon.csv`` et ``.html`` : second jugement partiel
  (échantillon de 20 % des candidats de chaque requête, autre graine, autre
  ordre), pour mesurer les désaccords (kappa pondéré).

Échelle : 0 hors sujet, 1 périphérique, 2 utile, 3 central.

Le CSV (UTF-8 avec BOM, séparateur « ; ») s'ouvre dans un tableur ; la
colonne ``jugement`` reçoit 0 à 3, ``remarque`` un commentaire libre. Le
fichier HTML, autonome et lisible hors ligne, permet de juger au clavier
(touches 0 à 3), garde les jugements dans le navigateur et exporte un CSV
rempli de même format. ``--prefill`` reporte des jugements déjà rendus (après
un nouveau ``pool.py build``, seuls les nouveaux candidats restent à juger).

Relancer ``generate`` ne perd aucun jugement : ceux déjà présents dans
``fiches/`` (export HTML ``*_rempli.csv``, CSV rempli en place au tableur)
sont reportés d'office, A sur A et B sur B ; un CSV rempli est copié dans
``fiches/sauvegardes/`` avant réécriture ; si des jugements n'avaient plus de
ligne (``--ids``, candidats sortis de l'ensemble), rien n'est écrit sans
``--force``. Même garde pour les fiches de vérification (verdicts reportés
d'après le contenu de la ligne).

Commandes annexes :

- ``fusions`` : ``verification_fusions.csv/.html``, échantillon de fusions
  réalisées par la déduplication dans les moissons (60 fusions de la passe
  floue, la plus risquée, et 20 fusions par clé forte, tirées à graine
  fixe ; ``--n-floues 0`` pour les prendre toutes) ; verdict 1 (même
  travail), 0 (travaux distincts) ou ? (incertain) ;
- ``dois`` : ``verification_doi.csv/.html``, DOI acceptés par la résolution
  CrossRef des candidats (``pool.py resolve-dois``) ; verdict 1 (DOI
  correct), 0 (DOI erroné) ou ?.

Exemple ::

    venv/bin/python scripts/eval/sheets.py generate --date 2026-09-29
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval import common as C  # noqa: E402

DEFAULT_SEED = 20260929
SECOND_FRACTION = 0.20
DELIMITER = ";"

COLUMNS = ["requete_id", "requete", "besoin", "numero", "candidat_id", "titre",
           "auteurs", "annee", "type", "support", "resume", "lien",
           "jugement", "remarque"]

FUSION_COLUMNS = ["fusion_id", "requete_id", "n_notices", "notice_1", "notice_2",
                  "notice_3", "notice_4", "notice_5", "verdict", "remarque"]
DOI_COLUMNS = ["verification_id", "requete_id", "candidat_id", "titre", "auteurs",
               "annee", "type", "doi_propose", "lien_doi", "verdict", "remarque"]

SHEET_A = "jugement_A"
SHEET_B = "jugement_B_echantillon"
SHEET_FUSIONS = "verification_fusions"
SHEET_DOI = "verification_doi"


# === Lecture des ensembles de candidats ===

def load_pools(out_dir: Path, order: Optional[Sequence[str]] = None,
               ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Ensembles de candidats (``pools/*.json``), dans l'ordre du fichier de
    requêtes si ``order`` est donné, sinon par identifiant."""
    pools = {p.stem: C.read_json(p) for p in sorted((out_dir / C.POOLS).glob("*.json"))}
    if not pools:
        raise C.EvalError(f"aucun ensemble de candidats dans {out_dir / C.POOLS} "
                          f"(lancer « pool.py build »)")
    keys = [k for k in (order or []) if k in pools] + sorted(k for k in pools if k not in (order or []))
    if ids:
        wanted = {i.upper() for i in ids}
        keys = [k for k in keys if k.upper() in wanted]
    return [pools[k] for k in keys]


# === Ordre aveugle ===

def blind_order(cids: Iterable[str], seed: int, qid: str, salt: str) -> List[str]:
    """
    Ordre de présentation : identifiants triés puis mélangés par un
    générateur initialisé sur (graine, fiche, requête). Il ne dépend ni de
    l'ordre d'insertion ni d'aucun classement.
    """
    ordered = sorted(set(cids))
    random.Random(f"{seed}:{salt}:{qid}").shuffle(ordered)
    return ordered


def second_sample(cids: Iterable[str], seed: int, qid: str,
                  fraction: float = SECOND_FRACTION) -> List[str]:
    """Échantillon du second jugement : ``ceil(fraction × n)`` candidats
    (au moins un si la requête en a), tirés à graine fixe."""
    ordered = sorted(set(cids))
    if not ordered:
        return []
    k = min(len(ordered), max(1, math.ceil(fraction * len(ordered))))
    return random.Random(f"{seed}:echantillon:{qid}").sample(ordered, k)


# === Lignes des fiches ===

def judgment_rows(pools: Sequence[Dict[str, Any]], seed: int = DEFAULT_SEED,
                  salt: str = "A", fraction: Optional[float] = None,
                  prefill: Optional[Dict[Tuple[str, str], Dict[str, str]]] = None
                  ) -> List[Dict[str, str]]:
    """Lignes d'une fiche : un candidat par ligne, requête par requête."""
    rows: List[Dict[str, str]] = []
    for pool in pools:
        qid = pool["qid"]
        q = pool["query"]
        cids = list(pool["pool"])
        if fraction is not None:
            # Échantillon du second jugement, plus les candidats de la
            # requête déjà jugés en second (``prefill``) et toujours dans
            # l'ensemble : une reconstruction qui change l'échantillon ne
            # fait perdre aucune paire de jugements.
            judged = {c for (q, c) in (prefill or {}) if q == qid and c in pool["candidates"]}
            cids = sorted(set(second_sample(cids, seed, qid, fraction)) | judged)
        for n, cid in enumerate(blind_order(cids, seed, qid, salt), start=1):
            disp = pool["candidates"][cid]["display"]
            known = (prefill or {}).get((qid, cid), {})
            rows.append({
                "requete_id": qid,
                "requete": query_label(q),
                "besoin": q.get("besoin", ""),
                "numero": str(n),
                "candidat_id": cid,
                "titre": disp.get("titre", ""),
                "auteurs": disp.get("auteurs", ""),
                "annee": disp.get("annee", ""),
                "type": disp.get("type", ""),
                "support": disp.get("support", ""),
                "resume": disp.get("resume", ""),
                "lien": disp.get("lien", ""),
                "jugement": known.get("jugement", ""),
                "remarque": known.get("remarque", ""),
            })
    return rows


def query_label(q: Dict[str, Any]) -> str:
    """Requête telle que le juge doit la lire (variante et dates comprises)."""
    label = q.get("query", "")
    variants = q.get("query_variants") or {}
    if variants.get("en"):
        label += f" (variante anglaise : {variants['en']})"
    lo, hi = q.get("from_date"), q.get("to_date")
    if lo and hi:
        label += f" — publications de {lo} à {hi}"
    elif lo:
        label += f" — publications depuis {lo}"
    elif hi:
        label += f" — publications jusqu'en {hi}"
    return label


# === CSV ===

def rows_to_csv(rows: Sequence[Dict[str, str]], columns: Sequence[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), delimiter=DELIMITER,
                            quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _cell(row.get(c, "")) for c in columns})
    return buf.getvalue()


def _cell(value: Any) -> str:
    """Valeur de cellule telle qu'écrite dans le CSV (sauts de ligne aplatis)."""
    return str(value or "").replace("\r", " ").replace("\n", " ")


def read_sheet_rows(path: Path, errors: Optional[List[str]] = None
                    ) -> List[Tuple[int, Dict[str, str]]]:
    """
    Lit une fiche CSV remplie (tableur ou export HTML) : BOM toléré,
    séparateur détecté (« ; », « , » ou tabulation), en-têtes insensibles à
    la casse et aux espaces. Renvoie ``(ligne, valeurs)`` pour chaque ligne,
    ``ligne`` étant le numéro de ligne du fichier (en-tête = 1).

    Jamais de trace Python ni de valeur ignorée en silence :

    - fichier absent ou illisible, encodage autre qu'UTF-8 (format « CSV »
      simple d'Excel : Windows-1252 ou Mac Roman) : ``EvalError`` nommant
      le fichier et la ligne ;
    - ligne ayant plus de champs que l'en-tête (séparateur saisi sans
      guillemets : colonnes possiblement décalées) : erreur nommée ajoutée à
      ``errors`` et ligne écartée ; sans liste ``errors``, ``EvalError``.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise C.EvalError(f"fiche illisible : {path} ({exc.strerror or exc})") from None
    try:
        text = raw.decode("utf-8-sig") if raw else ""
    except UnicodeDecodeError as exc:
        line = raw[:exc.start].count(b"\n") + 1
        raise C.EvalError(
            f"{path.name} ligne {line} : fichier non encodé en UTF-8 (octet "
            f"0x{raw[exc.start]:02x}) ; réenregistrer la fiche au format « CSV UTF-8 » "
            f"(le format « CSV » simple du tableur produit du Windows-1252 ou du "
            f"Mac Roman)") from None
    first = text.splitlines()[0] if text else ""
    delim = max((DELIMITER, ",", "\t"), key=first.count) if first else DELIMITER
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    out: List[Tuple[int, Dict[str, str]]] = []
    for row in reader:
        if None in row:
            extra = row.get(None) or []
            message = (f"{path.name} ligne {reader.line_num} : {len(extra)} champ(s) de plus "
                       f"que l'en-tête (séparateur « {delim} » saisi sans guillemets ?) ; "
                       f"ligne écartée, à corriger")
            if errors is None:
                raise C.EvalError(message)
            errors.append(message)
            continue
        out.append((reader.line_num,
                    {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}))
    return out


def read_sheet(path: Path) -> List[Dict[str, str]]:
    """Lignes d'une fiche CSV (voir ``read_sheet_rows`` ; toute ligne
    malformée lève ``EvalError``)."""
    return [row for _line, row in read_sheet_rows(path)]


def read_prefill(paths: Sequence[Path]) -> Dict[Tuple[str, str], Dict[str, str]]:
    """Jugements déjà rendus, par (requête, candidat). Un fichier absent,
    non UTF-8 ou malformé lève ``EvalError`` (jamais de report partiel)."""
    known: Dict[Tuple[str, str], Dict[str, str]] = {}
    for path in paths:
        for row in read_sheet(path):
            qid, cid = row.get("requete_id", ""), row.get("candidat_id", "")
            if qid and cid and row.get("jugement", "") != "":
                known[(qid, cid)] = {"jugement": row.get("jugement", ""),
                                     "remarque": row.get("remarque", "")}
    return known


# === HTML autonome ===

_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5f5d57;--card:#ffffff;--line:#dcd8cf;
--accent:#1f5f8b;--done:#2e7d4f;--warn:#9a5b00;--sel:#e8f0f7}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a8a59c;
--card:#201f1c;--line:#3a3833;--accent:#7db3db;--done:#6fcf97;--warn:#f2b35b;--sel:#233340}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:16px;border-bottom:1px solid var(--line)}h1{font-size:1.25rem;margin:0 0 6px}
main{display:grid;grid-template-columns:260px 1fr;gap:0;min-height:80vh}
nav{border-right:1px solid var(--line);padding:12px;overflow:auto;max-height:100vh;position:sticky;top:0}
nav button{display:flex;justify-content:space-between;width:100%;text-align:left;border:0;
background:none;color:var(--fg);padding:6px 8px;border-radius:6px;cursor:pointer;font:inherit}
nav button.cur{background:var(--sel)}nav .cnt{color:var(--muted);font-variant-numeric:tabular-nums}
nav .full .cnt{color:var(--done)}section{padding:16px;max-width:980px}
.need{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px}
.need h2{margin:0 0 6px;font-size:1.1rem}.card{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px;margin:10px 0}.card:focus{outline:2px solid var(--accent)}
.card.judged{border-left:5px solid var(--done)}.meta{color:var(--muted);font-size:.92rem}
.title{font-weight:600}.title a{color:var(--accent)}.abs{margin:6px 0;font-size:.95rem}
.grades{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.grades label{border:1px solid var(--line);
border-radius:6px;padding:3px 8px;cursor:pointer}.grades input{margin-right:4px}
.note{width:100%;margin-top:6px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg);font:inherit}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.bar button,.bar label.file{border:1px solid var(--line);
background:var(--card);color:var(--fg);border-radius:6px;padding:5px 10px;cursor:pointer;font:inherit}
.help{color:var(--muted);font-size:.9rem}.warn{color:var(--warn)}
@media (max-width:760px){main{grid-template-columns:1fr}nav{position:static;max-height:none;
border-right:0;border-bottom:1px solid var(--line)}section{padding:16px}}
"""

_JS = r"""
(function(){
const DATA=JSON.parse(document.getElementById('sheet-data').textContent);
const COLS=DATA.columns, ROWS=DATA.rows, KEY='moisson-eval:'+DATA.sheet_id;
let store={};try{store=JSON.parse(localStorage.getItem(KEY)||'{}')||{};}catch(e){store={};}
ROWS.forEach(r=>{const k=r.requete_id+'|'+r.candidat_id;if(!(k in store)&&r.jugement!==''){store[k]={j:r.jugement,r:r.remarque||''};}});
function save(){try{localStorage.setItem(KEY,JSON.stringify(store));}catch(e){}}
const byQ={};const order=[];ROWS.forEach(r=>{if(!byQ[r.requete_id]){byQ[r.requete_id]=[];order.push(r.requete_id);}byQ[r.requete_id].push(r);});
let cur=order[0];const nav=document.getElementById('nav'),sec=document.getElementById('sec'),prog=document.getElementById('prog');
function judged(q){return byQ[q].filter(r=>store[r.requete_id+'|'+r.candidat_id]&&store[r.requete_id+'|'+r.candidat_id].j!=='').length;}
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;if(txt!==undefined)e.textContent=txt;return e;}
function drawNav(){nav.textContent='';let tot=0,done=0;order.forEach(q=>{const n=byQ[q].length,d=judged(q);tot+=n;done+=d;
const b=el('button',(q===cur?'cur ':'')+(d===n?'full':''));b.appendChild(el('span','',q));b.appendChild(el('span','cnt',d+'/'+n));
b.onclick=()=>{cur=q;draw();window.scrollTo(0,0);};nav.appendChild(b);});prog.textContent=done+' / '+tot+' candidats jugés';}
function draw(){drawNav();sec.textContent='';const rows=byQ[cur];const need=el('div','need');
need.appendChild(el('h2','',cur+' : '+rows[0].requete));need.appendChild(el('div','',rows[0].besoin));sec.appendChild(need);
rows.forEach((r,i)=>{const k=r.requete_id+'|'+r.candidat_id;const s=store[k]||{j:'',r:''};
const c=el('div','card'+(s.j!==''?' judged':''));c.tabIndex=0;c.dataset.key=k;
const t=el('div','title');t.appendChild(document.createTextNode(r.numero+'. '));
if(r.lien){const a=el('a','',r.titre);a.href=r.lien;a.target='_blank';a.rel='noopener noreferrer';t.appendChild(a);}else{t.appendChild(document.createTextNode(r.titre));}
c.appendChild(t);c.appendChild(el('div','meta',[r.auteurs,r.annee,r.type,r.support].filter(Boolean).join(' · ')));
if(r.resume)c.appendChild(el('div','abs',r.resume));else c.appendChild(el('div','abs help','(pas de résumé)'));
const g=el('div','grades');[['0','0 hors sujet'],['1','1 périphérique'],['2','2 utile'],['3','3 central']].forEach(([v,lab])=>{
const l=el('label');const inp=document.createElement('input');inp.type='radio';inp.name='g'+i;inp.value=v;inp.checked=(s.j===v);
inp.onchange=()=>{set(k,v);c.classList.add('judged');};l.appendChild(inp);l.appendChild(document.createTextNode(lab));g.appendChild(l);});
c.appendChild(g);const note=el('input','note');note.placeholder='remarque (facultative)';note.value=s.r||'';
note.onchange=()=>{const o=store[k]||{j:'',r:''};o.r=note.value;store[k]=o;save();};c.appendChild(note);
c.addEventListener('keydown',ev=>{if(ev.target!==c)return;if('0123'.includes(ev.key)&&ev.key.length===1){set(k,ev.key);
c.querySelectorAll('input[type=radio]').forEach(x=>x.checked=(x.value===ev.key));c.classList.add('judged');
const nx=c.nextElementSibling;if(nx&&nx.classList.contains('card'))nx.focus();ev.preventDefault();}});sec.appendChild(c);});}
function set(k,v){const o=store[k]||{j:'',r:''};o.j=v;store[k]=o;save();drawNav();}
document.getElementById('export').onclick=()=>{const out=ROWS.map(r=>{const s=store[r.requete_id+'|'+r.candidat_id]||{j:'',r:''};
return Object.assign({},r,{jugement:s.j||'',remarque:s.r||''});});
const blob=new Blob([exportCsv(COLS,out)],{type:'text/csv;charset=utf-8'});const a=document.createElement('a');
a.href=URL.createObjectURL(blob);a.download=(DATA.file_stem||DATA.sheet_id)+'_rempli.csv';document.body.appendChild(a);a.click();a.remove();};
document.getElementById('next').onclick=()=>{for(const q of order){const r=byQ[q].find(x=>!(store[x.requete_id+'|'+x.candidat_id]||{}).j);
if(r){cur=q;draw();const c=[...document.querySelectorAll('.card')].find(x=>x.dataset.key===r.requete_id+'|'+r.candidat_id);if(c){c.focus();c.scrollIntoView({block:'center'});}return;}}alert('Tous les candidats sont jugés.');};
document.getElementById('import').onchange=ev=>{const f=ev.target.files[0];if(!f)return;const rd=new FileReader();rd.onload=()=>{
const txt=String(rd.result).replace(/^﻿/,'');const rows=parseCsv(txt);if(!rows.length)return;const h=rows[0].map(x=>x.trim().toLowerCase());
const qi=h.indexOf('requete_id'),ci=h.indexOf('candidat_id'),ji=h.indexOf('jugement'),ri=h.indexOf('remarque');let n=0;
rows.slice(1).forEach(r=>{if(qi<0||ci<0||ji<0)return;const j=(r[ji]||'').trim();if(j===''||!'0123'.includes(j)||j.length!==1)return;
store[r[qi]+'|'+r[ci]]={j:j,r:ri>=0?(r[ri]||''):''};n++;});save();draw();alert(n+' jugement(s) importé(s).');};rd.readAsText(f,'utf-8');};
draw();})();
"""

# Fonctions CSV pures de la fiche HTML (export et import), isolées pour être
# testées hors navigateur : même format que ``rows_to_csv`` (UTF-8 avec BOM,
# « ; », guillemets doublés, CRLF).
_JS_CSV = r"""
function csvCell(v){v=String(v==null?'':v);return /[;"\r\n]/.test(v)||/^\s|\s$/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
function exportCsv(cols,rows){const lines=[cols.map(csvCell).join(';')];
rows.forEach(o=>lines.push(cols.map(c=>csvCell(o[c])).join(';')));return '\ufeff'+lines.join('\r\n')+'\r\n';}
function parseCsv(t){const d=(t.split(/\r?\n/)[0].match(/;/g)||[]).length>=(t.split(/\r?\n/)[0].match(/,/g)||[]).length?';':',';
const out=[];let row=[],cell='',q=false;for(let i=0;i<t.length;i++){const ch=t[i];if(q){if(ch==='"'){if(t[i+1]==='"'){cell+='"';i++;}else q=false;}else cell+=ch;}
else if(ch==='"')q=true;else if(ch===d){row.push(cell);cell='';}else if(ch==='\n'){row.push(cell.replace(/\r$/,''));out.push(row);row=[];cell='';}else cell+=ch;}
if(cell!==''||row.length){row.push(cell);out.push(row);}return out;}
"""


def render_sheet_html(rows: Sequence[Dict[str, str]], sheet_id: str, title: str,
                      intro: str, file_stem: Optional[str] = None) -> str:
    """
    Fiche HTML autonome (aucune ressource externe) : mêmes champs que le
    CSV, jugement au clavier, sauvegarde locale (clé ``sheet_id``, propre à
    la campagne), export CSV rempli (``<file_stem>_rempli.csv``, nom attendu
    par ``metrics.py``).
    """
    payload = json.dumps({"sheet_id": sheet_id, "file_stem": file_stem or sheet_id,
                          "columns": COLUMNS, "rows": list(rows)},
                         ensure_ascii=False).replace("</", "<\\/")
    scale = "".join(f"<li><b>{k}</b> : {v}</li>" for k, v in C.SCALE.items())
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{_CSS}</style></head>
<body><header><h1>{_esc(title)}</h1>
<div class="help">{intro}</div>
<ul class="help">{scale}</ul>
<div class="help">Juger chaque notice d'après le besoin d'information de la requête (encadré), le titre, le résumé et, au besoin, le lien. Au clavier : cliquer une notice puis taper 0, 1, 2 ou 3 (passage automatique à la suivante). Les jugements sont gardés dans ce navigateur ; « Exporter le CSV » produit le fichier à rendre.</div>
<div class="bar"><button id="next" type="button">Aller au prochain non jugé</button>
<button id="export" type="button">Exporter le CSV</button>
<label class="file">Importer un CSV <input id="import" type="file" accept=".csv,text/csv" hidden></label>
<span id="prog" class="help"></span></div></header>
<main><nav id="nav" aria-label="Requêtes"></nav><section id="sec"></section></main>
<script type="application/json" id="sheet-data">{payload}</script>
<script>{_JS_CSV}{_JS}</script></body></html>
"""


def _esc(text: str) -> str:
    import html
    return html.escape(str(text), quote=True)


def _table_html(rows: Sequence[Dict[str, str]], columns: Sequence[str], title: str,
                intro: str) -> str:
    """Tableau HTML autonome en lecture (fiches de vérification)."""
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(
        f"<td>{_link(row.get(c, ''))}</td>" for c in columns) + "</tr>" for row in rows)
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{_CSS}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}td,th{{border:1px solid var(--line);
padding:4px 6px;vertical-align:top;text-align:left}}.wrap{{overflow-x:auto;padding:16px}}</style></head>
<body><header><h1>{_esc(title)}</h1><div class="help">{intro}</div></header>
<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></body></html>
"""


def _link(value: str) -> str:
    value = str(value or "")
    if value.startswith(("http://", "https://")) and " " not in value:
        return f'<a href="{_esc(value)}" target="_blank" rel="noopener noreferrer">{_esc(value)}</a>'
    return _esc(value)


# === Génération ===

INTRO_A = ("Fiche de jugement en aveugle (premier jugement). Les notices sont "
           "présentées dans un ordre aléatoire, sans source, sans score et sans "
           "rang ; aucun indice ne dit quel classement les a retenues.")
INTRO_B = ("Second jugement partiel : échantillon de 20 % des candidats, dans un "
           "autre ordre. À remplir sans consulter le premier jugement (par une "
           "autre personne, ou par la même après un délai) : il sert à mesurer "
           "les désaccords.")


BACKUPS = "sauvegardes"


def backup_sheet(path: Path) -> Path:
    """Copie horodatée d'une fiche avant réécriture
    (``fiches/sauvegardes/<nom>_<AAAAMMJJ-HHMMSS>.csv``)."""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = path.parent / BACKUPS
    dest = dest_dir / f"{path.stem}_{stamp}{path.suffix}"
    n = 2
    while dest.exists():
        dest = dest_dir / f"{path.stem}_{stamp}-{n}{path.suffix}"
        n += 1
    C.ensure_writable(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def _guard_overwrite(path: Path, lost: Sequence[str], force: bool, what: str) -> None:
    """Refuse de réécrire ``path`` si des saisies qu'il contient n'auraient
    plus de ligne dans la nouvelle fiche (sauf ``force`` ; une copie est
    gardée dans tous les cas)."""
    if lost and not force:
        shown = ", ".join(lost[:8]) + (f" (+{len(lost) - 8})" if len(lost) > 8 else "")
        raise C.EvalError(
            f"{path.name} contient {len(lost)} {what} qui ne figureraient plus dans la "
            f"nouvelle fiche ({shown}) : requêtes non retenues (--ids) ou candidats sortis "
            f"de l'ensemble après reconstruction. Rien n'a été écrit. Relancer avec --force "
            f"pour écraser malgré tout (une copie horodatée sera gardée dans "
            f"{C.SHEETS}/{BACKUPS}/).")


def _carry(sources: Sequence[Path]) -> Tuple[Dict[Tuple[str, str], Dict[str, str]], int]:
    """Jugements à reporter, par ordre de priorité décroissante des fichiers
    (le premier qui juge un candidat l'emporte) ; renvoie aussi le nombre
    de jugements contradictoires écartés."""
    merged: Dict[Tuple[str, str], Dict[str, str]] = {}
    conflicts = 0
    for path in sources:
        for key, value in read_prefill([path]).items():
            if key not in merged:
                merged[key] = value
            elif merged[key]["jugement"] != value["jugement"]:
                conflicts += 1
    return merged, conflicts


def generate(out_dir: Path, pools: Sequence[Dict[str, Any]], seed: int = DEFAULT_SEED,
             fraction: float = SECOND_FRACTION, prefill_paths: Sequence[Path] = (),
             force: bool = False) -> Dict[str, Path]:
    """
    Écrit les fiches A (complète) et B (échantillon) en CSV et HTML.

    Aucun jugement déjà saisi n'est perdu :

    - les jugements présents dans ``fiches/`` sont reportés d'office, fiche
      par fiche (A sur A, B sur B, jamais A sur B) : export HTML
      ``<fiche>_rempli.csv`` d'abord (même priorité que ``metrics.py``),
      puis le CSV rempli en place au tableur ; ``prefill_paths`` (fiche A
      seulement) passe avant eux ;
    - un CSV contenant des jugements est copié dans ``fiches/sauvegardes/``
      avant d'être réécrit ;
    - si des jugements du CSV en place n'avaient plus de ligne (``--ids``,
      candidats sortis de l'ensemble), rien n'est écrit sans ``force``.
    """
    sheets_dir = out_dir / C.SHEETS
    for path in prefill_paths:
        if not Path(path).exists():
            raise C.EvalError(f"fiche à reporter introuvable : {path}")
    in_place = {name: sheets_dir / f"{name}.csv" for name in (SHEET_A, SHEET_B)}
    exports = {name: sheets_dir / f"{name}_rempli.csv" for name in (SHEET_A, SHEET_B)}
    own = {name: (read_prefill([in_place[name]]) if in_place[name].exists() else {})
           for name in (SHEET_A, SHEET_B)}
    sources = {name: [p for p in [*(prefill_paths if name == SHEET_A else ()), exports[name],
                                  in_place[name]] if Path(p).exists()]
               for name in (SHEET_A, SHEET_B)}
    carried = {name: _carry(sources[name]) for name in (SHEET_A, SHEET_B)}
    rows = {SHEET_A: judgment_rows(pools, seed, "A", None, carried[SHEET_A][0]),
            SHEET_B: judgment_rows(pools, seed, "B", fraction, carried[SHEET_B][0])}
    for name in (SHEET_A, SHEET_B):
        present = {(r["requete_id"], r["candidat_id"]) for r in rows[name]}
        lost = sorted(f"{q}/{c}" for (q, c) in own[name] if (q, c) not in present)
        _guard_overwrite(in_place[name], lost, force, "jugement(s)")
    backups = [backup_sheet(in_place[name]) for name in (SHEET_A, SHEET_B) if own[name]]
    stamp = out_dir.name
    written = {}
    for name, title, intro in (
            (SHEET_A, f"Jugement de pertinence A — campagne {stamp}", INTRO_A),
            (SHEET_B, f"Jugement de pertinence B (échantillon) — campagne {stamp}", INTRO_B)):
        written[name + ".csv"] = C.write_text(sheets_dir / f"{name}.csv",
                                              rows_to_csv(rows[name], COLUMNS), bom=True)
        written[name + ".html"] = C.write_text(
            sheets_dir / f"{name}.html",
            render_sheet_html(rows[name], f"{stamp}-{name}", title, intro, name))
    n_pre = {name: sum(1 for r in rows[name] if r["jugement"] != "") for name in rows}
    print(f"fiche A : {len(rows[SHEET_A])} candidats ({n_pre[SHEET_A]} déjà jugés) ; "
          f"fiche B : {len(rows[SHEET_B])} candidats ({n_pre[SHEET_B]} déjà jugés) ; "
          f"{len(pools)} requêtes → {sheets_dir}")
    for name in (SHEET_A, SHEET_B):
        if carried[name][1]:
            print(f"attention : {carried[name][1]} jugement(s) contradictoire(s) entre les "
                  f"fichiers reportés dans {name} ({', '.join(p.name for p in sources[name])}, "
                  f"par priorité décroissante) : le premier l'emporte")
        dropped = len(carried[name][0]) - n_pre[name]
        if dropped > 0:
            print(f"{name} : {dropped} jugement(s) reporté(s) sans ligne dans la nouvelle "
                  f"fiche (candidats hors des ensembles courants)")
    for path in backups:
        print(f"copie de sauvegarde : {path}")
    return written


def notice_summary(item: Dict[str, Any]) -> str:
    disp = C.display_fields(item, abstract_chars=0)
    from utils.identity import extract_doi
    doi = extract_doi(item)
    parts = [disp["titre"], disp["auteurs"], disp["annee"], disp["type"], disp["support"],
             f"DOI {doi}" if doi else "", disp["lien"] if not doi else ""]
    return " — ".join(p for p in parts if p)


def fusion_rows(out_dir: Path, n_strong: int = 20, seed: int = DEFAULT_SEED,
                n_fuzzy: int = 60) -> List[Dict[str, str]]:
    """
    Fusions à vérifier, sur toutes les moissons de la campagne : ``n_fuzzy``
    fusions de la passe floue (appariement sans clé commune, le plus risqué ;
    0 : toutes) et ``n_strong`` fusions par clé forte, tirées à graine fixe.
    """
    from utils.deduplication import parse_evidence

    fuzzy, strong = [], []
    for path in sorted((out_dir / C.SNAPSHOTS).glob("*.json")):
        snap = C.read_json(path)
        groups = snap.get("groups") or []
        items = snap.get("items") or []
        sources = snap.get("sources") or []
        for g, members in enumerate(groups):
            if len(members) < 2:
                continue
            ev = parse_evidence(items[g]) or {}
            notices = [sources[s]["items"][p] for s, p in members]
            entry = {"qid": snap["query"]["id"], "index": g, "notices": notices,
                     "kind": "floue" if ev.get("fuzzy") else "forte"}
            (fuzzy if ev.get("fuzzy") else strong).append(entry)
    rng = random.Random(f"{seed}:fusions")
    if n_fuzzy and n_fuzzy < len(fuzzy):
        fuzzy = rng.sample(fuzzy, n_fuzzy)
    chosen = fuzzy + (rng.sample(strong, min(n_strong, len(strong))) if strong else [])
    rng.shuffle(chosen)
    rows = []
    for n, entry in enumerate(chosen, start=1):
        row = {"fusion_id": f"F{n:03d}", "requete_id": entry["qid"],
               "n_notices": str(len(entry["notices"])), "verdict": "", "remarque": ""}
        for k, item in enumerate(entry["notices"][:5], start=1):
            row[f"notice_{k}"] = notice_summary(item)
        if len(entry["notices"]) > 5:
            row["notice_5"] += f" (+{len(entry['notices']) - 5} autres)"
        row["_kind"] = entry["kind"]
        rows.append(row)
    return rows


def doi_rows(out_dir: Path) -> List[Dict[str, str]]:
    """DOI acceptés par la résolution CrossRef des candidats, à vérifier."""
    rows = []
    for path in sorted((out_dir / C.DOI_DIR).glob("*.doi_report.json")):
        doc = C.read_json(path)
        ev = doc.get("eval") or {}
        cids = ev.get("cids") or []
        for entry in doc.get("entries") or []:
            if entry.get("status") != "accepte" or not entry.get("doi"):
                continue
            idx = entry.get("index")
            cid = cids[idx] if isinstance(idx, int) and idx < len(cids) else ""
            rows.append({"requete_id": ev.get("qid", ""), "candidat_id": cid,
                         "titre": entry.get("title", ""),
                         "auteurs": entry.get("first_author") or "",
                         "annee": str(entry.get("year") or ""),
                         "type": entry.get("doi_type") or "",
                         "doi_propose": entry["doi"],
                         "lien_doi": f"https://doi.org/{entry['doi']}",
                         "verdict": "", "remarque": ""})
    for n, row in enumerate(rows, start=1):
        row["verification_id"] = f"V{n:03d}"
    return rows


FUSION_KEY = ("requete_id", "n_notices", "notice_1", "notice_2", "notice_3", "notice_4",
              "notice_5")
DOI_KEY = ("requete_id", "candidat_id", "doi_propose")


def carry_verdicts(path: Path, rows: List[Dict[str, str]], key_cols: Sequence[str],
                   force: bool = False) -> Optional[Path]:
    """
    Reporte sur ``rows`` les verdicts déjà saisis dans la fiche de
    vérification ``path`` (appariement sur le contenu de la ligne, jamais sur
    le numéro, qui change si l'échantillon change). Refuse d'écraser (sauf
    ``force``) si un verdict saisi n'a plus de ligne ; copie la fiche remplie
    dans ``fiches/sauvegardes/`` avant réécriture et renvoie cette copie.
    """
    if not path.exists():
        return None
    done = {}
    for _line, row in read_sheet_rows(path):
        if row.get("verdict", "") != "" or row.get("remarque", "") != "":
            done[tuple(row.get(c, "") for c in key_cols)] = row
    if not done:
        return None
    present = set()
    for row in rows:
        # Même normalisation que l'écriture (``rows_to_csv``) puis la relecture.
        key = tuple(_cell(row.get(c, "")).strip() for c in key_cols)
        present.add(key)
        if key in done:
            row["verdict"] = done[key].get("verdict", "")
            row["remarque"] = done[key].get("remarque", "")
    lost = sorted(done[k].get(key_cols[0], "") + "/" + (done[k].get("fusion_id")
                  or done[k].get("verification_id") or "?") for k in done if k not in present)
    _guard_overwrite(path, lost, force, "verdict(s)")
    return backup_sheet(path)


def write_fusions(out_dir: Path, n_strong: int, seed: int, n_fuzzy: int = 60,
                  force: bool = False) -> Path:
    rows = fusion_rows(out_dir, n_strong, seed, n_fuzzy)
    sheets_dir = out_dir / C.SHEETS
    backup = carry_verdicts(sheets_dir / f"{SHEET_FUSIONS}.csv", rows, FUSION_KEY, force)
    kinds = {r["fusion_id"]: r.pop("_kind") for r in rows}
    C.write_json(sheets_dir / f"{SHEET_FUSIONS}_nature.json", kinds)
    C.write_text(sheets_dir / f"{SHEET_FUSIONS}.csv", rows_to_csv(rows, FUSION_COLUMNS), bom=True)
    C.write_text(sheets_dir / f"{SHEET_FUSIONS}.html", _table_html(
        rows, FUSION_COLUMNS, f"Vérification des fusions — campagne {out_dir.name}",
        "Chaque ligne réunit des notices fusionnées par la déduplication. Verdict : "
        "1 = même travail, 0 = travaux distincts (fusion erronée), ? = incertain. "
        "Remplir la colonne « verdict » du CSV."))
    n_fuzzy = sum(1 for v in kinds.values() if v == "floue")
    n_done = sum(1 for r in rows if r.get("verdict"))
    print(f"fusions à vérifier : {len(rows)} ({n_fuzzy} de la passe floue ; {n_done} verdict(s) "
          f"reporté(s)) → {sheets_dir}" + (f" ; copie de sauvegarde : {backup}" if backup else ""))
    return sheets_dir / f"{SHEET_FUSIONS}.csv"


def write_dois(out_dir: Path, force: bool = False) -> Optional[Path]:
    rows = doi_rows(out_dir)
    sheets_dir = out_dir / C.SHEETS
    if not rows:
        print("aucun DOI accepté à vérifier (lancer « pool.py resolve-dois »)")
        return None
    backup = carry_verdicts(sheets_dir / f"{SHEET_DOI}.csv", rows, DOI_KEY, force)
    C.write_text(sheets_dir / f"{SHEET_DOI}.csv", rows_to_csv(rows, DOI_COLUMNS), bom=True)
    C.write_text(sheets_dir / f"{SHEET_DOI}.html", _table_html(
        rows, DOI_COLUMNS, f"Vérification des DOI — campagne {out_dir.name}",
        "DOI attribués par appariement CrossRef à des candidats qui n'en avaient pas. "
        "Ouvrir le lien : verdict 1 = DOI du même travail, 0 = DOI erroné, "
        "? = incertain. Remplir la colonne « verdict » du CSV."))
    n_done = sum(1 for r in rows if r.get("verdict"))
    print(f"DOI à vérifier : {len(rows)} ({n_done} verdict(s) reporté(s)) → {sheets_dir}"
          + (f" ; copie de sauvegarde : {backup}" if backup else ""))
    return sheets_dir / f"{SHEET_DOI}.csv"


# === Ligne de commande ===

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fiches de jugement en aveugle (OPT-15).")
    parser.add_argument("--date", default=None, help="campagne AAAA-MM-JJ (défaut : aujourd'hui)")
    parser.add_argument("--root", default=None, help="racine des campagnes (défaut : output/eval)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sub = parser.add_subparsers(dest="command", required=True)
    p_gen = sub.add_parser("generate", help="fiches A (complète) et B (échantillon)")
    p_gen.add_argument("--fraction", type=float, default=SECOND_FRACTION)
    p_gen.add_argument("--prefill", action="append", default=[],
                       help="CSV de jugements déjà rendus à reporter (répétable)")
    p_gen.add_argument("--ids", default=None)
    p_gen.add_argument("--force", action="store_true",
                       help="écraser même si des jugements saisis n'ont plus de ligne "
                            "(copie gardée dans fiches/sauvegardes/)")
    p_fus = sub.add_parser("fusions", help="fiche de vérification des fusions")
    p_fus.add_argument("--n-fortes", dest="n_strong", type=int, default=20,
                       help="fusions par clé forte tirées")
    p_fus.add_argument("--n-floues", dest="n_fuzzy", type=int, default=60,
                       help="fusions de la passe floue tirées (0 : toutes)")
    p_fus.add_argument("--force", action="store_true",
                       help="écraser même si des verdicts saisis n'ont plus de ligne")
    p_doi = sub.add_parser("dois", help="fiche de vérification des DOI acceptés")
    p_doi.add_argument("--force", action="store_true",
                       help="écraser même si des verdicts saisis n'ont plus de ligne")
    args = parser.parse_args(argv)
    try:
        out_dir = C.ensure_writable(C.run_dir(args.date, Path(args.root) if args.root else None))
        if args.command == "generate":
            if not 0 < args.fraction <= 1:
                raise C.EvalError("--fraction doit être dans ]0, 1]")
            doc = C.load_queries(strict=False)
            order = [q["id"] for q in doc["queries"]]
            pools = load_pools(out_dir, order, C.parse_id_list(args.ids))
            generate(out_dir, pools, args.seed, args.fraction,
                     [Path(p) for p in args.prefill], force=args.force)
        elif args.command == "fusions":
            write_fusions(out_dir, args.n_strong, args.seed, args.n_fuzzy, force=args.force)
        elif args.command == "dois":
            write_dois(out_dir, force=args.force)
    except C.EvalError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
