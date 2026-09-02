# Erste Schritte mit AI-PMO

> Quelle: Die japanische und die englische Fassung sind die Originale. Alle
> anderen Sprachen sind Übersetzungen.

---

## Worum geht es?

Ein Werkzeug, das Projektmanagement-Arbeit (PMO) an eine KI übergibt.

Es kann zum Beispiel:

- aus der Aufzeichnung einer Teams-Besprechung **automatisch ein Protokoll erstellen**
- daraus **wer was bis wann** herauslösen und als Aufgaben anlegen
- bei überfälligen Aufgaben **automatisch nachfassen**

Sie wählen eine „Vorlage" — den Bauplan der Arbeit — und es läuft.
Programmierkenntnisse sind nicht nötig.

---

## Für wen ist es gedacht?

- **Studierende** — die Formen des Projektmanagements im Gebrauch lernen
- **Kleine und mittlere Unternehmen** — die Arbeitsweisen ohne eigenes PMO bekommen
- **Große Organisationen** — was jede Abteilung anders macht, über Vorlagen angleichen

**Alles davon ist kostenlos** — keine eingeschränkte Fassung, keine
Testversion. Vorlagen und Prompts gelten zu denselben Bedingungen.
(Die Nutzung des KI-Dienstes zahlen Sie dem gewählten Anbieter.)

---

## Was Sie brauchen

| | Voraussetzungen | Kosten |
|---|---|---|
| **Einfache Einrichtung** | Ein Rechner und ein API-Schlüssel eines KI-Dienstes | KI-Nutzung (nach Verbrauch, gering) |
| **Interne Einrichtung** | Docker, 16 GB RAM oder mehr, möglichst eine GPU | Kostenlos (nur Strom) |

> **Welche wählen?**
> Zum Ausprobieren die **einfache Einrichtung**.
> Wenn Besprechungsinhalte das Haus nicht verlassen dürfen, die **interne
> Einrichtung**.

---

## In drei Schritten starten

### 1. Installieren

Folgen Sie [INSTALL.md](../../INSTALL.md).

- **Windows** — `AI-PMO-Setup.exe` doppelklicken
- **Mac / Linux** — im Terminal `./scripts/install.sh` ausführen
- **Docker** — `./scripts/install-docker.sh` ausführen

### 2. Einrichten

Nach der Installation öffnet sich der Einrichtungsdialog von selbst.
Beantworten Sie die Fragen; im Zweifel übernimmt Enter den Standardwert.

```
1) Wo soll die KI laufen?         → 1 (Cloud)
2) Wählen Sie einen KI-Anbieter   → 1 (OpenAI)
3) Geben Sie Ihren API-Schlüssel ein → einfügen
4) Name Ihrer Organisation        → Firmenname, kleingeschrieben
5) Datenbankanbindung aktivieren? → N
```

**Es stehen vier Anbieter zur Wahl.** Im Zweifel nehmen Sie OpenAI: dort sind
auch Embeddings vorhanden, eine Einstellung genügt.

| Anbieter | Eigenart |
|---|---|
| OpenAI | Die naheliegende Wahl |
| Gemini | Verarbeitet lange Protokolle günstig |
| Groq | Schnell, braucht aber zwei Schlüssel |
| OpenRouter | Ein Schlüssel, viele Modelle zum Vergleich |

**So bekommen Sie einen API-Schlüssel**
Legen Sie beim gewählten Anbieter ein Konto an und erzeugen Sie einen Schlüssel.
Es ist eine lange Zeichenfolge. Zeigen Sie sie niemandem.

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com

Näheres in [PROVIDERS.md](../PROVIDERS.md).

### 3. Ausprobieren

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

Erscheint dies, hat es geklappt:

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

`ステップ 5 件` bedeutet „5 Schritte“; die Ausgabe des Programms ist auf Japanisch.

---

## Was eine Vorlage ist

Ein Bauplan, der beschreibt, was in welcher Reihenfolge geschieht.
Eine Vorlage entspricht einer PMO-Aufgabe.

```yaml
name: meeting_minutes          # Name
trigger: "event:teams:meeting_ended"   # wann sie läuft (Besprechung beendet)

steps:                         # was sie tut
  - id: fetch_transcript       # 1. die Aufzeichnung holen
    adapter: teams

  - id: minutes                # 2. die KI das Protokoll schreiben lassen
    llm: { profile: default }

  - id: register_jira          # 3. die Aufgaben anlegen
    adapter: jira
```

Ändert sich die Arbeit, tauschen Sie die Vorlage.
**Auch die Art, wie die KI eingesetzt wird, ändert sich mit der Vorlage.**

---

## Häufige Befehle

```bash
aipmo setup       # Einrichtung erneut durchlaufen
aipmo validate <Datei>   # eine Vorlage auf Fehler prüfen
aipmo run <Datei>        # sie ausführen
aipmo adapters    # die angebundenen Werkzeuge auflisten
aipmo doctor      # prüfen, ob die Verbindungen stehen
aipmo serve       # die Oberfläche fürs Telefon öffnen
aipmo schedule    # zeitgesteuerte Ausführung starten
```

---

## Was Sie zur Sicherheit wissen sollten

**Ihr API-Schlüssel liegt in `.env`,** nicht in `config.yaml`.
Konfigurationsdateien werden mit Kolleginnen geteilt und landen in Git — der
Schlüssel wird deshalb getrennt gehalten.

**Interne Daten verlassen das System nicht.** Die Daten jeder Organisation
werden getrennt abgelegt; an die einer anderen zu gelangen ist technisch nicht
möglich.

**Nichts wird automatisch veröffentlicht.** Es gibt einen Weg, Erkenntnisse
öffentlich zu teilen, doch er verlangt immer die Zustimmung eines Menschen.
Kein Programm kann von sich aus veröffentlichen.

---

## Wenn etwas nicht klappt

**`aipmo` meldet „Befehl nicht gefunden"**
Führen Sie unter Mac oder Linux dies aus und öffnen Sie das Terminal neu:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**Unter Windows passiert beim Doppelklick auf die `.ps1` nichts**
Doppelklicken Sie stattdessen `install.bat`.

**Ich habe vergessen, den API-Schlüssel einzugeben**
Führen Sie `aipmo setup` noch einmal aus.

**Die Virenschutzsoftware blockiert das Installationsprogramm**
Unsignierte Dateien können eine Warnung auslösen. Falls Sie das stört,
verwenden Sie die Mac/Linux- oder die Docker-Fassung.

Mehr dazu in [INSTALL.md](../../INSTALL.md).

---

## Was Sie als Nächstes lesen sollten

- [INSTALL.md](../../INSTALL.md) — die Installation im Detail
- [MOBILE.md](../MOBILE.md) — Nutzung vom Telefon aus
- [PROVIDERS.md](../PROVIDERS.md) — die Wahl des KI-Anbieters
- [AGENTS.md](../AGENTS.md) — die KI selbst entscheiden lassen
- [TEAMS.md](../TEAMS.md) — Teams-Aufzeichnungen anbinden
- [JIRA-SLACK.md](../JIRA-SLACK.md) — Vorgänge in Jira anlegen, in Slack benachrichtigen
- [SCHEDULER.md](../SCHEDULER.md) — zeitgesteuert automatisch ausführen
- [AGILE.md](../AGILE.md) — über Sprints berichten
- [INDUSTRIES.md](../INDUSTRIES.md) — Bau, Marketing und andere Branchen
- [LICENSE](../../LICENSE) — MIT (kommerzielle Nutzung, Änderung und Weitergabe erlaubt)
- [README.md](../../README.md) — Funktionsweise und Aufbau, für Entwickler
- `templates/examples/` — Beispielvorlagen
