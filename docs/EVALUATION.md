# Protocole d'évaluation SHS du classement

Ce protocole décide si le **classement local** (OPT-13, `utils/ranking.py`) peut devenir le classement par défaut de `moisson_search`. Tant que ses critères ne sont pas satisfaits, le classement local reste désactivé : `MOISSON_RANKING=native` (défaut de `config.py`).

Le protocole est outillé de bout en bout. Les moissons sont faites et les fiches sont prêtes. Avant les métriques, les ensembles de candidats doivent être reconstruits hors réseau (§ 5, « Situation au 2026-09-29 », et § 7) : le module de classement a changé depuis leur construction. **Reste le jugement humain**, que rien ne remplace.

- Scripts : `scripts/eval/`.
- Données locales d'une campagne : `output/eval/<date>/`. Un `.gitignore` y exclut tout du dépôt.
- Campagne en cours : `output/eval/2026-09-29/`.

## 1. Principe

- **Des requêtes SHS réelles, en deux jeux disjoints.**
  - Le jeu de **réglage** (8 requêtes) sert à mettre au point le classement local et à comparer ses pondérations (information pour OPT-13).
  - Le jeu de **décision** (16 requêtes) est tenu à part. C'est le seul sur lequel s'applique le gate.
- **Un ensemble commun de candidats par requête.** Une moisson live est faite avec le code actuel. Chaque ordre comparé classe les mêmes notices :
  - `native` : l'ordre par défaut quand le classement local est désactivé ;
  - `local:simple` et `local:bm25` : les deux pondérations du classement local.

  Les candidats à juger sont l'union des 30 premiers de chaque ordre.
- **Un jugement en aveugle.** Les candidats sont présentés dans un ordre aléatoire à graine fixe, sans source, sans score et sans rang. Échelle :
  - 0 : hors sujet ;
  - 1 : périphérique ;
  - 2 : utile ;
  - 3 : central.

  Un second jugement partiel (20 % des candidats) mesure les désaccords.
- **Acquisition et classement sont mesurés séparément.** Un reclassement ne fait pas remonter une référence que la moisson n'a jamais récupérée. Le rappel sur les références connues distingue donc deux choses :
  - la présence dans la moisson, quel que soit le rang ;
  - la présence dans les 20 premiers de chaque ordre.

## 2. Les requêtes (`scripts/eval/queries_shs.json`)

Il y a 24 requêtes : R01 à R08 pour le réglage, D01 à D16 pour la décision. Toutes les catégories du plan sont couvertes par le jeu de décision :

- thèmes larges et thèmes rares ;
- expressions exactes (entre guillemets) ;
- auteurs homonymes : Olivier Martin, Pierre Lévy ;
- concepts FR/EN, avec variante anglaise explicite (`query_variants`) ;
- ouvrages, chapitres, articles, thèses ;
- périodes de publication restrictives : 2005-2010, jusqu'en 2015, depuis 2016, depuis 2023.

Groupes de sources :

- 16 requêtes interrogent le groupe `francophone` ;
- 8 requêtes (concepts FR/EN) interrogent le groupe `all` ;
- **Google Scholar n'est jamais interrogé.**

Chaque requête porte un **besoin d'information** rédigé pour le juge : ce qui est central, utile, périphérique ou hors sujet. C'est la référence du jugement.

**Références connues.** Il y en a 35, sur 21 requêtes. Trois requêtes en sont dépourvues (D04, D10, D12), et la raison est donnée dans `references_note`.

- Chaque référence a été choisie d'après la connaissance du domaine, jamais tirée des résultats d'une recherche équivalente à la requête : le rappel resterait sinon circulaire.
- Ses identifiants ont été vérifiés un à un le 2026-09-29, par l'API CrossRef (`/works/{doi}`) et l'API HAL (identifiant, ou auteur et titre connus).
- Les dépôts HAL d'un même travail sont déclarés comme identifiants alternatifs (champ `ids`), de même que les différentes éditions d'un ouvrage, seulement si titre, auteurs, année et type sont identiques. Un seul identifiant suffit. Un identifiant dont l'identité n'est pas établie est écarté, et l'écart est consigné dans la note de la référence (exemple : D09, DOI Cairn portant le titre du livre).
- Toutes les notices de la moisson qui portent l'un des identifiants d'une référence lui sont associées (un dépôt HAL et la notice DOI de l'éditeur restent souvent deux notices) : son rang dans un ordre est le meilleur de ces notices.

## 3. Commandes

Toutes les commandes se lancent depuis la racine du projet, avec le Python du venv. Chaque commande tient sur une ligne. `--date` désigne la campagne (défaut : aujourd'hui).

| Étape | Réseau | Commande |
|---|---|---|
| 1. Collecte des moissons | oui (API gratuites, sans clé) | `venv/bin/python scripts/eval/pool.py --date 2026-09-29 collect` |
| 2. Classements et candidats | non | `venv/bin/python scripts/eval/pool.py --date 2026-09-29 build` |
| 3. Fiches de jugement | non | `venv/bin/python scripts/eval/sheets.py --date 2026-09-29 generate` |
| 4. (facultatif) DOI des candidats | oui (CrossRef) | `venv/bin/python scripts/eval/pool.py --date 2026-09-29 resolve-dois` |
| 5. (facultatif) Fiches de vérification | non | `venv/bin/python scripts/eval/sheets.py --date 2026-09-29 fusions` puis `venv/bin/python scripts/eval/sheets.py --date 2026-09-29 dois` |
| 6. Métriques | non | `venv/bin/python scripts/eval/metrics.py --date 2026-09-29` |
| 7. Décision | non | `venv/bin/python scripts/eval/decide.py --date 2026-09-29` |
| État de la campagne | non | `venv/bin/python scripts/eval/pool.py --date 2026-09-29 status` |

Les étapes 1 à 5 ont été exécutées le 2026-09-29 :

- 24 moissons, 66 requêtes réseau (HAL 24, Isidore 24, CrossRef 9, Semantic Scholar 9), 78 s de moisson cumulée ;
- 1 098 candidats à juger : 360 en réglage, 738 en décision ;
- 227 candidats dans l'échantillon B ;
- 192 notices soumises à la résolution de DOI, 20 DOI acceptés ;
- 80 fusions à vérifier.

Options utiles :

- `collect` :
  - `--ids R01,D02` ou `--set decision` restreint les requêtes ;
  - `--force` refait une requête déjà collectée (la collecte est reprise là où elle s'est arrêtée) ;
  - `--pause 8` fixe l'espacement entre deux moissons, en secondes ;
  - `--max-per-source 100` fixe la taille des moissons ;
  - `--keys` utilise les clés des sources gratuites du `.env` ; la clé SerpAPI est toujours retirée.
- `build` :
  - `--rankers native,local` : `native`, `entrelacement`, `local` ou `local:<variante>` ;
  - `--ranker nom=paquet.module:fonction` branche un autre classeur ;
  - `--depth 30` fixe la profondeur de l'union des candidats.
- `generate` :
  - les jugements déjà présents dans `fiches/` sont reportés d'office, fiche par fiche (A sur A, B sur B, jamais A sur B) : export HTML `*_rempli.csv`, puis CSV rempli en place au tableur ;
  - un CSV rempli est copié dans `fiches/sauvegardes/` (nom horodaté) avant d'être réécrit ;
  - si des jugements du CSV en place n'ont plus de ligne dans la nouvelle fiche (`--ids`, candidats sortis de l'ensemble), rien n'est écrit : `--force` écrase malgré tout, la copie restant dans `sauvegardes/` ;
  - `--prefill autre_fiche.csv` reporte en plus, sur la fiche A seulement, des jugements rangés ailleurs.
- `fusions` et `dois` : mêmes garanties pour les verdicts (reportés d'après le contenu de la ligne, copie de sauvegarde, `--force`).
- `metrics` :
  - `--juge-a` / `--juge-b` désignent les fiches remplies (défaut : `fiches/jugement_A_rempli.csv`, sinon `fiches/jugement_A.csv`, idem pour B) ;
  - `--seuil 2` fixe le seuil de pertinence ;
  - `--gain exp|lin` choisit le gain du nDCG ;
  - `--k 20` fixe la profondeur des mesures.
- `decide` :
  - `--variante local:bm25` impose la variante comparée ; si ce n'est pas celle qu'applique `MOISSON_RANKING=local`, le verdict ne peut pas être « activer » ;
  - `--rappel-bloquant` rend bloquante toute baisse de rappel.

## 4. Les fiches à remplir (`output/eval/2026-09-29/fiches/`)

| Fichier | Contenu | Volume | Durée estimée |
|---|---|---|---|
| `jugement_A.html` (ou `.csv`) | premier jugement, tous les candidats | 1 098 notices (réglage 360, décision 738) | 6 à 7 h 30 (20 à 25 s par notice) |
| `jugement_B_echantillon.html` (ou `.csv`) | second jugement, 20 % des candidats, autre ordre | 227 notices | 1 h 15 à 1 h 30 |
| `verification_fusions.csv` / `.html` | fusions de la déduplication : 60 de la passe floue, 20 par clé forte | 80 fusions | 15 à 20 min |
| `verification_doi.csv` / `.html` | DOI attribués par appariement CrossRef | 20 DOI | 10 min |

Au total, comptez environ une journée. Ordre conseillé :

1. le réglage de la fiche A (R01 à R08, environ 2 h), qui compare les pondérations ;
2. la décision (D01 à D16, environ 4 h 30) ;
3. la fiche B, à un autre moment, sans relire A ;
4. les deux vérifications.

**Juger avec le fichier HTML (recommandé).** Il s'ouvre dans un navigateur, hors ligne, et fonctionne sans aucune ressource externe.

- À gauche, les requêtes et leur avancement.
- En tête, le besoin d'information de la requête courante.
- Pour chaque notice : titre (lien), auteurs, année, type, support, résumé tronqué.
- Au clavier, cliquez une notice puis tapez `0`, `1`, `2` ou `3` : la notice suivante prend la main. Une remarque libre est possible.
- Les jugements sont gardés dans le navigateur (stockage local). Changer de navigateur ou vider les données du site les efface : exportez régulièrement.
- « Exporter le CSV » produit `jugement_A_rempli.csv`. Placez ce fichier dans `fiches/`, où `metrics.py` le trouve seul.
- « Importer un CSV » recharge une fiche déjà remplie, pour reprendre ou changer d'ordinateur.

**Juger avec un tableur.** Ouvrez le CSV (UTF-8 avec BOM, séparateur « ; »).

- Remplissez la colonne `jugement` (0 à 3), et éventuellement `remarque`.
- Ne modifiez ni `requete_id` ni `candidat_id`.
- Enregistrez au format **CSV UTF-8** (et non « CSV » simple, qui produit du Windows-1252 ou du Mac Roman : `metrics.py` le refuse en nommant le fichier et la ligne).
- Vous pouvez remplir le CSV en place, dans `fiches/` : `metrics.py` le lit s'il n'y a pas d'export HTML, et une régénération des fiches reporte vos jugements (§7).

Une valeur illisible n'est jamais ignorée : elle est signalée nommément (fichier et ligne) dans le rapport des métriques. Une ligne qui a plus de champs que l'en-tête (un « ; » saisi sans guillemets dans un éditeur de texte) est signalée et écartée, car ses colonnes peuvent être décalées.

**Règles de jugement :**

- Jugez la notice au regard du **besoin d'information**, pas du seul libellé de la requête.
- Jugez-la sans chercher à deviner d'où elle vient. Le lien est neutre :
  - le DOI si la notice en a un ;
  - sinon le dépôt ou l'éditeur ;
  - sinon une recherche web sur le titre, quand seule une URL de moteur de découverte existait.
- Un doublon apparent (même travail sous deux notices) se juge comme l'original. Signalez-le en remarque : c'est une fusion manquée.
- En cas d'hésitation entre deux degrés, prenez le plus bas et notez la raison.

**Vérifications.** Remplissez la colonne `verdict` du CSV.

- Fusions : 1 = même travail, 0 = travaux distincts (fusion erronée), ? = incertain.
- DOI : 1 = DOI du même travail, 0 = DOI erroné, ? = incertain.

## 5. Lire le rapport

`metrics.py` écrit `rapports/metriques.md` et `metriques.json`. `decide.py` écrit `rapports/decision.md` et `decision.json`.

**Définitions** (k = 20, pertinent = jugement ≥ 2) :

- **P@20** : part de pertinents parmi les 20 premiers. Le dénominateur vaut toujours 20. Un candidat non jugé compte comme non pertinent et est dénombré.
- **nDCG@20** : DCG = Σ (2^g − 1) / log2(rang + 1). La normalisation se fait par le DCG des 20 meilleurs jugements de la requête. La mesure est indéfinie si aucun candidat n'a un jugement supérieur à 0.
- **R@20** : rappel relatif à l'ensemble jugé, c'est-à-dire la part des pertinents de l'ensemble commun présents dans les 20 premiers.
- **Références connues** :
  - acquisition : part des références présentes dans la moisson ;
  - part des références présentes dans les 20 et 30 premiers de chaque ordre.
- **Équilibre** : répartition des 20 premiers par langue (fr, en, multilingue, autre, inconnue), par type (article, livre, chapitre, thèse, communication…) et par ancienneté (avant 2000, 2000-2009, 2010-2019, 2020 et après).
- **Accord inter-juges** :
  - kappa de Cohen pondéré, quadratique et linéaire, et kappa non pondéré ;
  - accord brut et accord sur la pertinence binaire ;
  - liste des désaccords d'au moins deux degrés.
- **Fusions** :
  - rejeu du jeu témoin `tests/fixtures/dedup_pairs.json` (fausses fusions sur les paires négatives) ;
  - bilan des fusions des moissons ;
  - précision mesurée sur la fiche de vérification.
- **DOI** :
  - rejeu du jeu témoin de la résolution (`tests/unit/test_opt10_negative_set.py`, CrossRef simulé : cas négatifs, contrôles positifs, abstentions consignées) ;
  - bilan des rapports `doi/*.doi_report.json` (acceptés, rejetés, ambigus, sans candidat : la couverture est publiée à part de la précision) ;
  - précision mesurée sur la fiche de vérification.

Les moyennes sont des macro-moyennes : chaque requête pèse autant.

**Décision** (`decision.md`). La variante comparée est **celle qu'appliquerait `MOISSON_RANKING=local`** : `local:<DEFAULT_LEXICAL_MODEL>` du module `utils/ranking.py`, aujourd'hui `local:simple`. Le moteur ne transmet aucune pondération lexicale, et aucune variable ne la choisit. Le **réglage** classe les pondérations disponibles (meilleur nDCG@20) à titre d'information. Si une autre pondération l'emporte, par exemple `local:bm25`, le rapport le signale : pour l'activer, OPT-13 doit la rendre sélectionnable (ou en faire la pondération par défaut), puis il faut reconstruire les ensembles et réévaluer. Les cibles sont appliquées sur la **décision** seule, contre `native` :

| Critère | Seuil | Bloquant |
|---|---|---|
| variante évaluée = variante qu'applique `MOISSON_RANKING=local` | identique | oui (sinon indéterminé) |
| requêtes de décision comparables | au moins 16 | oui (sinon indéterminé) |
| 20 premiers de chaque ordre entièrement jugés | toutes les requêtes | oui (sinon indéterminé) |
| version du classement | une seule version (`RANKING_VERSION` et empreinte du code source) dans tous les ensembles de décision, identique au module actuel | oui (sinon indéterminé) |
| gain relatif de nDCG@20 moyen | ≥ 10 % | oui |
| P@20 moyen | sans baisse | oui |
| baisses de rappel | toutes listées : références connues sorties des 20 premiers, R@20 en baisse | non, sauf `--rappel-bloquant` ; à examiner une à une |
| fausses fusions sur les paires négatives du jeu témoin | 0 | oui |
| DOI erronés sur le jeu témoin de la résolution | 0 | oui |

Trois verdicts sont possibles :

- « activer le classement local par défaut » ;
- « ne pas activer le classement local par défaut » ;
- « indéterminé » : jugements manquants ou incomplets, classement local absent, trop peu de requêtes, variante évaluée différente de celle qu'applique `MOISSON_RANKING=local`, versions mélangées ou code du classement modifié depuis la construction des ensembles.

Un verdict « activer » nomme ce qu'il valide : variante, pondération lexicale, version et empreinte du code. Toute modification ultérieure du classement demande une nouvelle évaluation.

Un intervalle de confiance à 95 % du gain (bootstrap apparié sur les requêtes, graine fixe) est donné à titre d'information.

Le script ne modifie aucune configuration. L'activation (`MOISSON_RANKING=local`, voir OPT-13) reste une décision humaine, prise sur ce rapport.

**Situation au 2026-09-29.** Sans jugements, le verdict est « indéterminé ». Les ensembles et les fiches de la campagne ont été construits le 2026-09-29 avec `opt13-2` et portent l'empreinte du code `f1065cb9563f` (qui remplaçait l'empreinte `e9824a8a1f18` des premiers ensembles, périmée après la revue du classement). `utils/ranking.py` a été modifié depuis par OPT-17 (ajout de `match_notice` et de `citation_count` pour la consultation locale, sans changement de règle) : son empreinte n'est plus celle des ensembles, et le critère « version du classement » rend le verdict « indéterminé » tant que les ensembles n'ont pas été reconstruits. Une reconstruction faite le 2026-09-29 sur une copie de la campagne, hors de `output/` (empreinte `9775a647b6f8`), a donné des ordres et des ensembles de candidats identiques pour les 24 requêtes et des fiches identiques octet pour octet ; les métriques puis la décision recalculées sur cette copie satisfont le critère de version. Vous pouvez donc juger sur les fiches actuelles, mais **avant `metrics.py` et `decide.py`, relancer le § 7** (`pool.py … build`, puis `sheets.py … generate`, qui reporte les jugements déjà rendus). Les jeux témoins passent déjà :

- 0 fausse fusion sur 66 paires négatives ;
- 0 DOI erroné sur 33 cas négatifs ;
- 18 contrôles positifs sur 18 acceptés.

## 6. Fichiers d'une campagne (`output/eval/<date>/`)

**Ne pas ouvrir les dossiers `pools/` et `snapshots/` avant d'avoir jugé** : ils contiennent les classements et les sources.

| Chemin | Contenu |
|---|---|
| `snapshots/<id>.json` | moisson complète. Contenu : demande, plan de requête, statut de chaque source, notices de chaque source dans l'ordre servi, rangs et scores natifs, notices fusionnées, composition de chaque fusion, compteurs réseau, environnement (sans clé, révision git) |
| `journal_collecte.json` | une ligne par moisson : statut, notices, durée, requêtes par hôte |
| `pools/<id>.json` | ordre complet de chaque classeur (ou son erreur), ensemble commun de candidats, notices et champs d'affichage des candidats, clés d'identité de toutes les notices |
| `fiches/` | fiches de jugement et de vérification |
| `doi/<id>.doi_report.json` | rapports de résolution de DOI des candidats (échantillon de 8 par requête) |
| `rapports/` | `metriques.md`, `metriques.json`, `decision.md`, `decision.json` |

L'identifiant de candidat (`c` suivi de 10 caractères hexadécimaux) est une empreinte du contenu bibliographique : clés d'identité, titre, année, premier auteur et type. Il ne dit rien de la source ni du rang, et reste le même tant que la notice fusionnée ne change pas.

## 7. Relancer après une évolution du classement

Les classements se recalculent hors réseau à partir des instantanés. Après une modification d'OPT-13 (le rapport de décision la détecte : version ou empreinte du code différente de celle des ensembles) :

1. `venv/bin/python scripts/eval/pool.py --date 2026-09-29 build` ;
2. `venv/bin/python scripts/eval/sheets.py --date 2026-09-29 generate`.

Seuls les nouveaux candidats restent à juger. Les jugements déjà rendus sont reportés d'office, que vous ayez jugé au tableur (CSV en place) ou dans la page HTML (export `*_rempli.csv` dans `fiches/`). La page HTML garde aussi ses jugements dans le navigateur. Si des candidats jugés sortent de l'ensemble, `generate` s'arrête sans rien écrire et les nomme : relancez avec `--force` pour accepter leur retrait (copie dans `fiches/sauvegardes/`).

- Toute nouvelle collecte (`collect --force`) change les notices, donc les candidats. Faites-la dans une campagne à une autre date.
- Le fichier de requêtes courant fait foi pour les jeux et les références connues : une référence corrigée est prise en compte sans recollecter.

**Brancher un classeur.** L'interface commune est `rank(items, request, harvest)`. La fonction reçoit :

- les notices fusionnées ;
- la `SearchRequest` ;
- le `HarvestResult` reconstitué, dont l'attribut `eval_groups` donne la composition des fusions.

Elle rend un ordre sous forme d'indices, d'identifiants, de notices, de couples (notice, score) ou d'entrées portant `index`.

Pour OPT-13, l'adaptateur (`scripts/eval/rankers.py`) appelle `utils.ranking.rank_records(records, sources, mode=..., plan=..., groups=..., lexical_model=..., reference_year=...)`. Si le module publie un crochet `eval_rankers()` renvoyant `{nom: fonction}`, ce crochet est prioritaire. En l'absence du module, `native` retombe sur l'entrelacement des ordres natifs, et le rapport le signale.

## 8. Limites

- **Rappel relatif.** Il est calculé sur l'ensemble commun jugé : les pertinents hors de l'union des 30 premiers sont inconnus. C'est le biais habituel de la mise en commun.
- **Moissons sans clé.** OpenAlex et CORE, qui exigent une clé, sont `skipped` dans les 8 requêtes du groupe `all`. Pour les inclure, recollectez avec `--keys` dans une autre campagne.
- **Isidore et ses sous-sources.** Ils ne servent que les 200 premiers résultats d'Isidore. Les sous-sources sont souvent `partial` (fenêtre du fournisseur).
- **Semantic Scholar.** En mode bibliographie, il est classé par citations, pas par pertinence (le point d'accès classé par pertinence est inutilisable sans clé).
- **Références connues.** Elles sont peu nombreuses (35) et absentes pour trois requêtes. Plusieurs ouvrages de référence n'ont été retrouvés par aucune moisson à 100 notices par source. C'est un constat d'acquisition, pas de classement. Les écarts de « références dans les 20 premiers » entre ordres reposent sur une ou deux références : au 2026-09-29, l'écart entre `local:simple` et `native` sur le jeu de décision tient à une seule référence (D09).
- **Un seul juge principal.** Le second jugement porte sur 20 % des candidats seulement. Un kappa faible appelle à relire les besoins d'information avant de conclure.
- **Portée des résultats.** Les résultats sur échantillon ne garantissent pas l'absence d'erreur en production. Le score de classement reste un indicateur d'ordre, pas une probabilité de pertinence.

## 9. Vérification des règles d'identité et de DOI (sprint patch)

Les règles ajoutées après le retour d'expérience du 2026-09-30 restent **désactivées** tant que leur vérification humaine n'a pas passé le gate fixé d'avance (`.claude/dev/sprint-patch.md`, PAT-09) : co-diffusion d'un même article (`MOISSON_DEDUP_CODIFFUSION`), versions Zenodo (`MOISSON_DEDUP_ZENODO_VERSIONS`), chapitres Google Books en déduplication (`MOISSON_DEDUP_SCHOLAR_CHAPTERS`) et en résolution DOI (`MOISSON_DOI_CHAPTER_PREFIX`). Le DOI lu dans les liens Taylor & Francis est vérifié sur un échantillon.

**Où juger** : `output/eval/patch-2026-09-30/fiches/verification_regles.html` (63 lignes, environ 20 minutes), puis, à un autre moment et sans relire la première fiche, `verification_regles_B.html` (environ 20 % des lignes dans un autre ordre, environ 4 minutes). Page hors ligne : cliquer une ligne, taper `1`, `0` ou `?` (`u` vaut `?`) ; les verdicts restent dans le navigateur ; « Exporter le CSV » produit `verification_regles_rempli.csv` (et `verification_regles_B_rempli.csv`), à déposer dans `fiches/`. Ne pas ouvrir `cles/` avant d'avoir jugé.

**Échelle.** Paires (« même publication ? ») : **1** = même publication (même article du même numéro, même chapitre, même dépôt en deux versions) ; **0** = publications distinctes (autre texte, autre numéro ou autre revue, compte rendu, traduction, prépublication face à la version publiée) ; **?** = indécidable avec les liens. DOI (« DOI juste ? ») : **1** = le DOI désigne ce travail ; **0** = DOI erroné ; **?** = indécidable.

**À l'aveugle** : ordre aléatoire à graine fixe ; ni nom de règle, ni source, ni score ; les paires que les règles réuniraient sont mêlées à des témoins proches qu'elles écartent.

**Gate** (fixé avant tout jugement) : G1, aucune paire jugée « 0 » parmi celles qu'une règle réunit ; G2, au plus 10 % de « ? » ; G3, jeux témoins de déduplication tenus, règle active (0 fausse fusion sur les 66 négatives, positives ≥ 52/54, attendus de `tests/fixtures/dedup_pairs_patch.json`) ; G4, aucun DOI jugé « 0 » et jeu négatif de résolution à 0 DOI écrit, règle active ; G5, kappa et désaccords consignés (sous 0,6, relire les désaccords). Une règle n'est « à activer » que si G1 à G4 sont atteints ; les témoins jugés « 1 » sont consignés comme fusions manquées, sans élargir les règles.

**Calcul**, depuis la racine, une fois les fiches rendues : `venv/bin/python scripts/eval/rules_check.py metrics --date patch-2026-09-30 && venv/bin/python scripts/eval/rules_check.py decide --date patch-2026-09-30` ; le verdict règle par règle est écrit dans `output/eval/patch-2026-09-30/rapports/decision_regles.md`. Régénérer les fiches (`generate --force`, `--crossref` pour la résolution des chapitres, réseau CrossRef seulement) remplace les CSV : exporter d'abord les verdicts du navigateur.
