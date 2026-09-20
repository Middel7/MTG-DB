# Journal des modifications

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).
Les dates sont au format AAAA-MM-JJ.

---

## [Non publié] — 2026-09-20 (2) — Savoir quand la source a publié, pas seulement quand on a importé

Branche `feat/suivi-fraicheur-sources`.

### La question posée

« Quand Scryfall a-t-il proposé une mise à jour, quand Cardmarket a-t-il proposé
la sienne, et quand MTG-DB a-t-il été mis à jour ? » Les trois réponses
existaient en base, mais aucune n'était exploitable ensemble :

| Information | Où elle était | Pourquoi inutilisable |
|---|---|---|
| Publication Scryfall | `import_runs.source_updated_at` | Seulement pour les versions **importées** |
| Publication Cardmarket | `cardmarket_import_files.last_modified` | **Texte brut** (`Sun, 20 Sep 2026 00:42:36 GMT`) : ni triable ni soustrayable |
| Version publiée non encore absorbée | nulle part | C'est pourtant le seul cas où l'on veut être alerté |

### Ajouté

- **Table `mtgdb_source_publications`** : une ligne par **version publiée**, pas
  par vérification. Le pipeline passe 24 fois par jour, Scryfall publie 2 fois
  et Cardmarket 1 — c'est la contrainte `(source, version)` qui absorbe les
  répétitions. Préfixe `mtgdb_` parce que la base est partagée avec RELIC-Trade.
- **Colonne `last_seen_at`**, mise à jour à chaque passage. Sans elle, rien ne
  distingue « la source est calme » de « le cron ne tourne plus » : dans les
  deux cas, aucune ligne nouvelle n'apparaît. L'upsert utilise `RETURNING
  (xmax = 0)` pour distinguer une insertion d'une mise à jour, que PostgreSQL
  ne signale pas autrement.
- **Vue `mtgdb_fraicheur_sources`** : une ligne par source, avec le retard
  courant. Elle unifie `mtgdb_source_publications` et `import_runs` — Tagger
  n'ayant aucune notion de publication, son API étant interrogée en direct.
- **`scripts/fraicheur.py`** : tableau lisible, `--historique`, `--json`, et
  `--check` qui **sort en code 1** en cas de décrochage. Render envoyant un
  e-mail sur échec de cron job, l'alerte fonctionne sans qu'aucun identifiant
  SMTP n'ait à être stocké ni renouvelé.
- **Cron `mtgdb-veille-fraicheur`**, 08:00 UTC. Trois motifs d'alerte : version
  publiée en attente depuis plus de 6 h, source non interrogée depuis plus de
  3 h, tags vieux de plus de 9 jours. Seuils réglables en ligne de commande.
- 33 tests.

### Modifié

- **Le job catalogue passe de quotidien à horaire** (`0 * * * *`). Rendu
  possible par les 14 min du run et par le fait que le script est déjà sa
  propre sonde : Scryfall compare `source_file`, Cardmarket compare l'ETag par
  un HEAD, Game Changers ne touche plus que 53 lignes. Un passage sans
  nouveauté coûte ~40 s. Le catalogue suit désormais la source à moins d'une
  heure, au lieu de 24 h, et ne rate plus une publication Scryfall sur deux.
  Coût : ~24 h d'exécution par mois, sous le minimum de facturation.
- Le service garde son nom `mtgdb-catalogue-quotidien` bien qu'il soit horaire :
  renommer un service dans un blueprint en crée un nouveau, ce qui perdrait
  l'historique des runs et demanderait de ressaisir `DATABASE_URL`.
- Les tests du verrou **se sautent** au lieu d'échouer quand un import tient le
  verrou. Lancés pendant un run, ils échouaient tous les six sur un message qui
  ne désignait pas la cause.

### Réparé au passage

**L'arbre Alembic avait deux têtes.** `20260920_printing_image_status` et
`20260920_card_parts` partageaient le parent `20260919_tagger_checked_at`.
Conséquence : `alembic upgrade head` échoue avec « Multiple head revisions are
present » — alors que la procédure de migration de production documentée dans
`docs/deploiement.md` utilise précisément `head` au singulier. La divergence
serait apparue au pire moment, en pleine mise à jour de schéma en production.
Recousu par `20260920_merge_branches`, qui ne porte aucun changement.

### Principe retenu

Le suivi ne doit **jamais** faire échouer un import. Deux conséquences dans le
code : chaque écriture de traçabilité ouvre sa **propre session** — un `commit()`
au milieu de la transaction de `download_file()` validerait son travail à demi
et expirerait ses objets ORM — et toute exception est avalée avec un
avertissement. Perdre une ligne de suivi est sans gravité ; perdre un import de
540 000 impressions ne l'est pas.

---

## [Non publié] — 2026-09-20 (1) — Le catalogue sait enfin qu'une image n'est pas un scan

Branche `feat/image-status`. Demandé par RELIC-Trade, dont la vitrine affichait un
carton à la place d'une carte.

### Le défaut

Scryfall sert une image pour **toute** impression, y compris celles qu'il n'a
jamais scannées : dans ce cas l'URL renvoie un carton « Localized Image Not
Available ». Mesuré le 2026-09-20 sur Misdirection (MMQ #87) :

| Impression | Réponse HTTP | Taille | `image_uris` en base | `image_status` (API Scryfall) |
|---|---|---|---|---|
| Anglaise | `200`, `image/jpeg` | 135 Ko | renseignée | `highres_scan` |
| Française | `200`, `image/jpeg` | 67 Ko | renseignée | **`placeholder`** |

Aucune redirection, aucune erreur, un vrai JPEG des deux côtés, et les huit
langues de cette impression ont leurs trois colonnes `image_*` renseignées.
**Rien dans le catalogue ne distinguait donc un scan d'un carton.** Un
consommateur qui choisit une impression par sa langue — la vitrine « Cartes
recherchées » de RELIC-Trade affiche le visuel dans la langue de l'interface —
affichait le carton sans aucun moyen de s'en apercevoir : ni `404` à intercepter,
ni champ à tester.

### Ce qui change

`scryfall_card_printings.image_status` porte la qualité déclarée par Scryfall
(`highres_scan`, `lowres`, `placeholder`, `missing`). Un consommateur peut alors
écarter les impressions sans visuel réel et replier sur une autre langue.

La colonne est lue sur la **carte**, jamais sur la face : Scryfall qualifie
l'impression entière, alors que `image_uris` peut venir d'une face (cartes double
face). L'y lire la rendrait nulle sur toutes les DFC.

⚠️ **La colonne reste `NULL` jusqu'au prochain import Scryfall.** Les
consommateurs doivent traiter `NULL` comme « qualité inconnue » et se comporter
comme avant — sinon, entre la migration et le réimport, ils écarteraient la
totalité du catalogue.

### Garde-fous ajoutés

`tests/test_colonnes_impression_alignees.py` relie les trois déclarations qui
décrivaient une impression **séparément** : le parseur, le modèle et la liste
`COLONNES_IMPRESSION` que l'`UPDATE` réécrit. Rien ne les reliait, et l'oubli
correspondant a la pire forme qui soit : une colonne absente de
`COLONNES_IMPRESSION` est bien écrite à la **création** d'une impression — donc
sur une base neuve et dans tous les tests — mais **jamais mise à jour** ensuite.
Sur une production où les impressions existent déjà toutes, elle resterait vide
indéfiniment, sans la moindre erreur.

### Migration

`20260920_printing_image_status` — `ADD COLUMN` NULLable, opération de métadonnée,
sans réécriture des 542 876 impressions. Aucun index : la colonne se lit sur des
lignes déjà sélectionnées, jamais comme critère d'entrée.

---

## [Non publié] — 2026-09-19 (7) — Les upserts ne brûlent plus d'identifiants

Branche `perf/consommation-sequences`. Point soulevé par les audits croisés de
ManaMind_AI et de RELIC-Trade, confirmé ici.

### Le défaut

`INSERT … ON CONFLICT` consomme une valeur de séquence pour chaque ligne
**proposée**, y compris celles qui finissent en `UPDATE` ou qui ne font rien :
`nextval()` est évalué à la construction de la ligne candidate, bien avant que le
conflit ne soit détecté, et rien ne la rend ensuite.

| Séquence | Valeur | Lignes | Ratio | Insertions réelles |
|---|---|---|---|---|
| `cards_id_seq` | 39 840 157 | 38 907 | **1024×** | 544 |
| `card_printings_id_seq` | 40 830 218 | 542 876 | **75×** | 14 696 |
| `card_faces_id_seq` | 1 868 018 | 6 459 | 289× | — |
| `mtg_sets_id_seq` | 85 887 | 1 052 | 82× | ~1/mois |

Même constat sur `relictrade` : 655× et 47×, avec **91 insertions pour 922 554
updates**. Les colonnes `id` sont des `integer` : c'était ce gaspillage, et non la
croissance des données, qui fixait l'échéance d'épuisement du plafond.

### Le correctif

Les quatre écritures séparent l'existant du nouveau : un `SELECT` de la clé
métier par lot, un `INSERT` des seules nouvelles, puis un `UPDATE … FROM
(VALUES …)` pour le reste — qui ne consomme aucun identifiant.

> `INSERT … SELECT … WHERE NOT EXISTS` **ne suffirait pas** : le `DEFAULT
> nextval()` s'évalue à la projection du `SELECT`, avant le filtre. Il faut ne pas
> proposer la ligne du tout.

### Mesuré sur un run complet

| | Avant | Après |
|---|---|---|
| `cards_id_seq` | +520 790 | **+0** |
| `card_printings_id_seq` | +520 790 | **+0** |
| `UPDATE` sur les deux tables | ~520 790 | **+0** |
| Impressions traitées | 542 827 | 542 827 |
| Erreurs | 0 | 0 |

Un run qui ne trouve rien de nouveau n'écrit plus rien.

### Deux défauts que seul un vrai run pouvait montrer

1. `values()` avec un dict positionnel **et** un kwarg : refusé par SQLAlchemy.
2. `operator does not exist: integer = text`. Déclarer le type d'une colonne ne
   suffit pas à typer le SQL émis — SQLAlchemy n'ajoute un `::type` que pour
   certains types. Quand toutes les valeurs d'une colonne du lot valent `NULL`
   (`edhrec_rank`, `printed_name`, `cardmarket_id` le sont couramment),
   PostgreSQL la type en `text`. Corrigé par un `CAST` explicite.

Le second a coûté **117 327 cartes perdues** sur un run de contrôle — et le
mécanisme ajouté en livraison (4) l'a signalé : statut `partial`, code 1. Sans
lui, ces pertes seraient passées pour un succès.

### Surveillance

`mtgdb.db.sequences`, journalisé en fin de run. Une régression serait autrement
invisible : tout continuerait de fonctionner, et le problème ne se manifesterait
que le jour où une séquence bute et bloque toute insertion.

La séquence est reliée à sa colonne par `pg_depend`, non par comparaison de noms :
un `LIKE '%' || sequencename || '%'` apparie à tort, `cards_id_seq` étant contenu
dans `deck_cards_id_seq`. Le filtre sur les colonnes `integer` écarte au passage
`deck_cards_id_seq` — la plus avancée de la base (124 289 334, 5,79 % du plafond)
mais dont la colonne est **déjà un `bigint`**.

### Migration `bigint` : documentée, pas nécessaire

[`docs/migration_bigint.md`](docs/migration_bigint.md). Chronométrée en
transaction annulée : **93,6 s** en local pour les six colonnes et les deux vues.
Trois pièges y sont consignés, tous découverts en mesurant — deux vues bloquantes
dont une vue matérialisée qui n'appartient pas à ce dépôt, le périmètre imposé par
les clés étrangères, et le fait qu'un `serial` exige un `ALTER SEQUENCE … AS
bigint` séparé.

---

## [Non publié] — 2026-09-19 (6) — Suites d'audit : performance et dette

Branche `perf/p2-upsert`.

### Le sur-upsert, mesuré puis supprimé

Trois gisements, tous relevés dans `pg_stat_user_tables` sur la base locale.

| Table | INSERT cumulés | UPDATE cumulés | Lignes réelles |
|---|---|---|---|
| `scryfall_cards` | 544 | **27 073 993** | 38 907 |
| `scryfall_card_printings` | 11 449 | **43 785 680** | 539 629 |
| `scryfall_card_faces` | 1 152 858 | 0 (DELETE+INSERT) | 6 459 |

1. **Cartes.** La déduplication par `oracle_id` était locale au lot de 500 : un
   terrain de base présent dans 800 lots était upserté 800 fois. Un cache
   `oracle_id → id` partagé par tout le run la rend globale.
2. **Impressions.** L'upsert porte désormais un `WHERE … IS DISTINCT FROM` sur
   la valeur **cible**, `coalesce` comprise — sans quoi une impression dont le
   bulk ne fournit pas le `cardmarket_id` serait vue comme modifiée à chaque
   passage, et l'on retomberait sur le problème d'origine. C'est le « dernier
   gros gisement » que la livraison (3) identifiait sans le traiter.
3. **Faces.** `_replace_faces()` procède par DELETE puis INSERT ; le rejouer à
   chaque lot ne changeait rien à la donnée et ne produisait que des tuples
   morts. Traitées une seule fois par carte et par run.

### Les exports Cardmarket ne sont plus chargés en mémoire

`ijson` figurait dans les dépendances depuis l'origine **sans avoir jamais été
utilisé** : le bulk Scryfall est passé au JSONL gzippé, qui se lit ligne à ligne,
et personne n'est revenu sur les fichiers Cardmarket.

Mesuré sur le price guide du 19/09, 127 379 entrées :

| | Pic mémoire |
|---|---|
| `json.load()` | 109,2 Mo |
| streaming `ijson` | **0,5 Mo** |

> ⚠️ Piège rencontré, et silencieux jusqu'à l'INSERT : par défaut ijson rend les
> nombres en `Decimal` là où `json.load()` rendait des `float`. L'objet brut part
> tel quel dans la colonne JSONB `raw_json`, que `json.dumps()` ne sait pas
> sérialiser — **tous** les imports de Price Guide auraient échoué, les prix
> Cardmarket étant des nombres JSON (`"avg":0.09`) et non des chaînes. D'où
> `use_float=True`, vérifié sur le fichier réel et verrouillé par un test.

### Mesuré sur un run réel, base locale, même bulk

Run `#118`, `--force`, 228 s.

| | Avant | Après |
|---|---|---|
| Impressions upsertées | 520 790 | **542 827** |
| `cards_imported` | 520 790 | **38 906** |
| `UPDATE` sur `scryfall_cards` | ~520 790 | **0** |
| `UPDATE` sur `scryfall_card_printings` | ~520 790 | **10 855** |
| `INSERT` sur `scryfall_card_faces` | ~23 000 | **6 459** |
| `cards_id_seq` consommée | +520 790 | **+38 906** |

Zéro `UPDATE` sur `scryfall_cards` : aucune carte n'avait changé, et l'upsert
n'écrit plus rien dans ce cas. Sur les impressions, 10 855 lignes réellement
modifiées sur 542 827 proposées — soit **2 %** au lieu de 100 %.

**3 247 impressions sont entrées en base** à ce seul run : celles que la
déduplication écartait depuis toujours.

> ⚠️ `card_printings_id_seq` consomme toujours une valeur par ligne **proposée**,
> `ON CONFLICT` compris : +542 827 par run, contre +520 790 avant. La colonne est
> un `integer` et la séquence est à 40 287 391, soit **1,9 % de son plafond** —
> environ 5,3 ans au rythme local. Le passage en `bigint` reste à faire ; il
> réécrit la table et demande une fenêtre annoncée.

### Conséquences visibles

- `cards_imported` converge vers ~38 900 au lieu de ~520 000 : il compte enfin
  les cartes, non les lignes du bulk ;
- `updated_at` cesse d'avancer sur les lignes réellement inchangées. **À
  vérifier chez les consommateurs** s'ils s'en servaient comme signal de
  fraîcheur — `import_runs.finished_at` est la source correcte pour cela.

### Réparation des prix orphelins historiques

Le correctif de la livraison (5) empêche de créer de nouvelles lignes orphelines,
mais ne retouche pas les 252 414 existantes. `scripts/reparer_prix_orphelins.py`
s'en charge, avec `--dry-run` et par lots : 524 lignes dont le produit existait
déjà, 251 890 dont le produit — 5 085 au total — a disparu du catalogue
Cardmarket et doit être reconstitué.

Rien n'est inventé : `raw_json` conserve l'`idProduct` d'origine, le rattachement
n'est qu'une relecture. Les lignes dont le `raw_json` ne porte pas d'identifiant
numérique restent intactes et sont signalées.

> ⚠️ Ce script crée des `cardmarket_products` à `en_name` vide, en attendant que
> le Product Catalog les renseigne. **À vérifier chez les consommateurs** avant
> de l'exécuter en production : ManaMind_AI et RELIC-Trade lisent cette table, et
> un code qui suppose `en_name` non vide afficherait mal ces lignes.

Appliqué sur la base locale le 19/09/2026 (0 orpheline restante, 127 384
produits). **Pas** sur la production.

> Leçon retenue dans les tests : un script dont la portée est la table entière ne
> peut pas être testé sur une base partagée — l'appeler depuis un test répare tout
> ce qu'il trouve. `tests/test_reparation_prix_orphelins.py` crée donc sa propre
> base, y joue les migrations, et la détruit.

### Divers

- `Decimal` au lieu de `float` pour les prix Scryfall, par cohérence avec le
  pipeline Cardmarket qui le faisait déjà ;
- reset de `game_changer` limité aux lignes concernées (38 907 réécrites pour 53
  utiles) ;
- `docs/Launch.txt` : deux informations fausses corrigées — la tâche des tags est
  à 05:00 et non 03:00, et `docker compose run --rm updater --skip tags` ne peut
  pas fonctionner (l'image déclare un `CMD`, Docker chercherait un binaire nommé
  `--skip`) ;
- le README affirmait que les tests d'intégration n'écrivent rien : c'est faux
  depuis l'origine ;
- `rapport_audit_hebergement.txt` déplacé dans `docs/archives/` avec un en-tête
  qui signale ses chiffres périmés.

---

## [Non publié] — 2026-09-19 (5) — La base redevient reconstructible

Branche `feat/p1-ci-tests`.

### La CI a payé immédiatement

Le premier `alembic upgrade head` sur une base **vierge** a échoué :

```
sqlalchemy.exc.ProgrammingError: table "card_pricing_rules" does not exist
```

`20260609_drop_unused_tables` supprime une table qu'**aucune migration ne crée** —
elle n'existait que sur la base historique. La chaîne était donc injouable depuis
zéro : impossible de monter un environnement de test, de recette, ou de
reconstruire après un sinistre. Personne ne l'avait vu, faute d'avoir jamais
essayé.

`alembic check` a ensuite révélé un second écart : le modèle déclare un
`server_default CURRENT_DATE` sur `scryfall_card_prices.date` qu'aucune migration
ne pose. La base historique l'a — ajouté hors migration, comme le notait
`docs/migrations.md` — une base reconstruite ne l'aurait pas. Les deux schémas
divergeaient silencieusement. Migration `20260919_default_prix_date` ajoutée.

> ⚠️ Au passage : l'identifiant d'une révision doit tenir dans les **32
> caractères** de `alembic_version.version_num`. `20260826_cardmarket_id_expansion`
> en fait exactement 32. Un identifiant trop long échoue au tout dernier `UPDATE`,
> après que la migration a pourtant été exécutée.

La CI vérifie désormais ces deux propriétés à chaque push, sur `postgres:16` —
la version des hébergeurs visés, et non celle du poste (18.4).

### Les prix Cardmarket orphelins

Un produit absent du catalogue voyait son `id_product` mis à `NULL` pour esquiver
la clé étrangère, et sa ligne de prix insérée quand même. **252 414 lignes**
(4 % de la table) étaient dans cet état, rattachables à rien.

Trois dégâts, dont un non évident : `ON CONFLICT (import_file_id, id_product)`
cessait d'agir, puisqu'en SQL un `NULL` n'entre jamais en conflit avec un autre
`NULL`. Rejouer le même fichier dupliquait ces lignes.

Le produit manquant est désormais **créé** — ligne minimale, enrichie au prochain
passage du Product Catalog. Le cas est fréquent : les deux fichiers sont
téléchargés séparément, et le catalogue est souvent `skipped_not_modified` alors
que le price guide contient déjà les nouveautés du jour.

### Robustesse des téléchargements

- Écriture en `.part` puis renommage atomique : un fichier tronqué ne peut plus
  passer pour complet. Le run suivant se contentait de `dest.exists()` ;
- contrôle de taille contre le `Content-Length` annoncé ;
- le **sha256**, calculé mais jamais comparé alors qu'il porte une contrainte
  d'unicité `(file_type, sha256)`, sert enfin à la déduplication qu'il permettait
  — et cesse de provoquer une `IntegrityError` non capturée quand le `HEAD`
  échoue et que la comparaison d'ETag est sautée ;
- nettoyage des `cardmarket_import_files` restés `started` (une du 07/06 traînait
  encore).

### Observabilité

- La ligne de progression de l'import s'affichait **0 ou 1 fois par run**, mesuré
  sur 29 journaux consécutifs : son seuil (`cards_imported % 2_000`) supposait un
  compteur avançant par pas de 500, alors qu'il avance d'un nombre variable.
  Comptée en lots désormais ;
- l'écart entre lignes lues dans le bulk et impressions écrites est tracé à
  chaque run — c'est exactement ce chiffre qui manquait pour voir le défaut
  corrigé en (4) ;
- rapport de croissance de l'historique des prix, dont la rétention dépend d'un
  script **extérieur à ce dépôt**.

### Qualité

Ruff était configuré mais **absent des dépendances** : le lint n'était pas
exécutable, et le jeu de règles par défaut de l'outil varie d'une version à
l'autre. Épinglé, avec un `select` explicite.

**60 tests ajoutés** : parseurs Cardmarket et leurs 4 à 6 orthographes par champ,
idempotence du price guide contre PostgreSQL, échec de l'étape tags, sélection
des étapes de l'orchestrateur.

---

## [Non publié] — 2026-09-19 (4) — 22 037 impressions perdues à chaque run

Branche `fix/p0-integrite`. Défaut le plus grave trouvé par l'audit du 19/09.

### Le défaut

`_flush_batch()` appliquait aux **impressions** la déduplication conçue pour les
**cartes**. Toutes les impressions partageant un `oracle_id` à l'intérieur d'un
même lot de 500 étaient jetées, sauf une.

Mesuré en rejouant l'algorithme sur le bulk réel du 19/09 :

| | Lignes |
|---|---|
| Lignes du bulk avec `oracle_id` | 542 827 |
| Impressions réellement upsertées | 520 790 |
| **Écartées** | **22 037 (4,06 %)** |

Le bulk étant trié par `scryfall_id` (UUID, donc ordre pseudo-aléatoire), les
collisions étaient fréquentes — surtout sur les terrains de base, qui comptent des
centaines d'impressions. La base comptait **539 629** impressions contre 542 827
dans le bulk.

Rien ne le signalait : le compteur publié était celui des survivantes. La
signature du défaut — `cards_imported == printings_imported`, égalité pourtant
impossible pour 38 907 cartes et 539 629 impressions — figurait dans **chaque
journal depuis l'origine**.

> Effet de bord traité dans le même geste : plusieurs impressions d'une même carte
> cohabitent désormais dans un lot, et `_replace_faces()` aurait inséré les mêmes
> faces autant de fois. `scryfall_card_faces` n'a aucune contrainte d'unicité pour
> l'en empêcher.

### Trois façons dont un échec passait pour un succès

- **Scryfall** rendait `0` en statut `partial`. Un run ayant perdu 300 000 cartes
  produisait exactement le même signal d'exploitation qu'un run parfait ;
- **Tagger** confondait « carte inconnue » et « Tagger en panne » sous un même
  `return None` : le compteur d'erreurs restait à zéro même quand 100 % des
  requêtes échouaient. L'étape ne laissait par ailleurs **aucune trace en base** —
  rien ne disait quand les tags avaient été rafraîchis pour la dernière fois ;
- **`update_all.py`** ne configurait aucun `logging` : les messages de
  `mtgdb.db.lock`, dont « Verrou perdu ET repris par un autre run », partaient sur
  stderr via le handler de dernier recours, sans jamais atteindre le fichier de
  journal.

### Garde-fou sur les downgrade destructeurs

`downgrade` n'est pas un rollback sur une base partagée.
`20260609_rename_scryfall_tables` renomme `scryfall_cards` → `cards` et casse
instantanément les trois consommateurs ; `20260609_refactor_translations`
détruit `printed_name`. Les deux refusent désormais de s'exécuter, sauf
`MTGDB_ALLOW_DESTRUCTIVE_DOWNGRADE=1`.

---

## [Non publié] — 2026-09-19 (3) — Le bulk n'efface plus les cardmarket_id

`cardmarket_id` n'est fourni par Scryfall que sur l'impression anglaise.
L'upsert l'écrasait tel quel, remettant **401 230 impressions non anglaises à
`NULL` à chaque run** — que `propagate_cardmarket_ids()` repeuplait juste après
en recopiant la valeur depuis l'impression anglaise. 77 % de la table réécrite
deux fois par run pour revenir au point de départ.

### Modifié

- `_upsert_printings()` : `cardmarket_id = coalesce(excluded.cardmarket_id,
  scryfall_card_printings.cardmarket_id)`. Une seule colonne est concernée,
  listée dans `PRESERVE_IF_NULL`.

### Mesuré sur la base locale, même bulk

| | Avant | Après |
|---|---|---|
| Lignes propagées | 401 359 | **0** |
| Durée de la propagation | 68 s | **1 s** |
| Import des cartes | 6 min 30 | 4 min 50 |
| **Étape Scryfall complète** | **466 s** | **295 s** (−37 %) |

Aucune donnée perdue : 519 124 impressions portent un `cardmarket_id` avant
comme après. L'import des cartes gagne lui aussi, l'upsert ne réécrivant plus
ces 401 230 lignes — donc moins de WAL et moins de tuples morts.

Report attendu en production, où la propagation prend 24 à 25 min : étape
Scryfall de **84 min à 45-60 min**, à confirmer par un vrai run.

### Conséquence assumée

Un `cardmarket_id` ne peut plus être **effacé** par le bulk. Si Scryfall retire
l'identifiant d'un produit délisté, l'ancienne valeur subsiste. C'était déjà
largement le cas — la propagation la recopiait depuis une impression voisine —
et le rapport de liaison Cardmarket surveille cet écart (10 lignes sans
correspondance au 19/09).

### Non traité

L'upsert réécrit toujours 520 463 tuples même quand rien n'a changé. C'est le
dernier gros gisement, et il mérite sa propre livraison.

---

## [Non publié] — 2026-09-19 (2) — Résistance aux coupures de base

Le premier run de production sur Render a échoué après 70 minutes, quand la base
a cessé d'accepter les connexions (`the database system is not yet accepting
connections / Consistent recovery state has not been yet reached`). Deuxième
perte en trois jours après celle du 17/09 depuis le poste (`SSL connection has
been closed unexpectedly`, 78 min). Dans les deux cas, l'interruption a duré
moins longtemps que le travail jeté.

L'enchaînement exact du 19/09 : un batch échoue → le `session.rollback()` du
bloc de rattrapage tente de rouvrir une connexion et lève **à son tour** →
l'exception remonte hors de tout `except` → le `session.commit()` final échoue
pour la même raison → le processus meurt sans pouvoir marquer le run `failed`,
qui reste `running` indéfiniment.

### Ajouté

- `src/mtgdb/db/retry.py` : détection des erreurs de transport et rejeu avec
  attente croissante (5 s, 15 s, 30 s, 1 min, 2 min). Les erreurs de données —
  contrainte, syntaxe — ne sont jamais rejouées.
- `_safe_rollback()` dans `scripts/import_scryfall.py` : remet la session en
  état sans jamais lever, et recycle le pool pour ne pas réutiliser un socket
  mort.
- `fail_orphan_runs()` : au démarrage, tout run `running` depuis plus de 6 h est
  marqué `failed`. Le seuil est une sécurité ; la garantie réelle vient du
  verrou advisory.
- `finalize_run()` : écrit le statut final via une **session neuve**. Un
  `rollback` sur la session accidentée expire les objets ORM et ferait perdre
  silencieusement le statut qu'on vient d'y affecter.
- 21 tests supplémentaires, dont les messages d'erreur exacts des deux
  incidents, et un test d'intégration qui vérifie que la session du verrou n'est
  pas `idle in transaction`.

### Modifié

- Les batches d'import et les deux propagations sont désormais rejoués sur
  erreur transitoire, au lieu d'être comptés en erreur et abandonnés.
- **Statut `partial`** : un run qui a perdu des cartes n'est plus marqué
  `success`. Conséquences voulues — `bulk_already_imported()` ne le voit pas,
  donc le prochain run reprend ce bulk ; et la supervision de RELIC-Trade, qui
  compte les `success`, signale le décrochage. Auparavant, un run ayant perdu
  300 000 cartes était enregistré comme un succès.
- **Verrou en `AUTOCOMMIT`** : sans cela, SQLAlchemy ouvrait une transaction
  implicite au premier battement du heartbeat et ne la refermait jamais. La
  session restait `idle in transaction` pendant les deux heures du run, gelant
  l'horizon de `VACUUM` — les tuples morts des 520 000 lignes réécrites ne
  pouvaient plus être recyclés, ce qui aggravait l'IO qu'on cherche à réduire.
  Vérifié en base : `idle in transaction` avant, `idle` après.
- Le heartbeat **reprend** le verrou après une coupure, au lieu de se contenter
  de la signaler. Si un autre run l'a pris entre-temps, le run en cours continue
  malgré tout : un import à moitié appliqué est pire qu'un chevauchement, les
  upserts étant idempotents.

### Non traité

Les deux gaspillages repérés dans l'import restent à corriger, et méritent une
livraison séparée avec mesure entre chaque : `cardmarket_id` écrasé par `NULL`
à chaque run puis repeuplé (401 230 lignes, ~24 min), et l'upsert qui réécrit
520 463 tuples même quand rien n'a changé.

---

## [Non publié] — 2026-09-19 (1) — Bascule vers Render

### La production ne dépend plus d'un poste

La base Render est désormais alimentée par **deux Cron Jobs Render** qui
construisent le `Dockerfile` de ce dépôt (`render.yaml`). Les tâches planifiées
Windows qui visaient la production sont retirées ; les runs locaux vers la base
locale sont conservés tels quels.

Ce que cela règle : un PC éteint, en veille ou déconnecté ne prive plus la
production de mise à jour, et le lien WAN n'est plus un point de panne — le run
du 17/09 s'était interrompu après 78 minutes sur un
`SSL connection has been closed unexpectedly`.

Ce que cela ne règle pas, mesures à l'appui : **la durée**. Un run continuera de
prendre ~100 min. Le goulot est l'instance PostgreSQL (256 Mo de RAM, 0,1 vCPU
pour une base de 3,4 Go), pas la liaison — la part réseau et latence ne pèse que
~9 min sur 110. Voir `docs/deploiement.md`.

### Ajouté

- `render.yaml` : blueprint des deux cron jobs (`mtgdb-catalogue-quotidien`,
  quotidien à 01:00 UTC, `--skip tags` ; `mtgdb-tags-hebdomadaire`, dimanche à
  05:00 UTC, `--only tags`). Région `frankfurt`, imposée par l'URL interne de la
  base.
- `src/mtgdb/db/urls.py` : normalisation `postgres://` → `postgresql://`,
  détection d'une base locale, masquage du mot de passe dans les journaux.
- `src/mtgdb/db/lock.py` : verrou `pg_advisory_lock` avec heartbeat sur la
  connexion porteuse.
- `src/mtgdb/runtime.py` : détection de l'exécution en conteneur.
- `mtgdb.db.engine.assert_remote_database()` : refus d'une base locale quand le
  contexte exige une base distante.
- `tests/` : 47 tests (pytest). Ceux qui portent sur le verrou sont marqués
  `integration` et demandent une base ; ils n'écrivent rien.
- `CHANGELOG.md` (ce fichier).

### Modifié

- **Verrou anti-chevauchement** : `data/.update_all.lock` remplacé par
  `pg_advisory_lock`. Le verrou porte désormais sur la **base visée**, pas sur
  le dossier du dépôt. Deux conséquences : un run venu d'une autre machine est
  vu (indispensable pendant la transition poste → cloud), et un run local vers
  `manamind` ne bloque plus un run vers `relictrade`. Le code de sortie **2**
  est conservé. Plus de verrou périmé à nettoyer : PostgreSQL le libère quand la
  session tombe.
- **Normalisation de `DATABASE_URL`** : déplacée d'`update-prod.ps1` vers les
  **trois** points de lecture Python — `mtgdb.db.engine`, `alembic/env.py` et
  `scripts/import_game_changers.py`, ce dernier lisant `os.environ`
  directement. Sans cela, le premier run en conteneur échouait sur une URL
  `postgres://` fournie par Render.
- **Garde-fou « pas de base locale »** : déplacé d'`update-prod.ps1` vers
  `assert_remote_database()`, appelé par `update_all.py` avant la première
  écriture. Actif en conteneur, ou hors conteneur avec
  `MTGDB_REQUIRE_REMOTE_DB=1` (posé par `update-prod.ps1`).
  `MTGDB_ALLOW_LOCAL_DB=1` lève la restriction.
- **Journalisation** : en conteneur, plus de fichier dans `logs/` — écrire sur
  un disque éphémère revient à écrire dans le vide. Tout part sur stdout, que
  Render capture.
- **Dockerfile** : `ENTRYPOINT` remplacé par
  `CMD ["python", "scripts/update_all.py", "--skip", "tags"]`. Render ne sait
  remplacer que le `CMD` ; avec un `ENTRYPOINT`, `render.yaml` aurait porté un
  `dockerCommand: --skip tags` dont le sens dépend d'une ligne invisible.
  Ajout de `ENV MTGDB_CONTAINER=1`. `logs/` n'est plus créé dans l'image.
- **`docker-compose.yml`** : les variantes s'écrivent en commande complète
  (`docker compose run --rm updater python scripts/update_all.py --only tags`),
  conséquence du passage à `CMD`. Montage de `./logs` retiré.
- **`scripts/install_scheduled_tasks.ps1`** : n'installe plus que les deux
  tâches locales. `-Prod` renvoie désormais un message d'orientation. `-Remove`
  supprime toujours les anciennes tâches `(prod)`, pour le décommissionnement.
- **`update-prod.ps1`** : conservé comme **secours manuel** (incident Render,
  rattrapage immédiat), plus jamais planifié. Ses deux garde-fous vivent
  maintenant en Python ; il se contente de charger `.env.prod` et de poser
  `MTGDB_REQUIRE_REMOTE_DB=1`.
- **`pyproject.toml`** : groupe de dépendances `dev` (pytest) et configuration
  pytest. Le `uv sync --frozen --no-dev` du Dockerfile l'exclut de l'image.
- **`docs/deploiement.md`** : réécrit. Il décrivait un montage poste → Render
  qui n'existe plus.
- **`README.md`** : sections « Mise à jour automatique », « Tests » et
  « Structure du projet ».

### Notes

- **Aucune donnée de prix n'a été touchée.** `scryfall_card_prices` contient
  4 061 065 lignes du 07/06 au 30/08/2026 (738 Mo) : l'écriture est arrêtée
  depuis la mise en place de `SKIP_SCRYFALL_PRICES`, la table est **gelée**, pas
  vide. Cet historique n'est pas reconstituable.
- `uv sync --group dev` a retiré `pillow` du venv local : le paquet n'était
  déclaré nulle part et n'est importé par aucun fichier du dépôt.
- Le build de l'image n'a pas pu être vérifié localement (Docker absent de la
  machine) ; c'est Render qui le validera au premier déploiement.
