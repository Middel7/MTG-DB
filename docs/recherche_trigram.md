# Recherche par nom traduit — index GIN trigram

> Migration `20260824_printed_name_trgm` · mesuré le 24/08/2026 sur PostgreSQL 18.4

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

## Taille de l'index, et une observation annexe

L'index occupe **38 Mo** : les index de la table passent de 205 à 243 Mo, et son
total de 850 à 887 Mo (+4,4 %).

Sur la table restaurée à neuf du cluster jetable, **le même index sur les mêmes
528 180 lignes ne fait que 19 Mo**. L'écart ne vient pas du build `CONCURRENTLY`
(vérifié : 19 Mo dans les deux modes), mais du **bloat de la table
de travail** : 645 Mo de heap contre 340 Mo pour des données identiques. Les TID
y sont étalés sur deux fois plus de pages, ce qui dégrade la compression par delta
des posting lists du GIN.

Autrement dit, la table porte ~300 Mo d'espace libre accumulé par les réécritures
successives de l'import, et cet index en paie le prix. C'est un sujet distinct de
cette migration (un `VACUUM FULL` ou `pg_repack` le résorberait, au prix d'un
`ACCESS EXCLUSIVE` qui bloquerait les deux applications lectrices) — signalé ici,
pas traité.

## Le btree `ix_scryfall_card_printings_printed_name` : à supprimer, mais pas encore

Ce btree de 26 Mo ne peut servir **aucune** des requêtes du consommateur : `ILIKE`
lui est inaccessible, et les `lower(printed_name) = …` portent sur une expression.
L'audit côté RELIC-Trade conclut qu'il est inutile, et les compteurs vont dans le
même sens — 18 scans pour 5 863 492 tuples lus, soit 325 000 tuples par scan :
la signature de parcours quasi complets, pas de lookups d'égalité.

**Il n'a pas été supprimé**, car les deux réserves posées lors de la décision ne
sont pas levées :

1. La confirmation doit venir de la **production**, pas de cette base locale dont
   les compteurs étaient pollués par les requêtes d'analyse. Les compteurs des
   deux index `printed_name` ont été remis à zéro le 24/08/2026 pour permettre
   l'observation :

   ```sql
   SELECT pg_stat_reset_single_table_counters('ix_scryfall_card_printings_printed_name'::regclass);
   -- puis, après quelques jours d'usage réel :
   SELECT indexrelname, idx_scan, idx_tup_read
     FROM pg_stat_user_indexes
    WHERE relname = 'scryfall_card_printings' AND indexrelname LIKE '%printed_name%';
   ```

   ⚠️ Passer l'OID de **l'index**, pas celui de la table : la fonction ne
   réinitialise que la relation qu'on lui désigne.

2. **ManaMind_AI lit la même base** et n'a pas été consulté. La question doit lui
   être posée avant toute suppression.

Si `idx_scan` reste à 0 en production et que ManaMind_AI ne s'y oppose pas, le
supprimer par une migration dédiée — geste unique : retirer `index=True` de
`printed_name` dans `src/mtgdb/db/models/card_printing.py` **et** le
`DROP INDEX CONCURRENTLY` correspondant.

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

## Hors périmètre — signalé, non traité

`lower(printed_name) = …`, chemin de repli de la résolution de decklist côté
RELIC-Trade, fait un seq scan à 542 ms. **Le GIN trigram ne le couvre pas** : c'est
une égalité sur une expression. Un index fonctionnel sur `lower(printed_name)` le
rendrait instantané, mais c'est une autre migration.
