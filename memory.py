"""
Das Gedächtnis des Podcasts.

Es merkt sich drei Dinge und liegt als site/memory.json neben dem Feed auf GitHub Pages:
  1. Wann die letzte Folge erschienen ist, damit beim nächsten Mal alles seit dann eingesammelt wird
     (auch über das Wochenende oder nach einem ausgefallenen Tag).
  2. Welche Artikel schon verwendet wurden, damit kein Artikel zweimal eingeht.
  3. Welche Themen in den letzten Tagen vorkamen, damit Claude sie nicht wiederholt,
     sondern höchstens als Update mit echten Neuigkeiten bringt.

Das Gedächtnis wird nur aktualisiert, wenn eine Folge wirklich veröffentlicht wurde.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARTICLE_DAYS = 30  # so lange merken wir uns verwendete Artikel


def load(site: Path) -> dict:
    path = site / "memory.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"last_episode_utc": None, "articles": {}, "stories": []}


def collection_start(memory: dict, now: datetime, collection: dict) -> datetime:
    """Ab wann Artikel eingesammelt werden: seit der letzten Folge, aber nicht weiter zurück als erlaubt."""
    earliest = now - timedelta(hours=collection["max_lookback_hours"])
    if memory.get("last_episode_utc"):
        return max(datetime.fromisoformat(memory["last_episode_utc"]), earliest)
    return now - timedelta(hours=collection["lookback_hours"])  # allererster Lauf


def used_urls(memory: dict) -> set[str]:
    return set(memory.get("articles", {}))


def recent_stories(memory: dict, now: datetime, days: int) -> list[dict]:
    cutoff = (now - timedelta(days=days)).date().isoformat()
    return [s for s in memory.get("stories", []) if s["date"] >= cutoff]


def story_lines(shownotes_md: str) -> list[str]:
    """Holt aus den Shownotes die Themenzeilen, ohne Links: "- Thema: [Titel](url)" -> "Thema"."""
    lines = []
    for line in shownotes_md.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if line:
            lines.append(re.split(r":\s*\[", line, maxsplit=1)[0].strip())
    return lines


def update(site: Path, memory: dict, started: datetime, today: str,
           article_urls: list[str], shownotes_md: str, days: int) -> None:
    """Nach dem Veröffentlichen: Zeitpunkt, verwendete Artikel und Themen der Folge eintragen."""
    memory["last_episode_utc"] = started.astimezone(timezone.utc).isoformat()

    articles = memory.setdefault("articles", {})
    for url in article_urls:
        articles.setdefault(url, today)
    cutoff_articles = (started - timedelta(days=ARTICLE_DAYS)).date().isoformat()
    memory["articles"] = {u: d for u, d in articles.items() if d >= cutoff_articles}

    stories = memory.get("stories", []) + [{"date": today, "summary": line} for line in story_lines(shownotes_md)]
    cutoff_stories = (started - timedelta(days=days)).date().isoformat()
    memory["stories"] = [s for s in stories if s["date"] >= cutoff_stories]

    site.mkdir(parents=True, exist_ok=True)
    (site / "memory.json").write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
