"""
Tests OPT-02 — correctifs de la revue.

1. **Fichiers de service hors de portée des outils en HTTP.** `read_resource`,
   `list_resources` et `/download` les excluaient déjà, mais le confinement
   des outils (`utils.sandbox.resolve_in_output_dir`, via `_safe_path`)
   acceptait tout chemin absolu sous OUTPUT_DIR, dossiers cachés compris.
   OUTPUT_DIR étant prévisible en déploiement (`/data/output`), tout porteur
   de jeton pouvait écraser le magasin OAuth par l'`output_path` de
   `corpus_to_csl` ou `corpus_merge` (le serveur ne redémarrait plus :
   `AttributeError` au chargement), réécrire le journal d'audit du jour, ou
   lire le magasin comme corpus d'entrée (`corpus_filter` écrivait même
   `oauth_store_filtered.json` dans `.oauth/`). Le stdio reste libre
   (invariant : process local de confiance).
2. **Plafond en octets de l'échantillon tronqué de `read_resource`** : les
   notices étaient mesurées en JSON compact mais émises avec indent=2, d'où
   une réponse jusqu'à près de deux fois le plafond pour des notices faites
   de nombreux champs courts. Elles sont désormais mesurées dans le format
   d'émission.

Sur la base de référence, les tests d'écriture et de lecture de fichiers de
service en HTTP, et le test de taille sur notices imbriquées, échouent.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from utils.sandbox import PathSecurityError, resolve_in_output_dir  # noqa: E402


STORE = {"clients": {}, "access": {"JETON": {"client_id": "c"}}, "refresh": {}}
# Journal d'une autre date : le serveur écrit son propre audit dans le fichier
# du jour, celui-ci doit rester intact à l'octet près.
AUDIT_NAME = "audit-20000101.jsonl"
AUDIT_LINE = '{"tool": "trace-a-conserver"}\n'


def _run(coro):
    return asyncio.run(coro)


def _text(result) -> str:
    """Texte concaténé d'un retour d'outil (liste de blocs, tuple, ou
    CallToolResult d'erreur : isError depuis OPT-07)."""
    content = getattr(result, "content", None)
    if content is not None and hasattr(result, "isError"):
        blocks = content
    else:
        blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(blocks, str):
        return blocks
    return "\n".join(getattr(b, "text", "") or "" for b in blocks)


@pytest.fixture
def out(tmp_path, monkeypatch):
    """OUTPUT_DIR en mode HTTP, avec magasin OAuth, journal d'audit et index."""
    base = tmp_path / "out"
    base.mkdir()
    (base / ".oauth").mkdir()
    (base / ".oauth" / "oauth_store.json").write_text(
        json.dumps(STORE), encoding="utf-8")
    (base / ".audit").mkdir()
    (base / ".audit" / AUDIT_NAME).write_text(AUDIT_LINE, encoding="utf-8")
    (base / ".index.json").write_text("[]", encoding="utf-8")
    (base / "corpus_a.json").write_text(json.dumps([
        {"itemType": "journalArticle", "title": "T", "date": "2024",
         "creators": []}]), encoding="utf-8")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", base)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    return base


def _snapshot(base: Path) -> dict:
    """Contenu, à l'octet près, des fichiers de service (hors audit du jour)."""
    return {
        "store": (base / ".oauth" / "oauth_store.json").read_bytes(),
        "audit": (base / ".audit" / AUDIT_NAME).read_bytes(),
        "index": (base / ".index.json").read_bytes(),
        "oauth_dir": sorted(p.name for p in (base / ".oauth").iterdir()),
    }


# === Primitive de confinement ===

class TestSandboxRefusesServiceFiles:
    @pytest.mark.parametrize("rel", [
        ".oauth/oauth_store.json",
        ".audit/audit-20260928.jsonl",
        ".index.json",
        ".oauth/nouveau.json",          # cible inexistante : écriture refusée
    ])
    def test_absolute_path_under_hidden_component_refused(self, tmp_path, rel):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(PathSecurityError, match="service"):
            resolve_in_output_dir(str(target), tmp_path)

    def test_bare_hidden_name_refused(self, tmp_path):
        # Un nom nu est rebasé sous la racine : `.index.json` y est un
        # fichier de service.
        with pytest.raises(PathSecurityError, match="service"):
            resolve_in_output_dir(".index.json", tmp_path)

    def test_refusal_precedes_existence_check(self, tmp_path):
        # Même message qu'il existe ou non : pas d'oracle d'existence.
        (tmp_path / ".oauth").mkdir()
        (tmp_path / ".oauth" / "present.json").write_text("{}")
        msgs = []
        for name in ("present.json", "absent.json"):
            with pytest.raises(PathSecurityError) as exc:
                resolve_in_output_dir(str(tmp_path / ".oauth" / name),
                                      tmp_path, must_exist=True)
            msgs.append(str(exc.value).split(" : ")[0])
        assert msgs[0] == msgs[1] == "Fichier de service inaccessible"

    def test_internal_symlink_to_service_file_refused(self, tmp_path):
        (tmp_path / ".oauth").mkdir()
        store = tmp_path / ".oauth" / "oauth_store.json"
        store.write_text("{}")
        link = tmp_path / "corpus_piege.json"
        try:
            link.symlink_to(store)
        except (OSError, NotImplementedError):
            pytest.skip("liens symboliques non pris en charge")
        with pytest.raises(PathSecurityError):
            resolve_in_output_dir("corpus_piege.json", tmp_path)

    def test_output_dir_under_hidden_parent_still_usable(self, tmp_path):
        # Seuls les composants RELATIFS à la racine comptent.
        base = tmp_path / ".moisson" / "output"
        base.mkdir(parents=True)
        (base / "corpus_x.json").write_text("[]")
        assert resolve_in_output_dir("corpus_x.json", base, must_exist=True) \
            == (base / "corpus_x.json").resolve()
        assert resolve_in_output_dir(str(base / "corpus_y.json"), base) \
            == (base.resolve() / "corpus_y.json")

    def test_regular_paths_unchanged(self, tmp_path):
        assert resolve_in_output_dir("corpus_x.json", tmp_path) \
            == tmp_path.resolve() / "corpus_x.json"
        assert resolve_in_output_dir(str(tmp_path / "corpus_x.csl.json"),
                                     tmp_path) \
            == tmp_path.resolve() / "corpus_x.csl.json"


# === Outils en HTTP : ni écriture ni lecture des fichiers de service ===

class TestHttpToolsCannotTouchServiceFiles:
    def test_corpus_to_csl_cannot_overwrite_oauth_store(self, out):
        before = _snapshot(out)
        text = _text(_run(mcp_server.call_tool("corpus_to_csl", {
            "corpus_path": "corpus_a.json",
            "output_path": str(out / ".oauth" / "oauth_store.json")})))
        assert "PathSecurityError" in text
        assert str(out) not in text          # aucun chemin disque renvoyé
        assert _snapshot(out) == before

    def test_corpus_merge_cannot_overwrite_audit_log(self, out):
        before = _snapshot(out)
        text = _text(_run(mcp_server.call_tool("corpus_merge", {
            "corpus_paths": ["corpus_a.json", "corpus_a.json"],
            "output_path": str(out / ".audit" / AUDIT_NAME)})))
        assert "PathSecurityError" in text
        assert _snapshot(out) == before
        assert not list(out.glob("corpus_merged_*"))

    def test_corpus_merge_cannot_write_index(self, out):
        before = _snapshot(out)
        text = _text(_run(mcp_server.call_tool("corpus_merge", {
            "corpus_paths": ["corpus_a.json", "corpus_a.json"],
            "output_path": ".index.json"})))
        assert "PathSecurityError" in text
        assert _snapshot(out) == before

    @pytest.mark.parametrize("tool,extra", [
        ("corpus_filter", {"year_min": 1900}),
        ("corpus_analyze", {}),
        ("corpus_to_csl", {}),
    ])
    def test_oauth_store_not_readable_as_input(self, out, tool, extra):
        before = _snapshot(out)
        args = {"corpus_path": str(out / ".oauth" / "oauth_store.json")}
        args.update(extra)
        text = _text(_run(mcp_server.call_tool(tool, args)))
        assert "PathSecurityError" in text
        assert "JETON" not in text
        # corpus_filter écrivait `oauth_store_filtered.json` dans `.oauth/`.
        assert _snapshot(out) == before

    def test_merge_input_under_audit_refused(self, out):
        text = _text(_run(mcp_server.call_tool("corpus_merge", {
            "corpus_paths": ["corpus_a.json",
                             str(out / ".audit" / AUDIT_NAME)]})))
        assert "PathSecurityError" in text
        assert not list(out.glob("corpus_merged_*"))

    def test_oauth_provider_still_loads_after_attempts(self, out):
        from oauth_provider import FileOAuthProvider
        store = out / ".oauth" / "oauth_store.json"
        for tool, args in (
            ("corpus_to_csl", {"corpus_path": "corpus_a.json",
                               "output_path": str(store)}),
            ("corpus_merge", {"corpus_paths": ["corpus_a.json"] * 2,
                              "output_path": str(store)}),
        ):
            _run(mcp_server.call_tool(tool, args))
        # Sur la base de référence : AttributeError ('list' has no 'get').
        FileOAuthProvider(base_url="https://x.example", password="p",
                          resource_url="https://x.example/mcp",
                          store_path=store)

    def test_legitimate_absolute_output_still_accepted(self, out):
        target = out / "corpus_a_export.csl.json"
        text = _text(_run(mcp_server.call_tool("corpus_to_csl", {
            "corpus_path": str(out / "corpus_a.json"),
            "output_path": str(target)})))
        assert "PathSecurityError" not in text
        assert json.loads(target.read_text(encoding="utf-8"))[0]["title"] == "T"


class TestStdioUnchanged:
    def test_safe_path_stays_free_in_stdio(self, out, monkeypatch):
        # Invariant : en stdio (process local de confiance) les chemins sont
        # libres ; la garde des fichiers de service est propre au HTTP.
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        p = out / ".oauth" / "oauth_store.json"
        assert mcp_server._safe_path(str(p)) == p

    def test_safe_path_refuses_in_http(self, out):
        with pytest.raises(PathSecurityError):
            mcp_server._safe_path(str(out / ".oauth" / "oauth_store.json"))


# === Échantillon tronqué de read_resource : plafond mesuré à l'émission ===

def _nested_items(n: int, creators: int) -> list:
    return [{"itemType": "journalArticle", "title": f"T{i}",
             "creators": [{"creatorType": "author", "firstName": "A",
                           "lastName": f"N{j}"} for j in range(creators)]}
            for i in range(n)]


def _emitted_item_size(item) -> int:
    """Taille d'une notice dans la réponse réelle : différence entre une
    enveloppe indent=2 qui la contient et une enveloppe vide."""
    def env(items):
        return len(json.dumps({"items": items}, ensure_ascii=False,
                              indent=2).encode("utf-8"))
    return env([item]) - env([])


class TestTruncatedSampleByteCap:
    @pytest.fixture
    def res_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
        return tmp_path

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_nested_notices_response_bounded(self, res_dir, monkeypatch, mode):
        # 60 notices de 900 auteurs : l'indentation double presque la taille
        # (fichier compact de 3,5 Mo ; réponse de 3,56 Mo sur la base).
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", mode)
        items = _nested_items(60, 900)
        f = res_dir / "corpus_imbrique.json"
        f.write_text(json.dumps(items), encoding="utf-8")
        assert f.stat().st_size > mcp_server.RESOURCE_MAX_BYTES

        text = _run(mcp_server.read_resource("corpus://corpus_imbrique.json"))
        data = json.loads(text)
        assert data["_truncated"] is True
        assert data["_total_items"] == 60
        assert data["_byte_cap_reached"] is True
        # Plafond souple : au plus une notice au-delà, plus l'enveloppe.
        one = max(_emitted_item_size(it) for it in items)
        assert len(text.encode("utf-8")) \
            <= mcp_server.RESOURCE_MAX_BYTES + one + 1024
        # Et la dernière notice admise était bien nécessaire : sans elle,
        # l'échantillon restait sous le plafond.
        without_last = sum(_emitted_item_size(it)
                           for it in data["items"][:-1])
        assert without_last < mcp_server.RESOURCE_MAX_BYTES

    def test_sample_item_bytes_matches_emission(self):
        # Écart constant de 2 octets (fermeture du tableau), quelle que soit
        # la forme de la notice : la mesure suit le format émis.
        samples = [
            {"title": "Tï€", "creators": [{"lastName": "é"}] * 5, "tags": [],
             "extra": {"a": [1, [2, {}]]}},
            {}, [], "texte", 3, {"k": []},
        ]
        for n in range(1, len(samples) + 1):
            chunk = samples[:n]

            def env(items):
                return len(json.dumps(
                    {"_truncated": True, "_message": "m", "items": items},
                    ensure_ascii=False, indent=2).encode("utf-8"))
            actual = env(chunk) - env([])
            measured = sum(mcp_server._sample_item_bytes(it) for it in chunk)
            assert actual - measured == 2, (n, actual, measured)

    def test_historic_flat_case_keeps_50_items(self, res_dir):
        # Cas du test historique (60 × 40 Ko à plat) : l'indentation y est
        # négligeable, l'échantillon garde ses 50 items.
        big = [{"itemType": "journalArticle", "title": f"T{i}",
                "abstractNote": "x" * 40000, "date": "2024"}
               for i in range(60)]
        (res_dir / "corpus_plat.json").write_text(json.dumps(big),
                                                  encoding="utf-8")
        data = json.loads(_run(
            mcp_server.read_resource("corpus://corpus_plat.json")))
        assert data["_returned"] == 50
        assert data["_byte_cap_reached"] is False
