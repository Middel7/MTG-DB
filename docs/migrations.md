# Migrations Alembic — base partagée

## ⚠️ À lire avant toute migration

La base **`manamind` est partagée par trois projets** qui y appliquent chacun leurs
propres migrations :

| Projet | Table de version | Possède |
|---|---|---|
| **MTG-DB** (ce dépôt) | `mtgdb_alembic_version` | `scryfall_*`, `cardmarket_*`, `import_runs` |
| **ManaMind_AI** | `alembic_version` | `users`, `deck_cards`, `deck_stat_*`, `commander_clusters`, `card_neighbors`, `user_*`, `invitations`… |
| **mtgtrade** | `mtgtrade_alembic_version` | ses propres tables |

Chaque projet a **sa** table de version : c'est ce qui leur permet de coexister. Ne
jamais faire pointer MTG-DB sur `alembic_version` — ce serait écraser l'historique de
ManaMind_AI.

`alembic/env.py` applique deux garde-fous, à ne pas retirer :

- `version_table = "mtgdb_alembic_version"`
- `include_object` : masque à l'autogenerate les tables des autres projets. Sans lui,
  `alembic revision --autogenerate` génère des `op.drop_table('users')`,
  `op.drop_table('deck_cards')`… et détruirait la moitié de la base.

Les tables `deck_stat_global` / `deck_stat_commander` méritent une mention à part :
MTG-DB en expose des **modèles en lecture seule**, mais **ManaMind_AI en est
propriétaire** et y ajoute des colonnes (`tfidf`, `idf`, `tfidf_norm`) que nos modèles
ignorent. Elles sont donc listées dans `FOREIGN_TABLES` et exclues des migrations
— sans quoi un autogenerate proposerait de supprimer ces colonnes.

---

## Historique de la réparation (13/07/2026)

L'état initial était bloqué :

- `alembic_version` contenait `20260712_fix_ucoll`, une révision de **ManaMind_AI** —
  MTG-DB ne pouvait plus rien faire (« Can't locate revision »).
- MTG-DB avait **deux heads divergentes** (`20260620_add_deck_stats_tables` et
  `20260711_add_tcgplayer_id_en`).
- `alembic/script.py.mako` était **absent** : impossible de générer une migration.

Correctifs appliqués :

1. Table de version dédiée `mtgdb_alembic_version` + filtre `include_object`.
2. `script.py.mako` restauré.
3. Fusion des deux heads → révision `1b7a9f3835e5`.
4. `alembic stamp head` sur la nouvelle table (le schéma physique était déjà à jour).

`alembic_version` (ManaMind_AI) n'a **pas** été modifiée.

---

## Commandes

```powershell
alembic current                                # révision courante de MTG-DB
alembic heads                                  # doit afficher UNE seule head
alembic upgrade head                           # appliquer
alembic revision --autogenerate -m "message"   # générer
alembic downgrade -1                           # revenir en arrière
```

---

## Bruit de l'autogenerate : résorbé

`alembic revision --autogenerate` ne détecte plus **aucune** opération lorsque les
modèles n'ont pas changé. Une migration générée ne contient donc que vos vrais
changements.

L'état antérieur produisait ~68 opérations parasites à chaque génération. Trois causes,
toutes corrigées :

| Cause | Correctif |
|---|---|
| 15 index avaient gardé leur nom d'avant le renommage des tables (`ix_cards_name` sur `scryfall_cards`) | Migration `20260713_rename_legacy_indexes` : `ALTER INDEX … RENAME TO`, exécutée en **0,5 s** (métadonnée pure, aucune reconstruction) |
| 4 `server_default` existaient en base sans être déclarés dans les modèles (`CURRENT_DATE`, `0`, `'cardmarket'`) | Déclarés dans les modèles. La base n'a **pas** été touchée : c'est le code qui mentait, et Alembic proposait de supprimer des defaults utiles. |
| `cardmarket_price_guide_entries.id_product` cumulait `index=True` et un `Index()` explicite | `index=True` retiré (il aurait créé un index en double) |

Au passage, l'index `ix_cards_game_changer` existait en base sans être déclaré : Alembic
proposait de le **supprimer**. Il est désormais déclaré (`index=True`) et conservé.

> Ne renommez jamais un index avec le `drop_index` + `create_index` que génère Alembic :
> sur des tables de 500 000 lignes, cela reconstruit l'index. `ALTER INDEX … RENAME TO`
> fait le même travail instantanément.

---

## Migrations notables

| Migration | Objet |
|---|---|
| `20260713_rename_legacy_indexes` | Alignement des noms d'index sur les tables préfixées (voir ci-dessus) |
| `20260824_printed_name_trgm` | Extension `pg_trgm` + index GIN trigram sur `scryfall_card_printings.printed_name`, en `CREATE INDEX CONCURRENTLY`. Recherche par nom traduit : de 645 Mo lus par requête à quelques Ko. Mesures, bench d'import et exploitation : [`recherche_trigram.md`](recherche_trigram.md) |
| `20260824_printed_name_lower` | Index fonctionnel `lower(printed_name)` pour la résolution de decklist (261 ms → 0,0 ms), et retrait du btree `ix_scryfall_card_printings_printed_name` qu'aucun consommateur ne pouvait utiliser. Bilan : −14 Mo |

> Ces deux migrations sont les premières du dépôt à sortir de la transaction
> (`autocommit_block`) et à se garder sur `pg_index.indisvalid` pour rester rejouables :
> `CREATE INDEX CONCURRENTLY` n'est pas transactionnel et laisse un index **INVALID**
> derrière lui en cas d'échec. S'en inspirer pour tout futur index sur une grosse table.
>
> Elles rappellent la règle des index déclarés : **tout index créé en migration doit
> l'être aussi dans le modèle**, avec le même nom — sinon `--autogenerate` proposera de
> le supprimer à chaque génération. Un index d'expression se déclare avec
> `Index("nom", text("lower(colonne)"))` et ne produit, lui non plus, aucun bruit.
>
> Enfin : **avant de supprimer un index, lire le code des applications qui lisent la
> base**, pas seulement `idx_scan`. C'est le code de ManaMind_AI et de RELIC-Trade qui a
> établi qu'aucune requête n'atteignait `printed_name` par sa valeur brute — un compteur
> à zéro n'aurait prouvé qu'une absence d'usage *récent*.
