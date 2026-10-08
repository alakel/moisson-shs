"""
Menu de sélection des clients MCP (``moisson-shs setup`` sans ``--client``,
en terminal). Fonctions pures (rendu, lecture d'une réponse) et dialogue à
entrée injectable, pour les tests.

Réponses : Entrée = valider la sélection ; numéros (« 3 4 » ou « 3,4 ») =
cocher ou décocher ; « tous », « aucun » ; « q » = quitter sans rien faire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional

HELP = ("Entrée = valider · numéros (ex. « 3 4 ») = cocher ou décocher · "
        "tous · aucun · q = quitter")


@dataclass
class Row:
    id: str
    label: str
    path: str
    state: str
    checked: bool


def render_menu(rows: List[Row], title: str = "Clients MCP") -> str:
    lines = [f"{title} (cochés : ceux qui seront traités) :"]
    width = max((len(r.label) for r in rows), default=0)
    for i, row in enumerate(rows, 1):
        mark = "x" if row.checked else " "
        lines.append(f"  [{mark}] {i}. {row.label.ljust(width)}  {row.state}")
        lines.append(f"         {row.path}")
    lines.append(HELP)
    return "\n".join(lines)


def parse_selection(text: str, checked: List[bool]) -> Optional[List[bool]]:
    """Nouvel état des cases, ou None (quitter). ValueError si la réponse
    n'est pas comprise."""
    answer = (text or "").strip().lower()
    if answer == "":
        return list(checked)
    if answer in ("q", "quitter", "quit"):
        return None
    if answer in ("tous", "tout", "all"):
        return [True] * len(checked)
    if answer in ("aucun", "none"):
        return [False] * len(checked)
    state = list(checked)
    for token in re.split(r"[\s,;]+", answer):
        if not token.isdigit() or not 1 <= int(token) <= len(state):
            raise ValueError(f"réponse non comprise : « {token} » (numéros 1 à "
                             f"{len(state)}, tous, aucun, q)")
        state[int(token) - 1] = not state[int(token) - 1]
    return state


def ask_selection(rows: List[Row], input_func: Callable = input,
                  print_func: Callable = print, verb: str = "Configurer",
                  max_rounds: int = 50) -> Optional[List[str]]:
    """Dialogue de sélection ; ids retenus, ou None si l'utilisateur quitte."""
    for _ in range(max_rounds):
        print_func(render_menu(rows))
        try:
            answer = input_func("> ")
        except (EOFError, KeyboardInterrupt):
            return None
        if (answer or "").strip() == "":
            chosen = [r for r in rows if r.checked]
            if not chosen:
                print_func("Aucun client coché : cocher au moins un client, ou q.")
                continue
            try:
                confirm = input_func(f"{verb} : {', '.join(r.label for r in chosen)}. "
                                     f"Confirmer ? [O/n] ")
            except (EOFError, KeyboardInterrupt):
                return None
            if (confirm or "").strip().lower() in ("", "o", "oui", "y", "yes"):
                return [r.id for r in chosen]
            continue
        try:
            state = parse_selection(answer, [r.checked for r in rows])
        except ValueError as exc:
            print_func(str(exc))
            continue
        if state is None:
            return None
        for row, value in zip(rows, state):
            row.checked = value
    return None
