# Recherche par nom traduit — indexation de `printed_name`

> Migrations `20260824_printed_name_trgm` et `20260824_printed_name_lower`
> · mesuré le 24/08/2026 sur PostgreSQL 18.4
>
> Deux index servent cette colonne, et ils ne se recouvrent pas : le **GIN
> trigram** pour les `ILIKE` à joker (recherche et autocomplétion), l'index
> **fonctionnel `lower()`** pour les égalités de la résolution de decklist. Un
> btree sur la colonne brute, qui ne servait ni l'un ni l'autre, a été supprimé.

## Le problème

RELIC-Trade (dépôt MTG-TRADE-FAB) lit cette base et cherche les cartes par nom
traduit avec des `ILIKE` à joker sur `scryfall_card_printings.printed_name`
(528 180 lignes, 850 Mo). Un btree ne peut rien pour ces requêtes : `ILIKE` est
insensible à la casse et le joker de tête interdit la recherche par préfixe.

Résultat avant migration : **la table entière était lue à chaque frappe** —
82 491 buffers, soit 645 Mo, pour ramener 4 lignes.

## La solution

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY ix_scryfall_card_printings_printed_name_trgm
    ON scryfall_card_printings USING gin (printed_name gin_trgm_ops);
```

pg_trgm découpe chaque valeur en trigrammes et les indexe : c'est le seul moyen
pour PostgreSQL de servir un `LIKE '%…%'` par un index.

Construction mesurée : **3,0 s**, avec `maintenance_work_mem` monté à 512 Mo le
temps du build (il est à 64 Mo par défaut, insuffisant pour un GIN).

## Protocole de mesure

Sans lui, les chiffres ne sont pas comparables — un `Parallel Seq Scan` cumule
les buffers de ses workers et un cache chaud divise les temps par trois :

```sql
SET max_parallel_workers_per_gather = 0;
EXPLAIN (ANALYZE, BUFFERS) …   -- 3 exécutions, médiane, hit et read séparés
```

## Avant / après

| Requête | Avant | Après | Gain |
|---|---|---|---|
| `ILIKE '%feuervogel%'` (10 car., libre) | 288,7 ms · 82 491 buffers (8 437 hit / 74 054 read) | **0,2 ms** · 36 buffers (36 hit / 0 read) | **×1 444** |
| `'feu%' OR '% feu%' OR '%-feu%'` (3 car., ancrée) | 527,9 ms · 82 491 buffers | **1,6 ms** · 822 buffers | **×330** |
| `'ni%' OR '% ni%' OR '%-ni%'` (2 car., ancrée) | 1 146,0 ms · 82 491 buffers | **1,9 ms** · 1 269 buffers | **×603** |
| `ILIKE '%ni%'` (2 car., libre) | 576,6 ms · 82 491 buffers | 335,7 ms · 82 491 buffers | **aucun** |

Les plans passent de `Seq Scan` à `Bitmap Index Scan`, et de 645 Mo lus sur
disque à quelques centaines de Ko déjà en cache.

### Ce qui ne profite PAS de l'index

Seule la **dernière ligne** du tableau : une saisie courte en recherche **libre**
(`%ni%`). C'est une limite de pg_trgm, pas un défaut de la migration.
`show_trgm()` le montre :

| Saisie | Trigrammes produits |
|---|---|
| `feuervogel` | `{"  f"," fe","el ",erv,eue,feu,gel,oge,rvo,uer,vog}` |
| `feu` | `{"  f"," fe","eu ",feu}` |
| `ni` | `{"  n"," ni","ni "}` — **tous avec padding** |
| `n` | `{"  n"," n "}` |

Une saisie de 2 caractères ne produit que des trigrammes de **padding**
(espaces de bordure). En recherche **ancrée**, ils sont exploitables : PostgreSQL
sait où commence le mot, et `'ni%'` passe de 1 146 ms à 1,9 ms — c'était le cas
le plus lent avant migration. En recherche **libre**, ils ne le sont pas : le
moteur ignore où le motif tombe dans la chaîne, et le seq scan reste inévitable.

L'écran principal impose `min_length=2` et **ancre** les saisies de moins de
5 caractères : le chemin nominal est donc entièrement couvert. Seul un écran de
back-office à faible volume descend à 1 caractère.

## Impact sur l'import bulk

C'était le vrai risque : un index GIN ralentit les upserts massifs.

**Mesuré sur un cluster PostgreSQL 18.4 jetable** (`initdb` sur le port 55432,
jamais sur `manamind`), table restaurée par `pg_dump -Fc` puis `pg_restore`, en
rejouant `_upsert_printings()` de `scripts/import_scryfall.py` — batch de 500,
`ON CONFLICT DO UPDATE` sur 21 colonnes, soit exactement le chemin de l'import.

50 000 lignes, `gin_clean_pending_list()` inclus dans le total. Le bench a porté
sur la variante partielle, alors encore retenue ; les deux variantes ne diffèrent
que de 0,9 % en taille et n'ont montré aucun écart d'écriture mesurable :

| Scénario | Sans index | Avec index | Surcoût |
|---|---|---|---|
| A — upsert à valeurs identiques (cas nominal du bulk quotidien) | 11,1 s | 11,9 s | **+7,0 %** |
| B — upsert modifiant `printed_name` | 11,3 s | 12,2 s | **+8,1 %** |
| C — insertion de lignes neuves (pire cas) | 10,9 s | 11,6 s | **+6,1 %** |

Extrapolé aux 528 180 lignes de la table : **+7 à +9,5 s** sur un import de
~7 min (420 s), soit **+2 %**. Le seuil de bascule était fixé à 12 min :
**on est très largement en zone verte, aucune action n'est requise.**

`fastupdate` est ON par défaut sur GIN — les écritures passent par une pending
list vidée plus tard. Le vidage a été mesuré séparément (0,1 s) pour ne pas
créditer l'index d'un coût simplement différé : il est négligeable, inutile de
toucher à `gin_pending_list_limit`.

> **Piège de mesure à connaître.** Rejouer un upsert à valeurs identiques déclenche
> des mises à jour **HOT** (Heap-Only Tuple) : aucune colonne indexée ne change,
> donc PostgreSQL ne touche aucun index et le surcoût apparaît nul. Un premier
> bench affichait ainsi « -2 % », artefact pur. Les scénarios B et C ci-dessus ont
> été ajoutés pour forcer l'écriture réelle de l'index — et le contrôle se fait sur
> `n_tup_hot_upd` dans `pg_stat_user_tables` (0 % de HOT en B et C).
>
> Corollaire : si l'index est un jour re-benché, **varier la valeur écrite à chaque
> run**. Deux runs appliquant la même modification retombent en HOT au second.

## Pourquoi un index complet, et non partiel

Un `WHERE printed_name IS NOT NULL` a été envisagé puis **écarté**. L'idée était
d'exclure les 23,9 % de lignes sans nom traduit (126 439 sur 528 180) pour alléger
l'index et l'import.

**La mesure a invalidé le raisonnement.** À données égales :

| Variante | Taille | Écart |
|---|---|---|
| Index complet | 19 890 176 octets | — |
| Index partiel `WHERE printed_name IS NOT NULL` | 19 709 952 octets | **0,9 %** |

GIN n'indexe pas les valeurs NULL : le prédicat ne lui retirait rien qu'il n'ait
déjà écarté. Le gain escompté n'existait pas.

Face à un bénéfice nul, c'est le coût qui décide. Un index partiel n'est pas
gratuit : le planner ne peut s'en servir que s'il **prouve** que le `WHERE` de la
requête implique le prédicat de l'index. C'est acquis pour `ILIKE` (opérateur
strict), mais c'est une contrainte permanente sur toutes les requêtes futures,
achetée pour 0,9 % d'espace. Le prédicat a donc été retiré avant le commit —
le changer après coup aurait imposé une migration et une reconstruction complète.

### Ce que le prédicat ne changeait PAS

Un candidat évident à la fragilité était `coalesce(printed_name, '')`, forme déjà
utilisée côté RELIC-Trade dans un `ORDER BY` de pertinence et susceptible de
migrer un jour vers un `WHERE`. Vérification faite, **elle ne discrimine pas** :

| Requête | Index partiel | Index complet |
|---|---|---|
| `printed_name ILIKE '%feuervogel%'` | Bitmap Index Scan | Bitmap Index Scan |
| `coalesce(printed_name,'') ILIKE '%feuervogel%'` | **Seq Scan** | **Seq Scan** |
| `printed_name ILIKE '%…%' OR printed_name IS NULL` | **Seq Scan** | **Seq Scan** |

Un index sur `printed_name` ne sert pas une expression `coalesce(printed_name,'')`,
quel que soit son prédicat : il faudrait un index fonctionnel sur l'expression
elle-même. Le retrait du prédicat se justifie par le principe — ne pas payer une
contrainte pour un gain mesuré nul — et non par ce cas précis.

## Taille de l'index

L'index occupe **36 Mo** après compactage de la table (voir ci-dessous).

### Une hypothèse qui a été testée, puis INFIRMÉE

Sur la table restaurée à neuf du cluster jetable, le même index sur les mêmes
528 180 lignes ne faisait que **19 Mo**. On a d'abord attribué l'écart au bloat de
la table de travail — 645 Mo de heap contre 340 Mo pour des données identiques —
en supposant que des TID étalés sur deux fois plus de pages dégradaient la
compression par delta des posting lists du GIN.

**La mesure a démenti cette explication.** Un `VACUUM FULL` a été passé sur la
table le 2026-08-24 :

| | Avant | Après |
|---|---|---|
| Heap | 645 Mo | **310 Mo** (−52 %) |
| Tous les index | 228 Mo | **119 Mo** (−48 %) |
| **Index trigram** | 38 Mo | **36 Mo** (−5 %) |

Le heap est descendu *sous* les 340 Mo du cluster jetable, et l'index trigram n'a
pratiquement pas bougé. Le bloat n'était donc pas la cause. Les autres index,
eux, ont bien été divisés par deux — c'est spécifiquement le GIN trigram qui ne
se compacte pas.

**L'écart 36 Mo / 19 Mo reste inexpliqué à ce jour.** Une piste non vérifiée : le
cluster de bench avait été créé avec `initdb --locale=C`, là où la base de travail
utilise une locale UTF-8. `pg_trgm` passe les valeurs en minuscules selon la
locale avant d'extraire les trigrammes, ce qui peut changer le nombre de
trigrammes distincts sur des noms accentués — la table contient des noms dans
toutes les langues. À confirmer avant d'en faire une conclusion.

Ne pas propager l'explication par le bloat : elle est fausse.

### Le REINDEX, lui, compacte

En production, un `REINDEX INDEX CONCURRENTLY` après le rechargement de 528 180
lignes a ramené l'index trigram de **59 à 33 Mo en 81 s**, sans bloquer le site.
C'est le bon outil quand un GIN a grossi sous l'effet de réécritures massives —
contrairement au `VACUUM FULL`, qui traite le heap mais laisse ce type d'index
presque inchangé.

## `lower(printed_name)` : index fonctionnel, et retrait du btree

> Migration `20260824_printed_name_lower`

### Ce que font réellement les consommateurs

La question « le btree sert-il à quelqu'un ? » a été tranchée non par les
compteurs, mais par le **code des deux applications qui lisent la base** :

| Application | Code | Forme | Btree utilisable ? |
|---|---|---|---|
| ManaMind_AI | `routers/collection.py:401` | `printed_name.ilike(f"{q}%")` | ✗ ILIKE est insensible à la casse |
| RELIC-Trade | `services/deck_resolution.py:114` | `func.lower(printed_name) == …` | ✗ porte sur une expression |
| RELIC-Trade | `services/deck_resolution.py:401` | `func.lower(printed_name).in_(…)` | ✗ porte sur une expression |

**Aucun accès à `printed_name` par sa valeur brute.** Le btree
`ix_scryfall_card_printings_printed_name` coûtait 26 Mo et une écriture à chaque
import, pour zéro lecture possible. Les compteurs disaient la même chose — 18
scans pour 5 863 492 tuples lus, soit 325 000 tuples par scan, la signature de
parcours quasi complets et non de lookups d'égalité.

Il a été **supprimé**, et remplacé par l'index qui manquait vraiment.

### L'index fonctionnel

```sql
CREATE INDEX CONCURRENTLY ix_scryfall_card_printings_printed_name_lower
    ON scryfall_card_printings (lower(printed_name));
```

Il sert la résolution de decklist de RELIC-Trade, jusque-là en `Seq Scan` :

| Requête | Avant | Après | Gain |
|---|---|---|---|
| `lower(printed_name) = …` (`deck_resolution.py:114`) | 261,5 ms · 82 491 buffers | **0,0 ms · 4 buffers** | table entière → 4 blocs |
| `lower(printed_name) IN (…)` (`deck_resolution.py:401`) | 283,6 ms · 82 491 buffers | **0,0 ms · 15 buffers** | idem |

Contrôle que le retrait du btree ne coûte rien à ManaMind_AI — sa requête
d'autocomplétion exacte, jointure comprise, s'exécute en **2,3 ms** (628 buffers)
en passant par l'index trigram : la forme ancrée `ILIKE 'q%'` exploite les
trigrammes de padding.

### Bilan d'espace : on a gagné en supprimant

| | |
|---|---|
| Btree supprimé | −26 Mo |
| Index fonctionnel créé | +12 Mo |
| **Net** | **−14 Mo**, et deux requêtes de 260 ms passées sous la milliseconde |

Le nouvel index fait 12 Mo là où le btree en occupait 26 pour les mêmes lignes :
même cause que pour le trigram, l'ancien index avait accumulé de la fragmentation
au fil des imports, le nouveau est neuf.

Total des index de la table : 243 → **228 Mo** ; total table : 887 → **873 Mo**.

## Exploitation

### La migration est rejouable sans être destructrice

`CREATE INDEX CONCURRENTLY` n'est pas transactionnel : un échec laisse un index
**INVALID** qui occupe l'espace et pèse sur les écritures sans servir aucune
requête. La migration se garde sur `pg_index.indisvalid` — elle ne reconstruit que
ce qui est absent ou cassé, et ne touche pas à un index déjà valide.

Détecter un résidu d'exécution interrompue :

```sql
SELECT c.relname
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
 WHERE NOT i.indisvalid;
```

### Fenêtre de création

`CONCURRENTLY` ne bloque pas les écritures mais fait deux passes complètes sur la
table et **attend la fin de toutes les transactions ouvertes**. L'import tourne à
08:00 et 20:00 pour ~7 min : lancer la création en dehors, après avoir vérifié
`pg_stat_activity`.

### ⚠️ Ne jamais supprimer l'extension

Le `downgrade()` ne supprime que l'index. `manamind` est partagée avec ManaMind_AI
et RELIC-Trade : si l'un d'eux crée un jour ses propres index trigram, un
`DROP EXTENSION pg_trgm CASCADE` les détruirait **en silence**. Une extension
orpheline ne coûte rien.

## Le bloat de la table : traité en local le 2026-08-24

`scryfall_card_printings` portait ~300 Mo d'espace libre accumulé par les
réécritures successives de l'import. Un `VACUUM FULL` l'a résorbé **en 24 s** sur
la base locale : heap 645 → 310 Mo, index 228 → 119 Mo, total 873 → **429 Mo**.

L'opération prend un `ACCESS EXCLUSIVE` : la table est inaccessible pendant toute
la réécriture. 24 s en local, mais il faut compter bien davantage sur une
instance modeste — et cela gèlerait RELIC-Trade et ManaMind_AI. En production, la
faire dans une fenêtre de maintenance, ou passer par `pg_repack` (non installé),
qui travaille sans verrou exclusif.

> À ne pas confondre : le `VACUUM FULL` compacte le heap et les btree, mais laisse
> le GIN trigram quasiment inchangé (−5 %). Pour celui-là, l'outil est
> `REINDEX INDEX CONCURRENTLY`, qui l'a ramené de 59 à 33 Mo en production.

## La recherche lente en production n'est PAS un problème d'index

Mesuré le 2026-08-24 sur l'API publique, après application des deux migrations,
rafraîchissement du catalogue **et** REINDEX :

| Requête API | Temps |
|---|---|
| `island` | 1,4 s |
| `goblin` | 12,6 s |
| `dragon` | 12,5 s |

Alors qu'**en base**, la même recherche est instantanée : `printed_name ILIKE
'%goblin%'` renvoie ses 1 042 lignes en **3,6 ms** par `Bitmap Index Scan`.

Le coût est ailleurs :

- l'instance Render est **12,7× plus lente** que le poste local en CPU pur
  (2 M `md5()` : 4,33 s contre 0,34 s), avec `work_mem` à 1,6 Mo,
  `shared_buffers` à 64 Mo et `effective_cache_size` à 192 Mo ;
- `search_grouped` évalue `relevance_rank_clause` sur **~5 000 impressions** pour
  « goblin » (3 909 par le nom anglais, 1 042 par le nom traduit), puis agrège en
  `GROUP BY oracle_id`. C'est du CPU et du tri, pas de la recherche d'index.

**Ne pas chercher la solution du côté de l'indexation** : trois interventions
successives (index trigram, index fonctionnel, REINDEX) n'ont pas fait bouger
`goblin`. Les pistes réelles sont le dimensionnement de l'instance et
l'optimisation de la requête de pertinence, toutes deux côté RELIC-Trade.
