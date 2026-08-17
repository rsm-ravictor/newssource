# AAT Intel Briefing — Email Template Description

## What it is
An HTML email template used in the "Competitor Monitoring Agent" n8n workflow
(`Build Digest HTML` node), sent daily via Gmail to summarize competitor news
events. It is generated as a single HTML string built entirely inside a
JavaScript Code node — there is no separate template file; the HTML is
constructed with string concatenation at runtime, using live workflow data.

## Layout, top to bottom

**1. Banner**
Full-width dark header bar. Left: "AAT Intel Briefing" title. Right: current
date (e.g. "July 30, 2026").

**2. Two-column section**
- **Left column (~58% width):**
  - "Top Intel" box — a bordered card listing the top 5 events, sorted by
    severity (Urgent → Watch → Informational). Each row shows a colored
    severity badge, the event category as a clickable headline linking to
    the source, and the company name below it.
  - "Who's in the News — Jump to Full Briefing" — a row of pill-shaped
    buttons, one per company that has news today. This acts as an index:
    each button is an anchor link (`#company-slug`) that jumps down to
    that company's detail section further down the email. This label has
    `id="aat-index"` so detail sections can link back up to it.

- **Right column (~38% width):**
  - "Trending Now" — a purple box containing a vertical marquee/ticker.
    Company names are stacked and scroll upward continuously (CSS
    `translateY` animation, 12s loop). Each name is a fixed 33px-tall
    block; the visible window is 132px tall (exactly 4 names visible at
    once). The list of names is duplicated once in the HTML so the loop
    is seamless. Below the ticker, small text shows the total company
    count for the day.
  - Note: the animation only plays in email clients that respect CSS
    keyframes (e.g. Apple Mail). Gmail and Outlook strip animations, so
    the ticker just displays as a static stacked list there — still
    fully readable, just not moving.

**3. Divider**
A horizontal rule separating the 2-column summary area from the full
detail below.

**4. Full Briefing (single column, full width)**
One card per company that had news that day, in the same order as the
index buttons above. Each card contains:
- Company name (left) and a "↑ Back to index" link (right) that jumps
  back up to the `#aat-index` anchor.
- One block per event for that company: a severity badge, the event
  category, a plain-language summary, and a "Read source →" link to the
  original article (if a URL exists). A thin divider line separates
  multiple events for the same company.

**5. Footer**
Small gray text: "AAT Intel Briefing · Generated automatically by n8n ·
[timestamp]".

## Data flow / how it's filled in
- Input: an array of event objects, each with `entity` (company name),
  `category`, `summary`, `sourceUrl`, and `severity` (`Urgent` / `Watch` /
  `Informational`).
- The Code node groups events by entity, caps each entity at 2 events for
  the sort/display step, sorts everything by severity, and builds four
  HTML fragments: the Top Intel rows, the ticker items, the index buttons,
  and the full detail sections — then assembles them into one complete
  HTML document string.
- That string is passed to the Gmail node as the email body.

## Severity handling (current state)
Severity is displayed everywhere (colored badges: red = Urgent, amber =
Watch, blue = Informational) but nothing is filtered or hidden based on
severity yet — every event currently appears in the Full Briefing section
regardless of severity. Severity only affects sort order and the Top Intel
cutoff (top 5 by severity). Filtering/hiding rules are a planned next step,
not yet implemented.

## Known technical notes
- Uses `$now.toFormat(...)` (Luxon), not `$now.format(...)` — the latter
  throws a TypeError in n8n's Code node runtime.
- All dynamic text is HTML-escaped before insertion to avoid broken markup
  from special characters in company names, summaries, etc.
- Company names are slugified (lowercase, non-alphanumeric → hyphens) to
  generate anchor IDs, so the same slug function is used for both the
  index buttons and the detail section IDs to guarantee they match.
- Table-based layout throughout (not flexbox/grid) for compatibility with
  Gmail and Outlook rendering.
