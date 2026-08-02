"""Read-only one-off: render scripts/_open_items_out.json into a mobile-friendly
single-file HTML triage report. Not part of the maintained scripts/ toolkit.
"""
import json
import html
from datetime import datetime, timezone

with open("scripts/_open_items_out.json") as f:
    data = json.load(f)

NOW = datetime.now(timezone.utc)


def parse_dt(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_days(dt):
    if not dt:
        return 0
    return (NOW - dt).total_seconds() / 86400


def fmt_age(dt):
    if not dt:
        return "unknown"
    d = age_days(dt)
    if d < 1:
        return f"{int(d * 24)}h ago"
    return f"{int(d)}d ago"


def score_suggestion(s):
    sc = 0
    if s["type"] == "bug":
        sc += 50
    sc += min(s.get("message_count") or 0, 20) * 2
    created = parse_dt(s.get("created_at"))
    last_act = parse_dt(s.get("last_activity_at"))
    a = age_days(created)
    sc += max(0, 30 - a)  # newer = higher, decays over ~30d
    la = age_days(last_act)
    sc += max(0, 10 - la) * 1.5  # recently active bump
    return sc


def score_ticket(t):
    sc = 40
    sc += min(t.get("message_count") or 0, 20) * 2
    if t.get("status") == "close_requested":
        sc -= 15  # already winding down
    created = parse_dt(t.get("date_added"))
    sc += max(0, 20 - age_days(created))
    return sc


suggestions = data["suggestions"]
tickets = data["tickets"]

for s in suggestions:
    s["_score"] = score_suggestion(s)
for t in tickets:
    t["_score"] = score_ticket(t)

bugs = sorted([s for s in suggestions if s["type"] == "bug"], key=lambda x: -x["_score"])
suggs = sorted([s for s in suggestions if s["type"] != "bug"], key=lambda x: -x["_score"])
tickets_sorted = sorted(tickets, key=lambda x: -x["_score"])

all_scored = bugs + suggs + tickets_sorted
top_picks = sorted(all_scored, key=lambda x: -x["_score"])[:5]


def esc(s):
    return html.escape(s or "")


PRIMARY_GUILD_ID = "1172737525069135962"


def discord_link(thread_id):
    if not thread_id:
        return None
    return f"https://discord.com/channels/{PRIMARY_GUILD_ID}/{thread_id}"


def render_body(md, limit=420):
    text = md or ""
    truncated = len(text) > limit
    text = text[:limit]
    out = esc(text)
    if truncated:
        out += "…"
    return out.replace("\n", "<br>")


def render_suggestion_card(s, kind_label, kind_class):
    link = discord_link(s.get("discord_thread_id"))
    link_html = f'<a class="thread-link" href="{esc(link)}" target="_blank">Open thread ↗</a>' if link else ""
    return f"""
    <div class="card {kind_class}">
      <div class="card-head">
        <span class="badge {kind_class}">{kind_label}</span>
        <span class="score">score {s['_score']:.0f}</span>
      </div>
      <h3>{esc(s['title'])}</h3>
      <div class="meta">
        by <b>{esc(s.get('author_name') or 'unknown')}</b>
        &middot; opened {fmt_age(parse_dt(s.get('created_at')))}
        &middot; last activity {fmt_age(parse_dt(s.get('last_activity_at')))}
        &middot; {s.get('message_count') or 0} replies
      </div>
      <p class="body">{render_body(s.get('body_md'))}</p>
      {link_html}
    </div>"""


def render_ticket_card(t):
    status_label = {"open": "Open", "close_requested": "Close requested"}.get(t.get("status"), t.get("status"))
    return f"""
    <div class="card ticket">
      <div class="card-head">
        <span class="badge ticket">Ticket · {esc(t.get('type'))}</span>
        <span class="score">score {t['_score']:.0f}</span>
      </div>
      <h3>#{t['ticket_id']} {esc(t.get('subject') or '(no subject)')}</h3>
      <div class="meta">
        status <b>{esc(status_label)}</b>
        &middot; opened by {esc(t.get('first_author') or 'unknown')}
        &middot; opened {fmt_age(parse_dt(t.get('date_added')))}
        &middot; updated {fmt_age(parse_dt(t.get('date_updated')))}
        &middot; {t.get('message_count') or 0} messages
      </div>
      <p class="body">{render_body(t.get('first_message'))}</p>
    </div>"""


def render_top_pick(item):
    if "ticket_id" in item:
        title = f"#{item['ticket_id']} {item.get('subject') or '(no subject)'}"
        kind = f"Ticket · {item.get('type')}"
    else:
        title = item["title"]
        kind = "Bug" if item["type"] == "bug" else "Suggestion"
    return f'<li><span class="badge small">{esc(kind)}</span> <b>{esc(title)}</b> <span class="score">({item["_score"]:.0f})</span></li>'


bug_cards = "\n".join(render_suggestion_card(s, "Bug", "bug") for s in bugs) or '<p class="empty">No open bug reports 🎉</p>'
sugg_cards = "\n".join(render_suggestion_card(s, "Suggestion", "suggestion") for s in suggs) or '<p class="empty">No open suggestions.</p>'
ticket_cards = "\n".join(render_ticket_card(t) for t in tickets_sorted) or '<p class="empty">No open tickets.</p>'
top_picks_html = "\n".join(render_top_pick(i) for i in top_picks)

generated_at = NOW.strftime("%Y-%m-%d %H:%M UTC")

html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DropTracker — Open Items Triage</title>
<style>
  :root {{
    --bg: #0f1115; --card: #1a1d24; --border: #2a2e38;
    --text: #e6e8ec; --muted: #9aa1ac; --accent: #5b8cff;
    --bug: #ff5c5c; --sugg: #4fd08a; --ticket: #f2b84b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 16px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.45;
  }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 18px; }}
  .summary {{
    display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px;
  }}
  .stat {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; flex: 1; min-width: 90px; text-align: center;
  }}
  .stat .n {{ font-size: 1.5rem; font-weight: 700; }}
  .stat .l {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
  section {{ margin-bottom: 28px; }}
  section > h2 {{
    font-size: 1.05rem; border-bottom: 1px solid var(--border); padding-bottom: 6px; margin-bottom: 12px;
    position: sticky; top: 0; background: var(--bg); padding-top: 6px;
  }}
  .top-picks {{
    background: var(--card); border: 1px solid var(--accent); border-radius: 12px; padding: 12px 16px;
  }}
  .top-picks ol {{ margin: 8px 0 0; padding-left: 20px; }}
  .top-picks li {{ margin-bottom: 6px; font-size: 0.92rem; }}
  .card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px; margin-bottom: 12px; border-left: 4px solid var(--border);
  }}
  .card.bug {{ border-left-color: var(--bug); }}
  .card.suggestion {{ border-left-color: var(--sugg); }}
  .card.ticket {{ border-left-color: var(--ticket); }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
  .badge {{
    font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
    padding: 2px 8px; border-radius: 999px; background: #2a2e38; color: var(--muted);
  }}
  .badge.bug {{ background: rgba(255,92,92,0.15); color: var(--bug); }}
  .badge.suggestion {{ background: rgba(79,208,138,0.15); color: var(--sugg); }}
  .badge.ticket {{ background: rgba(242,184,75,0.15); color: var(--ticket); }}
  .badge.small {{ font-size: 0.62rem; padding: 1px 6px; }}
  .score {{ font-size: 0.72rem; color: var(--muted); }}
  h3 {{ font-size: 1rem; margin: 4px 0 6px; }}
  .meta {{ font-size: 0.78rem; color: var(--muted); margin-bottom: 8px; }}
  .body {{ font-size: 0.88rem; margin: 0 0 8px; color: #d3d6dc; }}
  .thread-link {{ font-size: 0.8rem; color: var(--accent); text-decoration: none; }}
  .empty {{ color: var(--muted); font-size: 0.85rem; font-style: italic; }}
  footer {{ color: var(--muted); font-size: 0.72rem; text-align: center; margin-top: 30px; }}
</style>
</head>
<body>
  <h1>Open Items Triage Report</h1>
  <div class="sub">Generated {generated_at} · pulled from the suggestions/bug/ticket DB mirror (not live Discord)</div>

  <div class="summary">
    <div class="stat"><div class="n">{len(bugs)}</div><div class="l">Open Bugs</div></div>
    <div class="stat"><div class="n">{len(suggs)}</div><div class="l">Open Suggestions</div></div>
    <div class="stat"><div class="n">{len(tickets_sorted)}</div><div class="l">Open Tickets</div></div>
  </div>

  <div class="top-picks">
    <b>Suggested focus for today</b> (ranked by type, reply volume, and recency)
    <ol>
      {top_picks_html}
    </ol>
  </div>

  <section>
    <h2>🐛 Bug Reports ({len(bugs)})</h2>
    {bug_cards}
  </section>

  <section>
    <h2>💡 Suggestions ({len(suggs)})</h2>
    {sugg_cards}
  </section>

  <section>
    <h2>🎫 Support Tickets ({len(tickets_sorted)})</h2>
    {ticket_cards}
  </section>

  <footer>DropTracker internal triage report — generated from suggestions/tickets tables, not for external distribution.</footer>
</body>
</html>
"""

with open("scripts/_open_items_report.html", "w") as f:
    f.write(html_out)

print("wrote scripts/_open_items_report.html", len(html_out), "bytes")
