"""
Die fertige Folge veröffentlichen: MP3 und Podcast-Feed im Ordner site/.

Der Ordner site/ wird vom GitHub-Job auf GitHub Pages hochgeladen (Branch gh-pages).
Vor jedem Lauf holt der Job die bisherigen Folgen von dort, damit der Feed weiterwächst.

Inhalt von site/:
  feed.xml        der Podcast-Feed, den Pocket Casts abonniert
  episodes.json   Liste der Folgen im Feed (damit wir wissen, was schon veröffentlicht ist)
  episodes/       die MP3-Dateien
  cover.jpg       das Coverbild
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"


def base_url(podcast: dict) -> str:
    """Webadresse des Podcasts. Wird aus dem Repository-Namen abgeleitet, falls nicht eingestellt."""
    if podcast.get("base_url"):
        return podcast["base_url"].rstrip("/")
    owner, repo = os.environ.get("GITHUB_REPOSITORY", "hrsommer/rss-podcast").split("/", 1)
    return f"https://{owner.lower()}.github.io/{repo}"


def attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def shownotes_to_html(markdown: str) -> str:
    """Wandelt die Shownotes (Markdown-Liste mit Links) in einfaches HTML für Podcast-Apps um."""
    items = []
    for line in markdown.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line:
            continue
        parts, last = [], 0
        for match in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", line):
            parts.append(html.escape(line[last:match.start()]))
            parts.append(f'<a href="{attr(match.group(2))}">{html.escape(match.group(1))}</a>')
            last = match.end()
        parts.append(html.escape(line[last:]))
        items.append("<li>" + "".join(parts) + "</li>")
    return "<ul>" + "".join(items) + "</ul>" if items else ""


def build_feed(podcast: dict, episodes: list[dict], url: str) -> str:
    items = []
    for ep in episodes:
        notes = ep["shownotes_html"].replace("]]>", "]]&gt;")
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description><![CDATA[{notes}]]></description>
      <enclosure url="{attr(url + '/' + ep['file'])}" length="{ep['bytes']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(ep['guid'])}</guid>
      <pubDate>{ep['pub_date']}</pubDate>
      <itunes:duration>{ep['seconds']}</itunes:duration>
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(podcast['title'])}</title>
    <link>{escape(url)}</link>
    <description>{escape(podcast['description'])}</description>
    <language>en</language>
    <itunes:author>{escape(podcast['author'])}</itunes:author>
    <itunes:image href="{attr(url + '/cover.jpg')}"/>
    <itunes:category text="Technology"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""


def publish_episode(mp3: Path, title: str, shownotes_md: str, seconds: float,
                    now_local: datetime, podcast: dict) -> str:
    """Legt die Folge in site/ ab, aktualisiert den Feed und gibt die Feed-Adresse zurück."""
    url = base_url(podcast)
    (SITE / "episodes").mkdir(parents=True, exist_ok=True)

    state_file = SITE / "episodes.json"
    episodes = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else []

    stamp = now_local.strftime("%Y-%m-%d-%H%M")
    day = now_local.strftime("%Y-%m-%d")
    file = f"episodes/{stamp}.mp3"
    shutil.copy(mp3, SITE / file)

    # Jeder Lauf ist eine eigene Folge. Dank Gedächtnis enthält ein zweiter Lauf am selben Tag
    # nur die Artikel, die seit dem ersten dazugekommen sind, ersetzt ihn also nicht.
    episodes.insert(0, {
        "day": day,
        "guid": f"episode-{stamp}",   # eindeutig pro Lauf, damit Pocket Casts neue Folgen sicher erkennt
        "title": f"{now_local:%a, %b} {now_local.day}: {title}",
        "file": file,
        "bytes": (SITE / file).stat().st_size,
        "seconds": round(seconds),
        "pub_date": format_datetime(now_local),
        "shownotes_html": shownotes_to_html(shownotes_md),
    })
    episodes.sort(key=lambda e: e["guid"], reverse=True)  # neueste zuerst
    episodes = episodes[: podcast.get("keep_episodes", 10)]

    # MP3-Dateien löschen, die nicht mehr im Feed stehen
    keep = {e["file"] for e in episodes}
    for path in (SITE / "episodes").glob("*.mp3"):
        if f"episodes/{path.name}" not in keep:
            path.unlink()

    shutil.copy(ROOT / "cover.jpg", SITE / "cover.jpg")
    state_file.write_text(json.dumps(episodes, indent=2, ensure_ascii=False), encoding="utf-8")
    (SITE / "feed.xml").write_text(build_feed(podcast, episodes, url), encoding="utf-8")
    (SITE / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")  # keine Suchmaschinen
    (SITE / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages soll die Dateien unverändert ausliefern
    return f"{url}/feed.xml"
