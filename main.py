"""
RSS-Podcast, Schritt 1: Artikel einsammeln und ein Skript schreiben lassen.

Ablauf:
  1. Einstellungen aus config.yaml und das Gedächtnis lesen (memory.py)
  2. Artikel seit der letzten Folge einsammeln (sources_rss.py)
  3. Kostenbremsen prüfen: Kosten pro Lauf, Läufe pro Tag, Monatsbudget (budget.py)
  4. Claude schreibt Titel, Skript und Shownotes (script_writer.py)
  5. Ergebnisse im Ordner out/ speichern
  6. Skript vorlesen lassen und als MP3 speichern (voice.py)
  7. Mit --veroeffentlichen: Folge und Feed im Ordner site/ ablegen (publish.py)

Mit "python main.py --nur-skript" wird ab Schritt 6 alles übersprungen, z. B. zum schnellen Testen.

"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import budget
import memory as mem
from script_writer import WORDS_PER_MINUTE, estimate_cost, write_episode
from sources_rss import collect_all
from publish import SITE, publish_episode
from voice import synthesize

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
TIMEZONE = ZoneInfo("Europe/Berlin")
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def num(value: int) -> str:
    """Zahl mit deutschem Tausenderpunkt, z. B. 12.345."""
    return f"{value:,}".replace(",", ".")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nur-skript", action="store_true", help="kein Audio erzeugen, nichts veröffentlichen")
    parser.add_argument("--veroeffentlichen", action="store_true", help="Folge in den Podcast-Feed stellen")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    collection, limits = config["collection"], config["limits"]

    now = datetime.now(timezone.utc)
    today = now.astimezone(TIMEZONE)
    OUT.mkdir(exist_ok=True)

    # --- 1. Gedächtnis lesen und Artikel einsammeln -----------------------------
    memory = mem.load(SITE)
    memory_days = config["memory"]["days"]
    since = mem.collection_start(memory, now, collection)
    recent = mem.recent_stories(memory, now, memory_days)
    since_local = since.astimezone(TIMEZONE)
    origin = "seit der letzten Folge" if memory.get("last_episode_utc") else "erster Lauf"
    print(f"Sammle Artikel seit {since_local:%d.%m. %H:%M} Uhr ({origin}). "
          f"Gedächtnis: {len(recent)} Themen der letzten {memory_days} Tage.", flush=True)
    articles, reports, dropped, already_used = collect_all(
        config["feeds"], since, collection, limits["max_input_chars"], skip_urls=mem.used_urls(memory))

    lines = [f"# Quellenbericht {today:%Y-%m-%d %H:%M}", "",
             f"Artikel seit {since_local:%d.%m.%Y %H:%M} Uhr ({origin}). "
             f"Gedächtnis: {len(recent)} Themen aus den letzten {memory_days} Tagen.", "", "## Feeds", ""]
    for r in reports:
        status = "OK" if r.ok else "FEHLER"
        lines.append(f"- **{r.name}** ({status}): {r.articles} Artikel, davon {r.full_texts} mit Volltext. {r.message}")
        print(f"  {r.name}: {status}, {r.articles} Artikel ({r.full_texts} Volltext). {r.message}", flush=True)
    if already_used:
        lines.append(f"\n{already_used} Artikel wurden schon in einer früheren Folge verwendet und übersprungen.")
        print(f"  {already_used} Artikel schon in einer früheren Folge verwendet, übersprungen.", flush=True)
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
    runs = budget.load(SITE)
    print(f"{len(articles)} Artikel, {num(input_chars)} Zeichen. Höchstens etwa {worst_case:.2f} $ für diesen Lauf. "
          f"Monat bisher: {budget.month_total(runs, today):.2f} $ von {limits['monthly_budget_usd']:.2f} $, "
          f"heute {budget.runs_today(runs, today)} von {limits['max_runs_per_day']} Läufen.", flush=True)
    try:
        budget.check(runs, today, worst_case, limits)
    except budget.BudgetExceeded as reason:
        sys.exit(f"Abbruch durch Kostenbremse: {reason}")

    # --- 3. Skript schreiben ----------------------------------------------------
    print(f"Claude ({config['model']['name']}) schreibt das Skript ...", flush=True)
    episode = write_episode(articles, today, config, recent)

    model = config["model"]
    cost = (episode.input_tokens * model["price_input_per_mtok_usd"]
            + episode.output_tokens * model["price_output_per_mtok_usd"]) / 1_000_000
    runs = budget.record(SITE, runs, today, cost, "nur Skript" if args.nur_skript else "Folge")
    words = len(episode.script.split())
    print(f"Fertig: „{episode.title}“, {num(words)} Wörter (etwa {words / WORDS_PER_MINUTE:.0f} Minuten). "
          f"Tatsächliche Kosten etwa {cost:.2f} $, Monat bisher {budget.month_total(runs, today):.2f} $.", flush=True)
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

    # --- 5. Vorlesen lassen -----------------------------------------------------
    if args.nur_skript:
        print("Audio übersprungen (--nur-skript).")
        return
    voice = config["voice"]
    print(f"Stimme {voice['name']} ({voice['engine']}) liest das Skript vor ...", flush=True)
    audio = synthesize(episode.script, OUT / "folge.mp3", voice, title=episode.title)
    minutes, seconds = divmod(round(audio.seconds), 60)
    print(f"Gespeichert: out/folge.mp3, {minutes}:{seconds:02d} Minuten, "
          f"{audio.size_bytes / 1_000_000:.1f} MB. Vorlesen dauerte {audio.render_seconds / 60:.1f} Minuten.")

    # --- 6. Veröffentlichen -----------------------------------------------------
    if args.veroeffentlichen:
        feed = publish_episode(OUT / "folge.mp3", episode.title, episode.shownotes,
                               audio.seconds, today, config["podcast"])
        mem.update(SITE, memory, now, today.date().isoformat(),
                   [a.url for a in articles if a.url], episode.shownotes, memory_days)
        print(f"Veröffentlicht. Feed: {feed}")
        print(f"Gedächtnis aktualisiert: {len(articles)} Artikel und die Themen dieser Folge gemerkt.")


if __name__ == "__main__":
    main()
