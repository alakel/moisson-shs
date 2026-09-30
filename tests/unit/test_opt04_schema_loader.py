"""
OPT-04 : schéma Zotero sans répétition d'échec.

Vérifie qu'un schéma officiel indisponible (réseau coupé, pas de cache
disque) ne coûte qu'une tentative réseau par fenêtre d'échec, quel que soit
le nombre de notices validées et de threads, que la fenêtre expire, que
force_refresh la contourne, que $MOISSON_ZOTERO_SCHEMA_FILE court-circuite
réseau et cache disque, et que les champs autorisés sont précalculés par
schéma.

Aucun appel réseau : requests.get est patché partout.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from mappers import zotero_schema_loader as loader
from mappers.zotero_schema import validate_and_clean

FIXTURE_SCHEMA = (
    Path(__file__).resolve().parents[1] / "fixtures" / "zotero_schema_test.json"
)

ITEM = {
    "itemType": "journalArticle",
    "title": "T",
    "DOI": "10.1/x",
    "publicationTitle": "Revue",
    "exoticField": "valeur",
}


def _fake_schema(version=1, extra_field=None):
    fields = [{"field": "title"}, {"field": "DOI"},
              {"field": "publicationTitle"}]
    if extra_field:
        fields.append({"field": extra_field})
    return {
        "version": version,
        "itemTypes": [
            {
                "itemType": "journalArticle",
                "fields": fields,
                "creatorTypes": [{"creatorType": "author"}],
            },
        ],
    }


class _CountingGet:
    """Remplaçant de requests.get, compteur protégé contre la concurrence."""

    def __init__(self, delay=0.0, payload=None):
        self.delay = delay
        self.payload = payload
        self.count = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.count += 1
        if self.delay:
            time.sleep(self.delay)
        if self.payload is None:
            raise requests.ConnectionError("réseau indisponible (test)")
        return _Resp(self.payload)


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _BlockingGet:
    """Remplaçant de requests.get qui attend le feu vert du test, puis échoue."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.count = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.count += 1
        self.started.set()
        assert self.release.wait(5), "tentative jamais libérée (test)"
        raise requests.ConnectionError("réseau indisponible (test)")


class _CountingLock:
    """Verrou instrumenté : compte les appels qui ont demandé le verrou."""

    def __init__(self):
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self.requests = 0

    def __enter__(self):
        with self._guard:
            self.requests += 1
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


class _Clock:
    """Horloge monotone pilotée par le test."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def isolated_loader(monkeypatch, tmp_path):
    """
    Chargeur isolé : pas de fichier figé, cache disque absent (répertoire
    temporaire), état remis à zéro ; l'état antérieur est restauré ensuite
    pour ne pas perturber les autres modules de tests.

    La fenêtre d'échec et la durée de validité du cache sont lues dans
    l'environnement à l'import du module : elles sont fixées ici à leurs
    valeurs par défaut, sinon un MOISSON_SCHEMA_RETRY_AFTER=0 hérité du
    shell ferait échouer ces tests (et laisserait passer un téléchargement
    réel).
    """
    saved = (loader._MEMORY_CACHE, loader._UNAVAILABLE_UNTIL,
             loader._SCHEMA_ORIGIN, loader._INDEX)
    monkeypatch.delenv(loader.SCHEMA_FILE_ENV, raising=False)
    monkeypatch.setattr(loader, "CACHE_PATH", tmp_path / "absent" / "schema.json")
    monkeypatch.setattr(loader, "SCHEMA_RETRY_AFTER_SECONDS", 600)
    monkeypatch.setattr(loader, "CACHE_MAX_AGE_SECONDS", 30 * 86400)
    loader.reset_state()
    yield
    (loader._MEMORY_CACHE, loader._UNAVAILABLE_UNTIL,
     loader._SCHEMA_ORIGIN, loader._INDEX) = saved


# === Une seule tentative réseau par fenêtre d'échec =========================

class TestSingleAttempt:
    def test_1000_validations_offline_one_attempt(self):
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            for _ in range(1000):
                cleaned = validate_and_clean(dict(ITEM))
        assert get.count == 1
        # Repli embarqué : DOI conservé, champ inconnu déplacé dans extra
        assert cleaned["DOI"] == "10.1/x"
        assert "exoticField: valeur" in cleaned["extra"]
        assert loader.schema_state() == loader.ETAT_INDISPONIBLE
        assert not loader.CACHE_PATH.exists()

    def test_repli_immediat_pendant_la_fenetre(self):
        # Une tentative lente (50 ms) : les 999 validations suivantes ne
        # doivent pas l'attendre (≈ 50 s sur l'ancien chargeur)
        get = _CountingGet(delay=0.05)
        with patch.object(loader.requests, "get", side_effect=get):
            t0 = time.perf_counter()
            for _ in range(1000):
                validate_and_clean(dict(ITEM))
            elapsed = time.perf_counter() - t0
        assert get.count == 1
        assert elapsed < 5.0

    def test_8_threads_concurrents_une_tentative(self):
        # La tentative dure 50 ms : les autres threads arrivent pendant
        # qu'elle est en cours et doivent attendre son issue, pas retenter
        get = _CountingGet(delay=0.05)
        barrier = threading.Barrier(8)

        def worker(n):
            barrier.wait()
            out = None
            for _ in range(n):
                out = validate_and_clean(dict(ITEM))
            return out

        with patch.object(loader.requests, "get", side_effect=get):
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = [f.result() for f in
                           [ex.submit(worker, 125) for _ in range(8)]]
        assert get.count == 1
        assert all(r["DOI"] == "10.1/x" for r in results)

    def test_8_threads_succes_un_seul_telechargement(self):
        get = _CountingGet(delay=0.05, payload=_fake_schema(extra_field="exoticField"))
        barrier = threading.Barrier(8)

        def worker(n):
            barrier.wait()
            return [validate_and_clean(dict(ITEM)) for _ in range(n)][-1]

        with patch.object(loader.requests, "get", side_effect=get):
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = [f.result() for f in
                           [ex.submit(worker, 125) for _ in range(8)]]
        assert get.count == 1
        # Tous les threads voient le schéma téléchargé (champ exotique admis)
        assert all(r.get("exoticField") == "valeur" for r in results)
        assert loader.schema_state() == loader.ETAT_CHARGE
        assert loader.schema_status()["origine"] == "reseau"
        assert loader.CACHE_PATH.exists()

    def test_mecanisme_du_patch_global_de_requests_get(self):
        # Mécanisme de l'échec intermittent de test_openalex_author_by_orcid :
        # un patch global de requests.get intercepte aussi le téléchargement
        # du schéma quand ni mémoire ni cache disque frais ne le fournissent.
        # Désormais au plus une fois par fenêtre, et jamais avec le fichier figé.
        with patch("requests.get", return_value=_Resp({"results": []})) as mg:
            for _ in range(5):
                validate_and_clean(dict(ITEM))
        assert mg.call_count == 1   # réponse sans itemTypes → échec mémorisé


# === Fenêtre d'échec : expiration, désactivation, contournement =============

class TestFailureWindow:
    def test_fenetre_expiree_nouvelle_tentative(self, monkeypatch):
        clock = _Clock(1000.0)
        monkeypatch.setattr(loader, "_clock", clock)
        monkeypatch.setattr(loader, "SCHEMA_RETRY_AFTER_SECONDS", 600)
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            assert loader.load_zotero_schema() is None
            assert get.count == 1
            clock.now = 1599.9
            assert loader.load_zotero_schema() is None
            assert get.count == 1
            assert loader.schema_state() == loader.ETAT_INDISPONIBLE
            clock.now = 1600.0
            assert loader.schema_state() == loader.ETAT_NON_INITIALISE
            assert loader.load_zotero_schema() is None
            assert get.count == 2
            # Nouvelle fenêtre ouverte à partir du second échec
            clock.now = 2199.0
            loader.load_zotero_schema()
            assert get.count == 2

    def test_reseau_revenu_apres_fenetre(self, monkeypatch):
        clock = _Clock(0.0)
        monkeypatch.setattr(loader, "_clock", clock)
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            assert loader.load_zotero_schema() is None
        clock.now = loader.SCHEMA_RETRY_AFTER_SECONDS + 1
        get_ok = _CountingGet(payload=_fake_schema())
        with patch.object(loader.requests, "get", side_effect=get_ok):
            assert loader.load_zotero_schema() == _fake_schema()
            loader.load_zotero_schema()
        assert get_ok.count == 1
        assert loader.schema_state() == loader.ETAT_CHARGE

    def test_retry_after_zero_desactive_la_memoire(self, monkeypatch):
        monkeypatch.setattr(loader, "SCHEMA_RETRY_AFTER_SECONDS", 0)
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            for _ in range(3):
                loader.load_zotero_schema()
        assert get.count == 3

    def test_retry_after_zero_appels_concurrents_partagent_la_tentative(
            self, monkeypatch):
        # Revue OPT-04 : avec la fenêtre désactivée, les threads qui
        # attendaient le verrou pendant une tentative relançaient chacun la
        # leur, en file (8 threads × délai de connexion). Ils reprennent
        # désormais l'issue de la tentative terminée pendant leur attente.
        monkeypatch.setattr(loader, "SCHEMA_RETRY_AFTER_SECONDS", 0)
        lock = _CountingLock()
        monkeypatch.setattr(loader, "_LOCK", lock)
        get = _BlockingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            with ThreadPoolExecutor(max_workers=8) as ex:
                first = ex.submit(loader.load_zotero_schema)
                assert get.started.wait(5)
                others = [ex.submit(loader.load_zotero_schema)
                          for _ in range(7)]
                # Les 7 appels ont demandé le verrou tenu par la tentative
                deadline = time.monotonic() + 5
                while lock.requests < 8 and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert lock.requests == 8
                get.release.set()
                results = [f.result(timeout=5) for f in [first, *others]]
            assert get.count == 1
            assert results == [None] * 8
            # Appels successifs : fenêtre désactivée, chacun retente
            loader.load_zotero_schema()
            loader.load_zotero_schema()
        assert get.count == 3
        assert loader.schema_state() == loader.ETAT_NON_INITIALISE

    def test_force_refresh_contourne_la_fenetre(self):
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            loader.load_zotero_schema()
            loader.load_zotero_schema()
            assert get.count == 1
            assert loader.load_zotero_schema(force_refresh=True) is None
            assert get.count == 2
        get_ok = _CountingGet(payload=_fake_schema())
        with patch.object(loader.requests, "get", side_effect=get_ok):
            assert loader.load_zotero_schema(force_refresh=True) == _fake_schema()
        assert loader.schema_state() == loader.ETAT_CHARGE

    def test_force_refresh_en_echec_conserve_le_schema_charge(self):
        loader._MEMORY_CACHE = _fake_schema(version=7)
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            assert loader.load_zotero_schema(force_refresh=True) is None
        # Comportement historique : l'ancien schéma continue de servir
        assert loader.schema_state() == loader.ETAT_CHARGE
        assert loader.load_zotero_schema()["version"] == 7

    def test_cache_perime_evite_la_fenetre(self, monkeypatch):
        # Cache disque présent mais périmé : utilisé en repli, pas d'état
        # « indisponible »
        import json
        loader.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        loader.CACHE_PATH.write_text(json.dumps(_fake_schema(version=3)))
        monkeypatch.setattr(loader, "CACHE_MAX_AGE_SECONDS", -1)
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            assert loader.load_zotero_schema()["version"] == 3
            loader.load_zotero_schema()
        assert get.count == 1
        assert loader.schema_status()["origine"] == "disque_perime"

    def test_clear_cache_efface_la_fenetre(self):
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            loader.load_zotero_schema()
            loader.clear_cache()
            assert loader.schema_state() == loader.ETAT_NON_INITIALISE
            loader.load_zotero_schema()
        assert get.count == 2

    def test_delai_de_lecture_plafonne(self):
        # Revue OPT-04 : le verrou est tenu pendant le téléchargement ; le
        # délai de lecture général (45 s par défaut) est plafonné à 15 s
        # pour le seul schéma, le délai de connexion reste celui de config
        import config
        seen = {}

        def get(*args, **kwargs):
            seen.update(kwargs)
            raise requests.ConnectionError("réseau indisponible (test)")

        with patch.object(loader.requests, "get", side_effect=get):
            loader.load_zotero_schema()
        connect, read = seen["timeout"]
        assert connect == config.HTTP_TIMEOUT[0]
        assert read == min(config.HTTP_TIMEOUT[1], loader.SCHEMA_READ_TIMEOUT_MAX)
        assert loader.SCHEMA_READ_TIMEOUT_MAX == 15.0

    @pytest.mark.parametrize("raw,expected", [
        ("", 600), ("abc", 600), ("-5", 0), ("30", 30), (" 45 ", 45),
    ])
    def test_lecture_de_la_variable(self, monkeypatch, raw, expected):
        monkeypatch.setenv("MOISSON_SCHEMA_RETRY_AFTER", raw)
        assert loader._env_int("MOISSON_SCHEMA_RETRY_AFTER", 600) == expected


# === États explicites =======================================================

class TestStates:
    def test_transitions(self, monkeypatch):
        clock = _Clock(50.0)
        monkeypatch.setattr(loader, "_clock", clock)
        assert loader.schema_state() == loader.ETAT_NON_INITIALISE
        with patch.object(loader.requests, "get", side_effect=_CountingGet()):
            loader.load_zotero_schema()
        status = loader.schema_status()
        assert status["etat"] == loader.ETAT_INDISPONIBLE
        assert status["origine"] is None
        assert status["nouvelle_tentative_dans_s"] == float(
            loader.SCHEMA_RETRY_AFTER_SECONDS)
        with patch.object(loader.requests, "get",
                          side_effect=_CountingGet(payload=_fake_schema(version=9))):
            loader.load_zotero_schema(force_refresh=True)
        status = loader.schema_status()
        assert status == {"etat": loader.ETAT_CHARGE, "origine": "reseau",
                          "version": 9, "nouvelle_tentative_dans_s": None}
        loader.reset_state()
        assert loader.schema_state() == loader.ETAT_NON_INITIALISE

    def test_affectation_directe_du_cache_memoire(self):
        # Compatibilité : les tests existants affectent _MEMORY_CACHE
        loader._MEMORY_CACHE = _fake_schema()
        assert loader.schema_state() == loader.ETAT_CHARGE
        with patch.object(loader.requests, "get", side_effect=_CountingGet()) as g:
            assert "DOI" in loader.get_allowed_fields("journalArticle")
        assert g.call_count == 0


# === Fichier de schéma figé =================================================

class TestSchemaFileEnv:
    def test_fichier_respecte_sans_reseau_ni_cache_disque(self, monkeypatch):
        import json
        # Cache disque frais d'un AUTRE schéma : doit être ignoré
        loader.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        loader.CACHE_PATH.write_text(json.dumps(_fake_schema(version=1)))
        before = loader.CACHE_PATH.read_text()
        monkeypatch.setenv(loader.SCHEMA_FILE_ENV, str(FIXTURE_SCHEMA))
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            schema = loader.load_zotero_schema()
            for _ in range(1000):
                validate_and_clean(dict(ITEM))
        expected = json.loads(FIXTURE_SCHEMA.read_text(encoding="utf-8"))
        assert schema["version"] == expected["version"] != 1
        assert get.count == 0
        assert loader.CACHE_PATH.read_text() == before
        assert loader.schema_status()["origine"] == "fichier"
        assert loader.is_valid_item_type("preprint")

    def test_force_refresh_relit_le_fichier(self, monkeypatch, tmp_path):
        import json
        path = tmp_path / "schema_fige.json"
        path.write_text(json.dumps(_fake_schema(version=11)))
        monkeypatch.setenv(loader.SCHEMA_FILE_ENV, str(path))
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            assert loader.load_zotero_schema()["version"] == 11
            path.write_text(json.dumps(_fake_schema(version=12)))
            assert loader.load_zotero_schema()["version"] == 11
            assert loader.load_zotero_schema(force_refresh=True)["version"] == 12
        assert get.count == 0

    @pytest.mark.parametrize("content", [None, "pas du json{{", '{"version": 1}'])
    def test_fichier_inutilisable_strategie_ordinaire(
            self, monkeypatch, tmp_path, content):
        path = tmp_path / "schema_fige.json"
        if content is not None:
            path.write_text(content)
        monkeypatch.setenv(loader.SCHEMA_FILE_ENV, str(path))
        get = _CountingGet(payload=_fake_schema(version=5))
        with patch.object(loader.requests, "get", side_effect=get):
            assert loader.load_zotero_schema()["version"] == 5
        assert get.count == 1
        assert loader.schema_status()["origine"] == "reseau"


# === Précalcul des champs autorisés =========================================

class TestPrecomputedFields:
    def test_ensembles_precalcules_et_partages(self):
        loader._MEMORY_CACHE = _fake_schema()
        a = loader.get_allowed_fields("journalArticle")
        b = loader.get_allowed_fields("journalArticle")
        assert a is b
        assert isinstance(a, frozenset)
        assert {"title", "DOI", "itemType", "creators"} <= a
        assert loader.get_allowed_creator_types("journalArticle") == {"author"}

    def test_invalide_si_le_schema_change(self):
        loader._MEMORY_CACHE = _fake_schema(version=1)
        assert "exoticField" not in loader.get_allowed_fields("journalArticle")
        loader._MEMORY_CACHE = _fake_schema(version=2, extra_field="exoticField")
        assert "exoticField" in loader.get_allowed_fields("journalArticle")

    def test_invalide_si_la_version_change_en_place(self):
        schema = _fake_schema(version=1)
        loader._MEMORY_CACHE = schema
        loader.get_allowed_fields("journalArticle")
        schema["version"] = 2
        schema["itemTypes"][0]["fields"].append({"field": "exoticField"})
        assert "exoticField" in loader.get_allowed_fields("journalArticle")

    def test_repli_embarque_precalcule(self):
        # Tous les appels dans le bloc patché : aucun ne doit pouvoir
        # atteindre le réseau, quelle que soit la fenêtre d'échec
        get = _CountingGet()
        with patch.object(loader.requests, "get", side_effect=get):
            a = loader.get_allowed_fields("thesis")
            b = loader.get_allowed_fields("thesis")
            doc = loader.get_allowed_fields("typeInconnu")
            document = loader.get_allowed_fields("document")
        assert a is b
        assert "university" in a
        assert doc == document
        assert get.count == 1

    def test_type_absent_du_schema_repli_embarque(self):
        loader._MEMORY_CACHE = _fake_schema()
        fields = loader.get_allowed_fields("thesis")
        assert "university" in fields
        assert loader.is_valid_item_type("thesis") is False
        assert "editor" in loader.get_allowed_creator_types("thesis")

    def test_schema_malforme_tolere(self):
        loader._MEMORY_CACHE = {
            "version": 1,
            "itemTypes": [
                "pas un dict",
                {"itemType": "book", "fields": [{"nofield": 1}, {"field": "ISBN"}]},
                {"fields": []},
            ],
        }
        assert "ISBN" in loader.get_allowed_fields("book")
        assert loader.is_valid_item_type("book")
