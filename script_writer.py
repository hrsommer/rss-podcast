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
- Depth: {depth_instruction} Depth never means adding facts: going deeper means explaining and connecting \
what the articles say, not adding outside knowledge. Cover the most significant stories first. \
Routine corporate news (appointments, small deals) gets at most one sentence or is skipped.
- Short news: some items carry little information, namely teaser-only articles (full_text="no") and the \
one- or two-sentence items in roundup or digest articles (lists of headlines with links to other outlets). \
Treat each of these as a short news item: one or two sentences, with clear attribution ("according to Reuters"). \
Never embellish them: no background, context, numbers or interpretation that is not in the text you received. \
Collect them in a "short news" block near the end, unless the same story is also covered in depth elsewhere.
- Recent episodes: the stories of the last days are listed in <recent_episodes>. Do not repeat them. \
Mention one again only if today's articles contain a genuine new development. Then present it briefly as an \
update ("On Tuesday we covered X. Now ...") and focus on what is new, without retelling the background.
- For each story: what happened and why it matters. Explain unfamiliar jargon in a few words the first time.
- Only use facts from the articles. Do not invent details, numbers or quotes. Attribute where helpful.
- Structure: short greeting with the date, a quick overview of the top stories (about 20 seconds), \
the main stories with clear spoken transitions, then the short news block, \
then a recap of the three most important takeaways and a short sign-off.
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


# Was der Tiefen-Regler (config.yaml, podcast.depth) für Claude bedeutet
DEPTH_INSTRUCTIONS = {
    1: "level 1 of 5, headlines only. Cover as many stories as possible, each in two to four sentences: "
       "what happened and one sentence on why it matters. No deep dives.",
    2: "level 2 of 5, headlines with short context. Give a broad overview: most stories get a short paragraph "
       "(what happened, why it matters). The two or three biggest stories may get a little more context.",
    3: "level 3 of 5, balanced. The three most important stories get real depth: background, implications "
       "and different angles. Everything else is covered briefly.",
    4: "level 4 of 5, in-depth. Focus on the five or six most important stories with background and implications. "
       "Everything else goes into the short news block or is skipped.",
    5: "level 5 of 5, analysis. Pick the two or three most important stories and analyze them thoroughly: "
       "background, how it works, implications, open questions and what to watch next. "
       "Everything else only as one-sentence items in the short news block.",
}


@dataclass
class Episode:
    title: str
    script: str
    shownotes: str          # Markdown mit echten Links
    input_tokens: int
    output_tokens: int
    truncated: bool         # True, wenn Claudes Antwort an der Obergrenze abgeschnitten wurde


def build_prompt(articles: list[Article], today: datetime, recent: list[dict] | None = None) -> str:
    blocks = []
    for a in articles:
        blocks.append(
            f'<article id="{a.id}" source="{a.source}" published="{a.published:%Y-%m-%d %H:%M} UTC" '
            f'full_text="{"yes" if a.full_text else "no"}">\n'
            f"<title>{a.title}</title>\n<content>\n{a.text}\n</content>\n</article>"
        )
    if recent:
        listed = "\n".join(
            f"- {datetime.fromisoformat(r['date']):%A, %B} {datetime.fromisoformat(r['date']).day}: {r['summary']}"
            for r in recent
        )
    else:
        listed = "(none yet)"
    return (
        f"Today is {today:%A, %B} {today.day}, {today.year}.\n\n"
        f"<recent_episodes>\n{listed}\n</recent_episodes>\n\n"
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
    """Ersetzt die Kennungen [A3] durch Links mit dem Artikeltitel als Linktext.

    Aus  "- Solid-state transformers [A22]"
    wird "- Solid-state transformers: [Why data centers are reviving ...](https://...) (Scientific American)"
    """
    by_id = {a.id: a for a in articles}
    lines = []
    for line in shownotes.splitlines():
        refs = re.findall(r"\[(A\d+)\]", line)
        text = re.sub(r"\s*\[A\d+\]", "", line).rstrip(" :")
        links = []
        for ref in refs:
            article = by_id.get(ref)
            if article and article.url:
                title = article.title.replace("[", "(").replace("]", ")")
                links.append(f"[{title}]({article.url}) ({article.source})")
        lines.append(f"{text}: " + ", ".join(links) if links else text)
    return "\n".join(lines)


def estimate_cost(input_chars: int, max_output_tokens: int, model_cfg: dict) -> float:
    """Obere Schätzung der Kosten in Dollar (etwa 4 Zeichen pro Token)."""
    input_tokens = input_chars / 4
    return (input_tokens * model_cfg["price_input_per_mtok_usd"]
            + max_output_tokens * model_cfg["price_output_per_mtok_usd"]) / 1_000_000


def write_episode(articles: list[Article], today: datetime, config: dict,
                  recent: list[dict] | None = None) -> Episode:
    max_minutes = config["podcast"]["max_minutes"]
    depth = min(5, max(1, int(config["podcast"].get("depth", 2))))
    system = SYSTEM_PROMPT.format(max_words=max_minutes * WORDS_PER_MINUTE, max_minutes=max_minutes,
                                  depth_instruction=DEPTH_INSTRUCTIONS[depth])
    prompt = build_prompt(articles, today, recent)

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
