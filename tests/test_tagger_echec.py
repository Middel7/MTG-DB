"""
L'étape « tags » doit pouvoir échouer.

Elle ne le pouvait pas. `graphql_request()` retournait `None` aussi bien pour
« Tagger ne connaît pas cette carte » que pour « Tagger répond HTTP 500 », et
l'appelant ne comptait d'erreur que dans le second cas — qui ne se produisait
jamais, l'exception n'étant pas levée. Le compteur restait donc à zéro même
lorsque 100 % des requêtes échouaient, et le script sortait en succès après
quarante minutes de travail perdu.

Aucune trace n'existait par ailleurs en base : rien ne permettait de savoir
quand les tags avaient été rafraîchis pour la dernière fois. Une panne de
plusieurs mois n'aurait laissé aucune trace consultable.

Ces tests sont purs : `httpx.MockTransport` remplace le réseau, aucune base
n'est touchée.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tagger():
    spec = importlib.util.spec_from_file_location(
        "import_tagger_sous_test", ROOT / "scripts" / "import_tagger_tags.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(reponse: httpx.Response) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda requete: reponse))


@pytest.mark.parametrize("code", [500, 502, 503, 403, 404])
def test_une_reponse_http_en_erreur_est_signalee(tagger, code):
    """
    Et non avalée.

    Chacun de ces codes produisait auparavant un `return None` silencieux,
    indiscernable d'une carte inconnue.
    """
    with _client(httpx.Response(code)) as client:
        with pytest.raises(tagger._TaggerIndisponible):
            tagger.graphql_request(client, "jeton", "blb", "280")


def test_un_timeout_est_signale(tagger):
    def leve_timeout(requete):
        raise httpx.ConnectTimeout("délai dépassé", request=requete)

    with httpx.Client(transport=httpx.MockTransport(leve_timeout)) as client:
        with pytest.raises(tagger._TaggerIndisponible):
            tagger.graphql_request(client, "jeton", "blb", "280")


def test_une_page_html_servie_en_200_est_signalee(tagger):
    """
    Le cas le plus sournois : un portail d'erreur qui répond 200.

    `resp.json()` lève alors une `ValueError` que rien ne rattrapait, et qui
    remontait comme un bug du script plutôt que comme une panne de Tagger.
    """
    reponse = httpx.Response(200, text="<html><body>503 Service Unavailable</body></html>")
    with _client(reponse) as client:
        with pytest.raises(tagger._TaggerIndisponible):
            tagger.graphql_request(client, "jeton", "blb", "280")


def test_une_carte_inconnue_n_est_pas_une_erreur(tagger):
    """
    La distinction qui donne son sens au seuil d'échec.

    Tagger répond correctement qu'il ne connaît pas la carte : c'est un résultat.
    Le compter comme une panne ferait échouer des runs parfaitement sains — le
    catalogue contient en permanence des cartes que Tagger n'a pas encore vues.
    """
    with _client(httpx.Response(200, json={"data": {"card": None}})) as client:
        assert tagger.graphql_request(client, "jeton", "blb", "280") is None


def test_seuls_les_tags_oracle_sont_retenus(tagger):
    """Tagger renvoie plusieurs types ; un seul nous intéresse."""
    corps = {"data": {"card": {"name": "Sol Ring", "taggings": [
        {"tag": {"name": "ramp", "type": "ORACLE_CARD_TAG"}},
        {"tag": {"name": "cycle-artefact", "type": "ILLUSTRATION_TAG"}},
        {"tag": {"name": "mana-rock", "type": "ORACLE_CARD_TAG"}},
    ]}}}
    with _client(httpx.Response(200, json=corps)) as client:
        assert tagger.graphql_request(client, "jeton", "c21", "263") == ["ramp", "mana-rock"]


def test_le_seuil_d_echec_reste_tolerant_au_bruit(tagger):
    """
    20 % : ni zéro, ni l'infini.

    Un seuil à zéro ferait échouer un run pour trois timeouts isolés sur des
    milliers de cartes, et apprendrait surtout à ignorer l'alerte. Ce test fige
    la valeur pour qu'elle ne dérive pas sans décision.
    """
    assert tagger.SEUIL_ECHEC == 0.20
    assert 0 < tagger.SEUIL_ECHEC < 1


def test_la_source_de_tracabilite_est_figee(tagger):
    """C'est la clé sur laquelle se lit la fraîcheur des tags en base."""
    assert tagger.SOURCE == "tagger"
