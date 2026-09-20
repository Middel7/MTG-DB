"""
Non-régression sur la détection de décrochage.

C'est la logique qui décide d'envoyer une alerte ou non. Deux erreurs y coûtent
cher, et dans des sens opposés : un faux négatif laisse le catalogue rassir en
silence, un faux positif finit par faire ignorer les alertes.

Les cas sont construits à la main plutôt que lus en base : on teste la règle,
pas l'état du moment.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fraicheur():
    spec = importlib.util.spec_from_file_location(
        "fraicheur_sous_test", ROOT / "scripts" / "fraicheur.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ligne(source: str, *, verifiee_il_y_a=timedelta(minutes=30),
          retard=None, importee_il_y_a=timedelta(hours=1), a_jour=True) -> dict:
    """Construit une ligne de la vue `mtgdb_fraicheur_sources`."""
    maintenant = datetime.now(timezone.utc)
    return {
        "source": source,
        "verifiee_le": maintenant - verifiee_il_y_a,
        "derniere_version": "version-X",
        "publiee_le": maintenant - (retard or timedelta(hours=1)),
        "detectee_le": maintenant - (retard or timedelta(hours=1)),
        "derniere_version_importee": "version-X" if a_jour else None,
        "dernier_import_le": maintenant - importee_il_y_a if importee_il_y_a else None,
        "a_jour": a_jour,
        "retard": retard,
        "depuis_derniere_verification": verifiee_il_y_a,
        "depuis_dernier_import": importee_il_y_a,
    }


def toutes_les_sources(**surcharges) -> list[dict]:
    """Un état nominal complet, que chaque test dégrade sur un seul point."""
    sources = ["scryfall_bulk", "cardmarket_price_guide",
               "cardmarket_product_catalog", "tagger"]
    return [ligne(s, **surcharges.get(s, {})) for s in sources]


def detecter(module, lignes, **seuils):
    defauts = {"retard_max_h": 6, "veille_max_h": 3, "tags_max_jours": 9}
    defauts.update(seuils)
    return module.detecter_decrochages(lignes, **defauts)


class TestCasNominal:
    def test_tout_a_jour_ne_declenche_rien(self, fraicheur):
        assert detecter(fraicheur, toutes_les_sources()) == []

    def test_un_retard_court_ne_declenche_pas(self, fraicheur):
        """
        Une publication de moins de six heures est normale : le pipeline passe
        toutes les heures, mais un import peut durer, ou un run peut être
        retardé par le précédent.
        """
        lignes = toutes_les_sources(
            scryfall_bulk={"retard": timedelta(hours=2), "a_jour": False})
        assert detecter(fraicheur, lignes) == []

    def test_un_catalogue_cardmarket_calme_ne_declenche_pas(self, fraicheur):
        """
        Le catalogue produits n'est republié que de loin en loin. Un dernier
        import vieux de plusieurs jours est parfaitement sain tant que la
        version courante est absorbée.
        """
        lignes = toutes_les_sources(
            cardmarket_product_catalog={"importee_il_y_a": timedelta(days=12)})
        assert detecter(fraicheur, lignes) == []


class TestDecrochages:
    def test_une_publication_qui_attend_trop_longtemps(self, fraicheur):
        lignes = toutes_les_sources(
            scryfall_bulk={"retard": timedelta(hours=9), "a_jour": False})
        anomalies = detecter(fraicheur, lignes)
        assert len(anomalies) == 1
        assert "Scryfall" in anomalies[0]
        assert "pas absorbée" in anomalies[0]

    def test_une_source_qui_n_est_plus_interrogee(self, fraicheur):
        """Le cron horaire ne passe plus : build cassé, service suspendu."""
        lignes = toutes_les_sources(
            cardmarket_price_guide={"verifiee_il_y_a": timedelta(hours=7)})
        anomalies = detecter(fraicheur, lignes)
        assert len(anomalies) == 1
        assert "plus interrogée" in anomalies[0]

    def test_les_tags_hebdomadaires_manques(self, fraicheur):
        lignes = toutes_les_sources(
            tagger={"importee_il_y_a": timedelta(days=11)})
        anomalies = detecter(fraicheur, lignes)
        assert len(anomalies) == 1
        assert "tags" in anomalies[0].lower()

    def test_une_source_jamais_suivie_est_signalee(self, fraicheur):
        lignes = [ligne(s) for s in ("scryfall_bulk", "tagger")]
        anomalies = detecter(fraicheur, lignes)
        assert len(anomalies) == 2
        assert all("jamais suivie" in a for a in anomalies)

    def test_un_etat_vide_signale_les_quatre_sources(self, fraicheur):
        assert len(detecter(fraicheur, [])) == 4

    def test_les_anomalies_se_cumulent(self, fraicheur):
        lignes = toutes_les_sources(
            scryfall_bulk={"retard": timedelta(hours=9), "a_jour": False},
            tagger={"importee_il_y_a": timedelta(days=11)})
        assert len(detecter(fraicheur, lignes)) == 2


class TestSeuils:
    def test_les_seuils_sont_reglables(self, fraicheur):
        lignes = toutes_les_sources(
            scryfall_bulk={"retard": timedelta(hours=4), "a_jour": False})
        assert detecter(fraicheur, lignes, retard_max_h=6) == []
        assert len(detecter(fraicheur, lignes, retard_max_h=2)) == 1

    def test_le_tagger_n_est_pas_juge_sur_le_retard(self, fraicheur):
        """
        Tagger n'a pas de version publiée : l'API est interrogée en direct. Le
        seul critère qui ait un sens pour lui est la date du dernier import.
        """
        lignes = toutes_les_sources(
            tagger={"retard": timedelta(hours=50), "a_jour": False})
        assert detecter(fraicheur, lignes) == []


class TestAffichage:
    @pytest.mark.parametrize("duree,attendu", [
        (None, "—"),
        (timedelta(minutes=14), "14 min"),
        (timedelta(hours=2, minutes=5), "2 h 05"),
        (timedelta(days=3, hours=4), "3 j 04 h"),
    ])
    def test_les_durees_restent_lisibles(self, fraicheur, duree, attendu):
        assert fraicheur.humaniser(duree) == attendu

    def test_une_duree_negative_ne_produit_pas_d_absurdite(self, fraicheur):
        # Une horloge serveur en avance de quelques secondes sur la date
        # publiée ne doit pas afficher « -1 j 23 h ».
        assert fraicheur.humaniser(timedelta(seconds=-5)) == "à l'instant"
