# Passage des clés primaires en `bigint` — note de préparation

> **À ce jour, cette migration n'est PAS nécessaire.** Elle est documentée pour
> qu'elle soit faisable le jour venu sans redécouvrir ce qui suit.

## Pourquoi elle n'est plus urgente

Les colonnes `id` du catalogue Scryfall sont des `serial`, donc des `integer`
plafonnés à 2 147 483 647. Ce plafond est brutal : à l'atteindre, `nextval()`
lève et **toute insertion s'arrête** sur la table concernée.

Ce qui rapprochait l'échéance n'était pas la croissance des données mais le
gaspillage : `INSERT … ON CONFLICT` consomme un identifiant par ligne
**proposée**, y compris celles qui finissent en `UPDATE` ou qui ne font rien.
`nextval()` est évalué à la construction de la ligne candidate, avant la
détection du conflit, et rien ne la rend ensuite.

| Séquence | Valeur | Lignes | Ratio | Insertions réelles |
|---|---|---|---|---|
| `cards_id_seq` | 39 840 157 | 38 907 | 1024× | 544 |
| `card_printings_id_seq` | 40 830 218 | 542 876 | 75× | 14 696 |
| `card_faces_id_seq` | 1 868 018 | 6 459 | 289× | — |
| `mtg_sets_id_seq` | 85 887 | 1 052 | 82× | ~1/mois |

*Relevé le 19/09/2026 sur la base locale.*

Depuis le correctif de `mtgdb.scryfall.upserts`, un run qui ne trouve rien de
nouveau consomme **zéro** identifiant — vérifié sur un run complet : 542 827
impressions traitées, `+0` sur les deux séquences. L'échéance passe de quelques
années à un horizon sans intérêt pratique.

`mtgdb.db.sequences` journalise l'état à chaque run et alerte au-delà de 25 % du
plafond. C'est ce qui doit déclencher cette migration, et rien d'autre.

## Ce que le chronométrage a révélé

Mesuré sur la base locale (PostgreSQL 18.4, `maintenance_work_mem` à 64 Mo),
dans une transaction annulée — `ALTER TABLE` est transactionnel :

| Opération | Durée |
|---|---|
| `DROP` des deux vues | 0,0 s |
| `scryfall_cards.id` | 0,6 s |
| `scryfall_card_printings.card_id` | 12,8 s |
| `scryfall_card_faces.card_id` | 0,0 s |
| `scryfall_card_tags.card_id` | 1,2 s |
| `scryfall_card_printings.id` | 16,0 s |
| `scryfall_card_prices.printing_id` | 57,1 s |
| `CREATE` des deux vues + remplissage | 5,8 s |
| **Total** | **93,6 s** |

Tailles concernées : `scryfall_card_prices` 2 716 Mo, `scryfall_card_printings`
958 Mo, `scryfall_cards` 65 Mo, `scryfall_card_tags` 33 Mo.

**Sur la production, compter davantage** : l'instance Render est sur le plus petit
plan payant (256 Mo de RAM, 0,1 vCPU) contre une machine de développement. Un
facteur 5 à 10 est raisonnable, soit **8 à 16 minutes d'indisponibilité totale**
— la table est sous `ACCESS EXCLUSIVE`, ni lecture ni écriture. À chronométrer
sur une restauration de sauvegarde avant d'annoncer une fenêtre.

## Les trois pièges, tous découverts en chronométrant

### 1. Deux vues bloquent l'`ALTER`

```
cannot alter type of a column used by a view or rule
```

| Objet | Genre | Dépend de |
|---|---|---|
| `v_cardmarket_latest_prices_by_printing` | vue | `scryfall_cards.id`, `scryfall_card_printings.id` et `.card_id` |
| `card_min_price` | **vue matérialisée** | `scryfall_card_printings.card_id` |

`card_min_price` (2 336 kB) **n'appartient pas à MTG-DB** — elle n'est créée par
aucune de ses migrations. Prévenir son propriétaire avant la fenêtre : elle sera
supprimée et recréée.

`CREATE MATERIALIZED VIEW` **ne restitue pas les index** de la vue matérialisée.
Les relever avant, les recréer après.

### 2. Les clés étrangères imposent le périmètre

Toutes sont en `integer`. Migrer `id` sans elles laisserait des références
plafonnées à 2³¹ :

```
scryfall_cards.id
  ← scryfall_card_printings.card_id   (card_printings_card_id_fkey, ON DELETE CASCADE)
  ← scryfall_card_faces.card_id       (card_faces_card_id_fkey,     ON DELETE CASCADE)
  ← scryfall_card_tags.card_id        (scryfall_card_tags_card_id_fkey, ON DELETE CASCADE)
scryfall_card_printings.id
  ← scryfall_card_prices.printing_id  (card_prices_printing_id_fkey, ON DELETE CASCADE)
```

### 3. `serial` n'est pas `IDENTITY`

`ALTER COLUMN … TYPE bigint` ne touche pas la séquence sous-jacente. Sans

```sql
ALTER SEQUENCE cards_id_seq AS bigint;
ALTER SEQUENCE card_printings_id_seq AS bigint;
```

elle reste plafonnée à 2³¹ et la migration n'aura servi à rien. L'oubli ne se
verrait que le jour où la séquence bute.

## Marche à suivre le jour venu

1. **Chronométrer sur une restauration de sauvegarde**, pas sur la production, et
   avec le `maintenance_work_mem` de la production.
2. Relever les index de `card_min_price` et prévenir son propriétaire.
3. Annoncer la fenêtre à RELIC-Trade et ManaMind_AI : indisponibilité totale des
   tables du catalogue.
4. Une seule transaction : `DROP` des vues → les six `ALTER COLUMN` → `ALTER
   SEQUENCE` → recréation des vues et de leurs index. Tout est transactionnel :
   un échec ne laisse rien à moitié fait.
5. Vérifier `information_schema.columns` sur les sept colonnes, et
   `pg_sequences.data_type` sur les deux séquences.

**Ne pas faire d'expand/contract.** Les tables se réécrivent en minutes ; ajouter
une colonne miroir, la backfiller par lots et permuter coûterait plusieurs jours
pour éviter dix minutes d'indisponibilité, et demanderait quand même une fenêtre
au moment de la bascule.
