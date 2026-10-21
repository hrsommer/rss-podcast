"""
RSS-Podcast, Schritt 1: Artikel einsammeln und ein Skript schreiben lassen.

Ablauf:
  1. Einstellungen aus config.yaml lesen
  2. Artikel aus allen Feeds einsammeln (sources_rss.py)
  3. Kosten vorab schätzen und abbrechen, falls sie über dem Limit liegen
  4. Claude schreibt Titel, Skript und Shownotes (script_writer.py)
  5. Ergebnisse im Ordner out/ speichern

Noch nicht enthalten (kommt in den nächsten Schritten): Vertonung, Feed,
Gedächtnis gegen Wiederholungen, Tiefen-Regler, Zeitplan.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from script_writer import WORDS_PER_MINUTE, estimate_cost, write_episode
from sources_rss import collect_all

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
TIMEZONE = ZoneInfo("Europe/Berlin")
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def num(value: int) -> str:
    """Zahl mit deutschem Tausenderpunkt, z. B. 12.345."""
    return f"{value:,}".replace(",", ".")


def main() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    collection, limits = config["collection"], config["limits"]

    now = datetime.now(timezone.utc)
    today = now.astimezone(TIMEZONE)
    since = now - timedelta(hours=collection["lookback_hours"])
    OUT.mkdir(exist_ok=True)

    # --- 1. Artikel einsammeln --------------------------------------------------
    print(f"Sammle Artikel seit {since:%Y-%m-%d %H:%M} UTC ...", flush=True)
    articles, reports, dropped = collect_all(config["feeds"], since, collection, limits["max_input_chars"])

    lines = [f"# Quellenbericht {today:%Y-%m-%d %H:%M}", "", "## Feeds", ""]
    for r in reports:
        status = "OK" if r.ok else "FEHLER"
        lines.append(f"- **{r.name}** ({status}): {r.articles} Artikel, davon {r.full_texts} mit Volltext. {r.message}")
        print(f"  {r.name}: {status}, {r.articles} Artikel ({r.full_texts} Volltext). {r.message}", flush=True)
    if dropped:
        lines.append(f"\n{dropped} ältere Artikel wurden wegen der Textobergrenze weggelassen.")
    lines += ["", "## Artikel", ""]
    for a in articles:
        kind = "Volltext" if a.full_text else "nur Anriss" + (f" (Grund: {a.note})" if a.note else "")
        lines.append(f"- {a.id} | {a.source} | {a.published:%d.%m. %H:%M} | {kind}, {num(len(a.text))} Zeichen | [{a.title}]({a.url})")
    (OUT / "quellenbericht.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not articles:
        print("Keine neuen Artikel gefunden. Heute keine Folge.")
        return

    # --- 2. Kostenbremse vor dem Aufruf -----------------------------------------
    input_chars = sum(len(a.text) for a in articles)
    worst_case = estimate_cost(input_chars, limits["max_output_tokens"], config["model"])
    print(f"{len(articles)} Artikel, {num(input_chars)} Zeichen. Höchstens etwa {worst_case:.2f} $ für diesen Lauf.", flush=True)
    if worst_case > limits["max_cost_per_run_usd"]:
        sys.exit(f"Abbruch: geschätzte Kosten {worst_case:.2f} $ liegen über dem Limit "
                 f"von {limits['max_cost_per_run_usd']:.2f} $ (config.yaml, limits).")

    # --- 3. Skript schreiben ----------------------------------------------------
    print(f"Claude ({config['model']['name']}) schreibt das Skript ...", flush=True)
    episode = write_episode(articles, today, config)

    model = config["model"]
    cost = (episode.input_tokens * model["price_input_per_mtok_usd"]
            + episode.output_tokens * model["price_output_per_mtok_usd"]) / 1_000_000
    words = len(episode.script.split())
    print(f"Fertig: „{episode.title}“, {num(words)} Wörter (etwa {words / WORDS_PER_MINUTE:.0f} Minuten). "
          f"Tatsächliche Kosten etwa {cost:.2f} $.", flush=True)
    if episode.truncated:
        print("Warnung: Claudes Antwort wurde an der Obergrenze abgeschnitten (limits.max_output_tokens).")

    # --- 4. Ergebnis speichern --------------------------------------------------
    result = (
        f"# {episode.title}\n\n"
        f"{WEEKDAYS[today.weekday()]}, {today:%d.%m.%Y} · {num(words)} Wörter · etwa {words / WORDS_PER_MINUTE:.0f} Minuten · "
        f"Kosten etwa {cost:.2f} $\n\n"
        f"## Skript\n\n{episode.script}\n\n"
        f"## Shownotes\n\n{episode.shownotes}\n"
    )
    (OUT / "skript.md").write_text(result, encoding="utf-8")
    print("Gespeichert: out/skript.md und out/quellenbericht.md")


if __name__ == "__main__":
    main()
