# Autoresearch

[English](../../README.md) | **Deutsch**

Eine autonome, messbare Experimentierschleife für Codex.

Nenne Codex ein numerisches Ziel. Codex untersucht das Repository, bestätigt den Versuchsaufbau, ändert genau eine Sache, misst, behält Verbesserungen, macht Fehlschläge rückgängig und wiederholt dies bis zum Ziel.

Geeignet für fehlgeschlagene Tests, Coverage, Typfehler, Warnungen, Latenz, Binärgröße und reproduzierbare Sicherheitsbefunde.

## Schnellstart

In Codex installieren:

```text
$skill-installer install https://github.com/leo-lilinxiao/codex-autoresearch
```

Ein sauberes Git-Repository mit Full Access öffnen:

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

Dann aufrufen:

```text
$autoresearch error_count aus `python3 scripts/score.py` auf 0 senken
```

Vor dem ersten Schreibzugriff bestätigt Codex Ziel, Bereich, Ausgangswert, Zielwert, Messbefehl, optionalen Guard und die Nebenläufigkeit.

## Schleife

```text
Evidenz prüfen -> eine Hypothese ändern -> Commit und Messung
                                             |
                                besser + Guard erfolgreich: behalten
                                sonst: git revert
                                             |
                                      protokollieren, wiederholen
```

Codex verantwortet Hypothesen und Codeänderungen. Das Kontrollskript verantwortet Git-Grenzen, Messung, Rollback und Zustand.

## Parallele Kandidaten

| | |
|---|---|
| Isolierung | Ein langlebiger Git-Worktree pro Slot |
| Zuteilung | Adaptive Aufteilung zwischen Vertiefen des besten Ergebnisses und neuen Ideen |
| Rechenleistung | Eine deklarierte Bank aus Kernen und ganzen Maschinen; jeder Kandidat erhält eine Zuweisung |
| Aufnahme | Serialisiert; ein Kandidat mit veralteter Basis wird rebased und neu gemessen |
| Lebendigkeit | Leases, da die Steuerebene die Worker-Prozesse nicht besitzt |

Jeder Worker erhält dasselbe übergreifende Ziel und dieselben kuratierten Entscheidungen sowie sein eigenes individuelles Ziel. Ein Host ohne nebenläufige Subagenten belegt einen Slot nach dem anderen und fällt auf sequenzielle Ausführung mit identischem Zustandsmodell zurück.

## Ergebnisse

Nicht eingecheckte Dateien liegen unter `autoresearch-results/`:

| Pfad | Zweck |
|---|---|
| `run.json` | Bestätigte, unveränderliche Konfiguration |
| `events.jsonl` | Nur angehängte Zustands- und Audit-Historie |
| `logs/` | Vollständige Mess-, Guard- und Worker-Ausgaben |
| `slots.json` | Slot-Status, Leases und offene Rechenzuweisungen |
| `docs/` | Momentaufnahmen der kuratierten Dokumente |

`events.jsonl` ist die einzige Zustandsquelle. Fehlende, beschädigte oder widersprüchliche Daten führen zu einem klaren Fehler und werden nicht erraten oder rekonstruiert.

## Verlauf und Bericht

```text
$autoresearch show experiment history
$autoresearch export experiment history as TSV
$autoresearch generate an HTML report
```

Tabelle und HTML-Bericht werden aus validierten Ereignissen erzeugt. Der HTML-Schnappschuss liegt unter `autoresearch-results/report.html` und ist weder Laufzeitstatus noch Wiederherstellungsquelle.

## Garantien

- Neue Läufe benötigen einen sauberen, benannten Git-Branch.
- Ein Lauf verwaltet ein Repository, eine Metrik und einen Zielwert.
- Jedes Experiment wird committed; Fehlschläge werden mit `git revert` rückgängig gemacht.
- Änderungen außerhalb des Bereichs, Git-Drift, ungültige Metriken, Befehlsfehler, Timeouts und Rollback-Fehler stoppen mit Log-Pfad.
- `complete` wird nur gesetzt, wenn die behaltene Metrik den Zielwert erreicht.

## Voraussetzungen

- Aktuelle Codex CLI mit Skills und Goals
- Python 3.11+
- Git

Siehe [Installation](../INSTALL.md), [Benutzerhandbuch](../GUIDE.md) und [Beispiele](../EXAMPLES.md).

MIT-Lizenz. Inspiriert von [Karpathys autoresearch](https://github.com/karpathy/autoresearch).
