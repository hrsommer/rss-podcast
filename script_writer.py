"""
Aus den gesammelten Artikeln mit Claude ein Podcast-Skript schreiben.

Claude bekommt alle Artikel mit einer Kennung (A1, A2, ...) und antwortet mit
Titel, Skript und Shownotes. In den Shownotes verweist Claude auf die Kennungen,
und wir ersetzen sie anschließend durch die echten Links.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import anthropic

from sources_rss import Article

WORDS_PER_MINUTE = 150  # übliches Sprechtempo von Nachrichtensprechern

SYSTEM_PROMPT = """You write and host a private daily tech and science news podcast for exactly one \
listener, who listens in the car on the way to work. The listener cannot look at anything, \
so the script must work purely as audio.

You receive the latest articles from the listener's chosen RSS feeds. Each article has an id like A7. \
Some articles are full texts, others only short teasers (marked full_text="no").

How to write the script:
- English, one host, news briefing style. Conversational but precise, like a good morning radio show.
- Organize by STORY, not by source. If several articles cover the same news, merge them into one segment \
and mention different angles briefly.
- Give a broad overview. Cover the most significant stories first and with a bit more context, smaller items briefly. \
Routine corporate news (appointments, small deals) gets at most one sentence or is skipped.
- For teaser-only articles, stay brief and do not guess beyond what the teaser says.
- For each story: what happened and why it matters. Explain unfamiliar jargon in a few words the first time.
- Only use facts from the articles. Do not invent details, numbers or quotes. Attribute where helpful.
- Structure: short greeting with the date, a quick overview of the top stories (about 20 seconds), \
the stories with clear spoken transitions, then a recap of the three most important takeaways and a short sign-off.
- Written for the ear: short sentences, no lists, no headings, no markdown, no emojis, no URLs, no article ids, \
no stage directions. Write symbols as words (percent, dollars). Avoid long strings of numbers.
- Length: as long as the news deserves, but at most {max_words} words (about {max_minutes} minutes). \
Never pad. A quiet news day can be a short episode.

Answer in exactly this format and nothing else:
<title>A short, specific episode title (max 8 words)</title>
<script>
The full spoken script, paragraphs separated by blank lines.
</script>
<shownotes>
- One line per story, in the order of the episode, followed by the ids of its articles, e.g. [A3] [A7]
</shownotes>"""


@dataclass
class Episode:
    title: str
    script: str
    shownotes: str          # Markdown mit echten Links
    input_tokens: int
    output_tokens: int
    truncated: bool         # True, wenn Claudes Antwort an der Obergrenze abgeschnitten wurde


def build_prompt(articles: list[Article], today: datetime) -> str:
    blocks = []
    for a in articles:
        blocks.append(
            f'<article id="{a.id}" source="{a.source}" published="{a.published:%Y-%m-%d %H:%M} UTC" '
            f'full_text="{"yes" if a.full_text else "no"}">\n'
            f"<title>{a.title}</title>\n<content>\n{a.text}\n</content>\n</article>"
        )
    return (
        f"Today is {today:%A, %B} {today.day}, {today.year}.\n\n"
        "<articles>\n" + "\n\n".join(blocks) + "\n</articles>\n\n"
        "Write today's episode now, in the required format."
    )


def extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def clean_for_speech(script: str) -> str:
    """Entfernt alles, was beim Vorlesen stören würde, falls Claude es doch hineinschreibt."""
    s = re.sub(r"\s*\[A\d+\]", "", script)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"^[#>\-\*• \t]+", "", s, flags=re.MULTILINE)
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def link_shownotes(shownotes: str, articles: list[Article]) -> str:
    """Ersetzt [A3] durch einen Markdown-Link auf den Originalartikel."""
    by_id = {a.id: a for a in articles}

    def replace(match: re.Match) -> str:
        article = by_id.get(match.group(1))
        return f"[{article.source}]({article.url})" if article and article.url else ""

    return re.sub(r"\[(A\d+)\]", replace, shownotes)


def estimate_cost(input_chars: int, max_output_tokens: int, model_cfg: dict) -> float:
    """Obere Schätzung der Kosten in Dollar (etwa 4 Zeichen pro Token)."""
    input_tokens = input_chars / 4
    return (input_tokens * model_cfg["price_input_per_mtok_usd"]
            + max_output_tokens * model_cfg["price_output_per_mtok_usd"]) / 1_000_000


def write_episode(articles: list[Article], today: datetime, config: dict) -> Episode:
    max_minutes = config["podcast"]["max_minutes"]
    system = SYSTEM_PROMPT.format(max_words=max_minutes * WORDS_PER_MINUTE, max_minutes=max_minutes)
    prompt = build_prompt(articles, today)

    client = anthropic.Anthropic(max_retries=3)
    with client.messages.stream(
        model=config["model"]["name"],
        max_tokens=config["limits"]["max_output_tokens"],
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    text = "".join(block.text for block in message.content if block.type == "text")
    script = clean_for_speech(extract_tag(text, "script"))
    if not script:
        raise RuntimeError("Claudes Antwort enthielt kein Skript. Antwort:\n" + text[:2000])

    return Episode(
        title=extract_tag(text, "title") or "Daily Briefing",
        script=script,
        shownotes=link_shownotes(extract_tag(text, "shownotes"), articles),
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        truncated=message.stop_reason == "max_tokens",
    )
