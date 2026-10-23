"""
Artikel aus RSS-Feeds einsammeln.

Für jeden Feed in config.yaml:
  1. Feed abrufen und alle Einträge der letzten Stunden heraussuchen
  2. Text des Eintrags nehmen. Ist er nur ein kurzer Anrisstext, den Artikel von der Webseite holen
  3. Alles als einheitliche "Article"-Objekte zurückgeben, plus einen Bericht pro Feed

Weitere Quellenarten (z. B. E-Mail-Newsletter) können später als eigenes Modul dazukommen,
solange sie ebenfalls Article-Objekte liefern.
"""

from __future__ import annotations

import calendar
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup

# Wir geben uns ehrlich als dieses Projekt zu erkennen, nicht als Browser.
USER_AGENT = "rss-podcast/0.1 (+https://github.com/hrsommer/rss-podcast)"
TIMEOUT_SECONDS = 20


@dataclass
class Article:
    source: str          # Name des Feeds, z. B. "IEEE Spectrum"
    title: str
    url: str
    published: datetime
    text: str
    full_text: bool      # True = ganzer Artikel, False = nur Anrisstext
    id: str = ""         # Kurze Kennung wie "A7", wird später vergeben
    note: str = ""       # Warum das Nachladen des Volltexts gescheitert ist (für den Quellenbericht)


@dataclass
class FeedReport:
    name: str
    url: str
    ok: bool
    message: str = ""
    articles: int = 0
    full_texts: int = 0
    failures: Counter = field(default_factory=Counter)  # Gründe für gescheitertes Nachladen


def html_to_text(html: str) -> str:
    """Macht aus HTML lesbaren Text und räumt Leerzeichen auf."""
    text = BeautifulSoup(html or "", "html.parser").get_text(" ")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def entry_date(entry) -> datetime | None:
    """Veröffentlichungsdatum eines Feed-Eintrags (feedparser liefert es bereits in UTC)."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def entry_text(entry) -> str:
    """Bevorzugt den vollen Inhalt (content), sonst die Zusammenfassung (summary)."""
    if entry.get("content"):
        return html_to_text(entry.content[0].get("value", ""))
    return html_to_text(entry.get("summary", ""))


def fetch_full_article(url: str) -> tuple[str | None, str]:
    """Holt die Webseite und schneidet mit trafilatura den eigentlichen Artikeltext heraus.

    Gibt den Text zurück und, falls es nicht geklappt hat, eine kurze Begründung.
    """
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    except requests.Timeout:
        return None, "Zeitüberschreitung beim Abruf"
    except requests.RequestException as error:
        return None, f"nicht erreichbar ({type(error).__name__})"

    if response.status_code != 200:
        return None, f"Server antwortet mit Status {response.status_code}"
    redirect = ""
    final_url = getattr(response, "url", url) or url
    if final_url.rstrip("/") != url.rstrip("/"):
        redirect = f", weitergeleitet auf {final_url}"
    content_type = getattr(response, "headers", {}).get("Content-Type", "text/html")
    if "html" not in content_type:
        return None, f"keine Webseite ({content_type}){redirect}"

    text = trafilatura.extract(response.text, include_comments=False, include_tables=False)
    if not text:
        return None, f"kein Artikeltext in der Seite gefunden ({len(response.text):,} Zeichen HTML)".replace(",", ".") + f"{redirect}"
    return text.strip(), ""


def collect_feed(feed: dict, since: datetime, settings: dict) -> tuple[list[Article], FeedReport]:
    name, url = feed["name"], feed["url"]
    report = FeedReport(name=name, url=url, ok=False)

    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as error:
        report.message = f"nicht erreichbar ({type(error).__name__})"
        return [], report
    if response.status_code != 200:
        report.message = f"Server antwortet mit Status {response.status_code}"
        return [], report

    parsed = feedparser.parse(response.content)
    if not parsed.entries:
        report.message = "kein gültiger Feed oder keine Einträge"
        return [], report

    patterns = [p.lower() for p in settings.get("exclude_title_patterns") or []]
    fetch_enabled = feed.get("fetch_full_text", settings["fetch_full_text"])  # Feed-Einstellung hat Vorrang
    min_chars = settings.get("min_chars", 0)
    articles: list[Article] = []
    undated = excluded = too_short = 0
    for entry in parsed.entries[: settings["max_articles_per_feed"]]:
        published = entry_date(entry)
        if published is None:
            undated += 1
            continue
        if published <= since:
            continue

        # Titelfilter: z. B. Rätsel oder Gedichte gar nicht erst einsammeln
        title = html_to_text(entry.get("title", "(ohne Titel)"))
        if any(pattern in title.lower() for pattern in patterns):
            excluded += 1
            continue

        text = entry_text(entry)
        link = entry.get("link", "")
        full = len(text) >= settings["teaser_threshold_chars"]
        note = ""
        if not full and not fetch_enabled:
            note = "Nachladen für diesen Feed abgeschaltet"
        elif not full and link:
            article_text, note = fetch_full_article(link)
            if article_text:
                # Die Webseite hat den Artikel geliefert. Ist er nicht länger als der Feed-Text,
                # stand schon im Feed der ganze (kurze) Artikel. Beides gilt als Volltext.
                if len(article_text) > len(text):
                    text = article_text
                full = True
            if note:
                report.failures[note.split(" (")[0].split(", weiter")[0]] += 1

        # Sicherheitsnetz: Einträge ohne nennenswerten Inhalt (z. B. nur eine Überschrift)
        if len(text) < min_chars:
            too_short += 1
            continue

        articles.append(Article(
            source=name,
            title=title,
            url=link,
            published=published,
            text=text[: settings["max_chars_per_article"]],
            full_text=full,
            note=note,
        ))

    report.ok = True
    report.articles = len(articles)
    report.full_texts = sum(a.full_text for a in articles)
    notes = []
    if excluded:
        notes.append(f"{excluded} per Titelfilter übersprungen")
    if too_short:
        notes.append(f"{too_short} mit zu wenig Text übersprungen")
    if not fetch_enabled:
        notes.append("Nachladen abgeschaltet")
    if undated:
        notes.append(f"{undated} ohne Datum übersprungen")
    for reason, count in report.failures.items():
        notes.append(f"Volltext {count}× gescheitert: {reason}")
    report.message = "ok" + (", " + ", ".join(notes) if notes else "")
    return articles, report


def collect_all(feeds: list[dict], since: datetime, settings: dict, max_total_chars: int
                ) -> tuple[list[Article], list[FeedReport], int]:
    """Sammelt alle Feeds ein. Gibt Artikel, Feed-Berichte und die Zahl verworfener Artikel zurück."""
    all_articles: list[Article] = []
    reports: list[FeedReport] = []
    for feed in feeds:
        articles, report = collect_feed(feed, since, settings)
        all_articles.extend(articles)
        reports.append(report)

    # Derselbe Artikel kann in mehreren Feeds stehen: nur einmal behalten
    unique: dict[str, Article] = {}
    for article in all_articles:
        unique.setdefault(article.url or f"{article.source}:{article.title}", article)
    articles = sorted(unique.values(), key=lambda a: a.published, reverse=True)

    # Harte Obergrenze für die Textmenge: neueste Artikel zuerst, der Rest fällt weg
    kept, total = [], 0
    for article in articles:
        if total + len(article.text) > max_total_chars:
            break
        kept.append(article)
        total += len(article.text)
    dropped = len(articles) - len(kept)

    for number, article in enumerate(kept, 1):
        article.id = f"A{number}"
    return kept, reports, dropped
