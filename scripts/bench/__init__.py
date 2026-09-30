"""
Banc de mesure du moissonneur (OPT-00) : aides du script
``scripts/benchmark_moisson.py``.

Modules :

- ``instrument`` : compteurs posés sous ``requests`` (``HTTPAdapter.send``),
  comptabilité des sommeils, chronométrage des étapes, garde-socket ;
- ``synth`` : univers de travaux synthétiques déterministes et rendu des
  notices au format natif de chaque API ;
- ``simulator`` : simulateur d'API installé au même point que les compteurs
  (pagination propre à chaque fournisseur, injection de pannes) ;
- ``scenarios`` : scénarios rejouables du plan (référence, international,
  requête rare, 429, source indisponible, grand corpus, etc.) ;
- ``analysis`` : analyse du corpus produit (doublons résiduels, vérité
  terrain des scénarios simulés, couverture par identifiants de travaux,
  taille du retour MCP) ;
- ``runner`` : exécution d'une mesure dans un processus isolé, contre une
  copie quelconque du code (``--tree``).

Le paquet n'importe aucun module du moissonneur au chargement : c'est le
lanceur qui place la copie mesurée en tête de ``sys.path`` puis vérifie, par
``module.__file__``, que c'est bien elle qui a été importée.
"""
