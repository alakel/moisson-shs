"""
Sprint « universel », lot 8 : menu de sélection des clients (fonctions pures
et dialogue simulé).
"""

import pytest

from mcp_clients import menu


def _rows(*checked):
    return [menu.Row(f"c{i}", f"Client {i}", f"/p/{i}", "état", c)
            for i, c in enumerate(checked, 1)]


class TestParseSelection:
    def test_enter_keeps(self):
        assert menu.parse_selection("", [True, False]) == [True, False]

    def test_toggle_numbers(self):
        assert menu.parse_selection("1 2", [True, False]) == [False, True]
        assert menu.parse_selection("2,3", [False, False, False]) == [False, True, True]

    def test_all_none_quit(self):
        assert menu.parse_selection("tous", [False, False]) == [True, True]
        assert menu.parse_selection("aucun", [True, True]) == [False, False]
        assert menu.parse_selection("q", [True]) is None

    @pytest.mark.parametrize("bad", ["0", "4", "x", "1 deux"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            menu.parse_selection(bad, [True, False, True])


class TestRender:
    def test_marks_and_numbers(self):
        text = menu.render_menu(_rows(True, False))
        assert "[x] 1. Client 1" in text and "[ ] 2. Client 2" in text
        assert "/p/2" in text and "q" in text


class TestAsk:
    def _ask(self, answers, rows):
        answers = list(answers)
        printed = []

        def fake_input(prompt=""):
            if not answers:
                raise EOFError
            return answers.pop(0)
        out = menu.ask_selection(rows, input_func=fake_input,
                                 print_func=printed.append, verb="Configurer")
        return out, printed

    def test_validate_preselection(self):
        out, _ = self._ask(["", "o"], _rows(True, False, True))
        assert out == ["c1", "c3"]

    def test_toggle_then_validate(self):
        out, _ = self._ask(["2", "", ""], _rows(True, False))
        assert out == ["c1", "c2"]

    def test_quit(self):
        out, _ = self._ask(["q"], _rows(True))
        assert out is None

    def test_refused_confirmation_returns_to_menu(self):
        out, _ = self._ask(["", "n", "2", "", "o"], _rows(True, False))
        assert out == ["c1", "c2"]
        out, _ = self._ask(["", "n", "q"], _rows(True))
        assert out is None

    def test_nothing_checked_is_not_validated(self):
        out, printed = self._ask(["", "1", "", "o"], _rows(False))
        assert out == ["c1"]
        assert any("Aucun client" in p for p in printed)

    def test_bad_answer_is_explained(self):
        out, printed = self._ask(["9", "", ""], _rows(True))
        assert out == ["c1"] and any("9" in p for p in printed)

    def test_eof_quits(self):
        out, _ = self._ask([], _rows(True))
        assert out is None
