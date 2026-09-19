#!/usr/bin/env python3
"""
Mise à jour COMPLÈTE de la base de données MTG-DB.

Enchaîne, dans l'ordre, toutes les sources de données du projet :

  1. Scryfall         → cartes, éditions, prix, cardmarket_id, printed_name
  2. Cardmarket       → Product Catalog + Price Guide + rapport de liaison
  3. Game Changers    → flag game_changer sur les cartes
  4. Tagger tags      → ORACLE_CARD_TAG (cartes sans tags uniquement par défaut)

Conçu pour tourner aussi bien à la main qu'en tâche planifiée (Planificateur
Windows, cron, Cron Job Render) :

  - verrou anti-chevauchement porté par la base (pg_advisory_lock) : deux runs
    ne peuvent pas s'écraser mutuellement, même lancés depuis deux machines
  - garde-fou : en conteneur, une DATABASE_URL locale est refusée avant écriture
  - idempotence : un bulk Scryfall déjà importé n'est ni retéléchargé ni re-parsé
  - purge automatique des anciens fichiers bulk (393 Mo pièce)
  - journalisation : fichier avec rotation sur un poste, stdout en conteneur
  - codes de sortie exploitables par un superviseur

Codes de sortie :
  0  toutes les étapes demandées ont réussi
  1  au moins une étape a échoué
  2  un autre run est déjà en cours (verrou détenu)

Usage :
  python scripts/update_all.py                 # mise à jour complète
  python scripts/update_all.py --skip tags     # tout sauf les tags Tagger
  python scripts/update_all.py --only scryfall cardmarket
  python scripts/update_all.py --tags-all      # remplace TOUS les tags (long)
  python scripts/update_all.py --force         # réimporte le bulk Scryfall
  python scripts/update_all.py --stop-on-error # arrête à la première erreur
  python scripts/update_all.py --dry-run       # affiche le plan sans exécuter

Étapes disponibles pour --skip / --only :
  scryfall | cardmarket | game-changers | tags
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Sous Windows, la console est souvent en cp1252 : on force stdout/stderr en UTF-8
# pour afficher sans planter les encadrés Unicode et les accents.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LOG_DIR = ROOT / "logs"
PYTHON = sys.executable  # le même interpréteur (donc le venv actif)

sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import (  # noqa: E402 — après sys.path, comme les autres scripts
    DATABASE_URL,
    LocalDatabaseRefused,
    assert_remote_database,
)
from mtgdb.db.lock import AdvisoryLockHeld, advisory_lock  # noqa: E402
from mtgdb.db.urls import redact_database_url  # noqa: E402
from mtgdb.runtime import in_container  # noqa: E402

LOG_RETENTION = 30  # nombre de fichiers de log conservés

# Ordre canonique des étapes. Chaque entrée : (clé, libellé, script, args de base)
STEPS = [
    ("scryfall", "Scryfall (cartes, éditions, prix)", "import_scryfall.py", []),
    ("cardmarket", "Cardmarket (produits + prix + liaison)", "import_cardmarket_all.py", []),
    ("game-changers", "Game Changers (flag game_changer)", "import_game_changers.py", []),
    ("tags", "Tags Tagger (cartes sans tags)", "import_tagger_tags.py", []),
]

_C = {
    "reset": "\033[0m", "bold": "\033[1m", "green": "\033[32m",
    "red": "\033[31m", "yellow": "\033[33m", "cyan": "\033[36m", "grey": "\033[90m",
}


# ══════════════════════════════════════════════════════════════════════════════
# Sortie : console (couleurs) + fichier de log (texte brut)
# ══════════════════════════════════════════════════════════════════════════════

class Output:
    """Écrit sur la console et, si activé, dans un fichier de log sans codes couleur."""

    def __init__(self, log_path: Path | None):
        self.log_path = log_path
        self._fh = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = log_path.open("a", encoding="utf-8")

    def _colorize(self, text: str, color: str | None) -> str:
        if not color or not sys.stdout.isatty():
            return text
        return f"{_C.get(color, '')}{text}{_C['reset']}"

    def line(self, text: str = "", color: str | None = None) -> None:
        print(self._colorize(text, color))
        self.write_raw(text + "\n")

    def write_raw(self, text: str) -> None:
        """Écrit dans le fichier de log uniquement (déjà sans couleur)."""
        if self._fh:
            self._fh.write(text)
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class _JournalHandler(logging.Handler):
    """
    Route les messages des modules de la bibliothèque vers l'`Output` du run.

    Sans cela, `mtgdb.db.lock` et `mtgdb.db.retry` n'ont aucun handler : Python
    retombe sur `logging.lastResort`, qui écrit sur stderr, sans horodatage, et
    surtout HORS du fichier de journal — `Output` ne capte que la sortie des
    sous-processus, jamais celle du processus parent.

    Les trois messages ainsi perdus étaient les plus importants du système :
    perte de la connexion porteuse du verrou, reprise, et surtout
    « Verrou perdu ET repris par un autre run » — l'annonce que deux runs
    écrivent peut-être en parallèle.
    """

    def __init__(self, out: "Output"):
        super().__init__()
        self._out = out

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 — un handler ne doit jamais tuer le run
            return
        couleur = {"WARNING": "yellow", "ERROR": "red", "CRITICAL": "red"}.get(record.levelname)
        self._out.line(message, couleur)


def configurer_journalisation(out: Output) -> None:
    """Branche le logging de la bibliothèque sur le journal du run."""
    handler = _JournalHandler(out)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  [%(name)s] %(message)s", datefmt="%H:%M:%S"))
    racine = logging.getLogger()
    racine.setLevel(logging.INFO)
    # `force`-like : on repart d'une ardoise nette pour ne pas doubler l'affichage
    # si la fonction est appelée deux fois (tests).
    for ancien in list(racine.handlers):
        racine.removeHandler(ancien)
    racine.addHandler(handler)


def rotate_logs(log_dir: Path, keep: int = LOG_RETENTION) -> None:
    """Supprime les fichiers de log les plus anciens au-delà de `keep`."""
    if not log_dir.exists():
        return
    logs = sorted(log_dir.glob("update_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in logs[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Exécution des étapes
# ══════════════════════════════════════════════════════════════════════════════

def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def build_step_args(key: str, cli: argparse.Namespace) -> list[str]:
    """Construit les arguments spécifiques à une étape en fonction des options CLI."""
    extra: list[str] = []
    if key == "scryfall":
        if cli.force:
            extra.append("--force")
        if cli.no_purge:
            extra.append("--no-purge")
        if cli.keep_bulks != 1:
            extra += ["--keep-bulks", str(cli.keep_bulks)]
    if key == "cardmarket" and cli.keep_captures:
        extra += ["--keep-captures", str(cli.keep_captures)]
    if key == "tags" and cli.tags_all:
        extra.append("--all")
    return extra


def run_step(key: str, label: str, script: str, base_args: list[str],
             cli: argparse.Namespace, index: int, total: int, out: Output) -> dict:
    args = base_args + build_step_args(key, cli)
    cmd = [PYTHON, str(SCRIPTS / script), *args]

    out.line()
    out.line("═" * 78, "cyan")
    out.line(f"[{index}/{total}] {label}", "bold")
    out.line(f"      → {' '.join(cmd)}", "grey")
    out.line("═" * 78, "cyan")

    if cli.dry_run:
        out.line("      (dry-run : non exécuté)", "yellow")
        return {"key": key, "label": label, "status": "skipped", "duration": 0.0}

    start = time.monotonic()
    # PYTHONIOENCODING=utf-8 : indispensable sous Windows pour l'affichage tqdm/accents.
    # On hérite de l'environnement courant (sinon DATABASE_URL & co disparaîtraient).
    #
    # DATABASE_URL est réinjectée sous sa forme NORMALISÉE : les sous-scripts la
    # relisent chacun de leur côté, et tous n'ont pas la même porte d'entrée
    # (import_game_changers.py lit os.environ directement). La normaliser une
    # fois ici garantit que les quatre étapes visent la même URL, écrite pareil.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if DATABASE_URL:
        env["DATABASE_URL"] = DATABASE_URL

    returncode = _stream_subprocess(cmd, env, out)
    duration = time.monotonic() - start

    ok = returncode == 0
    if ok:
        out.line(f"  ✓ OK — {label} — {fmt_duration(duration)}", "green")
    else:
        out.line(f"  ✗ ÉCHEC (code {returncode}) — {label} — {fmt_duration(duration)}", "red")

    return {
        "key": key, "label": label, "status": "ok" if ok else "failed",
        "duration": duration, "returncode": returncode,
    }


def _stream_subprocess(cmd: list[str], env: dict, out: Output) -> int:
    """
    Lance le sous-processus en relayant sa sortie vers la console ET le fichier de log.

    Les barres de progression tqdm se réécrivent avec des retours chariot (\\r) : on
    les laisse telles quelles sur la console, mais le fichier de log ne conserve que
    l'état final de chaque ligne — sinon un run de 40 min produirait un log de
    plusieurs dizaines de milliers de lignes de barres intermédiaires.
    """
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert proc.stdout is not None

    pending = ""
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")

        # Console : fidélité totale (les barres tqdm s'animent normalement)
        sys.stdout.write(text)
        sys.stdout.flush()

        # Fichier : on ne retient que les lignes complètes, réduites à leur état final
        pending += text
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            out.write_raw(_clean_line(line) + "\n")

    proc.wait()
    if pending.strip():
        out.write_raw(_clean_line(pending) + "\n")
    return proc.returncode


def _clean_line(line: str) -> str:
    """
    Réduit une ligne brute à ce qui mérite d'être journalisé.

    Deux sources de retours chariot se superposent : le \\r de CRLF sous Windows
    (à retirer) et ceux dont tqdm se sert pour réécrire sa barre (on ne garde que
    le dernier état). Les traiter dans cet ordre est indispensable, sans quoi toute
    ligne CRLF se réduirait à une chaîne vide.
    """
    return line.rstrip("\r").rsplit("\r", 1)[-1]


@contextmanager
def _lock_context(cli: argparse.Namespace, out: Output) -> Iterator[None]:
    """
    Enveloppe le run dans le verrou advisory, ou ne fait rien s'il est désactivé.

    Le verrou n'a de sens que s'il y a quelque chose à protéger : `--dry-run`
    n'écrit rien, et `--no-lock` est une échappatoire assumée.
    """
    if cli.no_lock or cli.dry_run:
        if cli.no_lock and not cli.dry_run:
            out.line("  ⚠ --no-lock : rien n'empêche un second run d'écrire en même temps.",
                     "yellow")
        yield
        return

    if not DATABASE_URL:
        out.line("\n  ✗ DATABASE_URL absent : impossible de poser le verrou anti-chevauchement.",
                 "red")
        out.close()
        sys.exit(1)

    with advisory_lock(DATABASE_URL):
        yield


def _run_steps(steps: list[tuple], cli: argparse.Namespace, out: Output) -> list[dict]:
    """Exécute les étapes dans l'ordre et retourne leurs résultats."""
    results: list[dict] = []
    total = len(steps)
    for i, (key, label, script, base_args) in enumerate(steps, start=1):
        res = run_step(key, label, script, base_args, cli, i, total, out)
        results.append(res)
        if res["status"] == "failed" and cli.stop_on_error:
            out.line("\n  --stop-on-error : arrêt après l'échec de cette étape.", "red")
            break
    return results


def select_steps(cli: argparse.Namespace) -> list[tuple]:
    keys = {s[0] for s in STEPS}
    if cli.only:
        invalid = set(cli.only) - keys
        if invalid:
            sys.exit(f"Étapes inconnues pour --only : {', '.join(sorted(invalid))}\n"
                     f"Valides : {', '.join(k for k, *_ in STEPS)}")
        return [s for s in STEPS if s[0] in set(cli.only)]
    skip = set(cli.skip or [])
    invalid = skip - keys
    if invalid:
        sys.exit(f"Étapes inconnues pour --skip : {', '.join(sorted(invalid))}\n"
                 f"Valides : {', '.join(k for k, *_ in STEPS)}")
    return [s for s in STEPS if s[0] not in skip]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mise à jour complète de la base MTG-DB (Scryfall + Cardmarket + tags).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Étapes : scryfall | cardmarket | game-changers | tags",
    )
    parser.add_argument("--skip", nargs="+", metavar="ÉTAPE", help="Étape(s) à ignorer")
    parser.add_argument("--only", nargs="+", metavar="ÉTAPE",
                        help="Ne lancer QUE cette/ces étape(s)")
    parser.add_argument("--force", action="store_true",
                        help="Réimporte le bulk Scryfall même s'il l'a déjà été")
    parser.add_argument("--tags-all", action="store_true",
                        help="Retraite TOUTES les cartes pour les tags (long, remplace l'existant)")
    parser.add_argument("--keep-bulks", type=int, default=1, metavar="N",
                        help="Nombre de fichiers bulk Scryfall à conserver (défaut : 1)")
    parser.add_argument("--keep-captures", type=int, default=0, metavar="N",
                        help="Ne conserver que les N dernières captures de prix Cardmarket "
                             "(0 = tout garder, défaut). Une capture pèse ~62 Mo : en "
                             "quotidien, sans purge, +23 Go/an. SUPPRESSION DÉFINITIVE.")
    parser.add_argument("--no-purge", action="store_true",
                        help="Ne supprime pas les anciens fichiers bulk")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="Arrête dès qu'une étape échoue")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le plan d'exécution sans rien lancer")
    parser.add_argument("--no-lock", action="store_true",
                        help="Ignore le verrou advisory anti-chevauchement (à éviter)")
    parser.add_argument("--no-log-file", action="store_true",
                        help="N'écrit pas de fichier de log")
    cli = parser.parse_args()

    # En conteneur, pas de fichier de log : le disque est éphémère et meurt avec
    # le conteneur. Render capture stdout, seule trace qui subsiste après le run.
    containerized = in_container()
    log_path = None
    if not cli.no_log_file and not cli.dry_run and not containerized:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"update_{stamp}.log"

    out = Output(log_path)
    configurer_journalisation(out)
    steps = select_steps(cli)
    total = len(steps)

    out.line("╔" + "═" * 76 + "╗", "cyan")
    out.line("║" + " MISE À JOUR COMPLÈTE — MTG-DB ".center(76) + "║", "bold")
    out.line("╚" + "═" * 76 + "╝", "cyan")
    out.line(f"  Démarré : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out.line(f"  {total} étape(s) : {', '.join(k for k, *_ in steps)}")
    out.line(f"  Base    : {redact_database_url(DATABASE_URL)}", "grey")
    if containerized:
        out.line("  Contexte: conteneur — journal sur stdout, pas de fichier.", "grey")
    if log_path:
        out.line(f"  Journal : {log_path}", "grey")

    # Garde-fou hérité d'update-prod.ps1 : refuser une base locale en conteneur,
    # AVANT la moindre écriture. Sans lui, un run mal configuré met à jour la
    # base de développement et rend un rapport final tout vert.
    if not cli.dry_run:
        try:
            assert_remote_database()
        except (LocalDatabaseRefused, RuntimeError) as exc:
            out.line(f"\n  ✗ {exc}", "red")
            out.close()
            sys.exit(1)

    results: list[dict] = []
    global_start = time.monotonic()

    try:
        with _lock_context(cli, out):
            results = _run_steps(steps, cli, out)
    except AdvisoryLockHeld as exc:
        out.line(f"\n  ⚠ {exc}", "yellow")
        out.line("  Rien à faire — sortie sans erreur applicative (code 2).", "yellow")
        out.close()
        sys.exit(2)

    total_duration = time.monotonic() - global_start

    # ── Rapport final ────────────────────────────────────────────────────────
    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_failed = sum(1 for r in results if r["status"] == "failed")
    n_skipped = sum(1 for r in results if r["status"] == "skipped")

    out.line()
    out.line("─" * 78, "cyan")
    out.line("  RAPPORT FINAL", "bold")
    out.line("─" * 78, "cyan")
    for r in results:
        icon = {"ok": "✓", "failed": "✗", "skipped": "∘"}[r["status"]]
        color = {"ok": "green", "failed": "red", "skipped": "yellow"}[r["status"]]
        out.line(f"    {icon}  {r['label']:<45} {fmt_duration(r['duration'])}", color)
    out.line("─" * 78, "cyan")
    out.line(
        f"  {n_ok} ok · {n_failed} échec(s) · {n_skipped} ignorée(s) "
        f"— total {fmt_duration(total_duration)}",
        "bold",
    )

    if log_path:
        rotate_logs(LOG_DIR)
    out.close()

    sys.exit(1 if n_failed else 0)


if __name__ == "__main__":
    main()
