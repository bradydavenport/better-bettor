"""
render.py — turn lines.json into a CSV and a standalone HTML viewer.

No network. Safe to run any time; it only reads the JSON file.

    python render.py                       # lines.json -> lines.csv + lines.html
    python render.py week1.json             # week1.json -> week1.csv + week1.html
    open lines.html                         # double-click, no server needed

fetch_lines.py calls this automatically after a successful --out write
(disable with --no-render).

Handoff note: the canonical file for another agent is still lines.json — it is
tiny (~9 KB for a full NFL week) and self-describing. lines.csv is the same
data, same field names and sign convention, ~40% fewer bytes; prefer it only
if you are piping a large multi-week / multi-sport dump.
"""

import csv
import json
import sys
from pathlib import Path

FIELDS = [
    "commence_time", "away_team", "away_abbr", "home_team", "home_abbr",
    "book", "spread", "spread_price_home", "spread_price_away",
    "total", "total_over_price", "total_under_price",
    "moneyline_home", "moneyline_away", "last_update", "fetched_at",
]


def write_csv(games, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for g in games:
            w.writerow(g)


def write_html(games, path):
    # Data is embedded; all formatting (local time, "x ago", favorite side)
    # happens in the browser so the file is fully self-contained.
    payload = json.dumps(games, separators=(",", ":"))
    book = (games[0].get("book") if games else "—") or "—"
    fetched = games[0].get("fetched_at", "") if games else ""
    html = _TEMPLATE.replace("__BOOK__", _esc(book)) \
                    .replace("__FETCHED__", _esc(fetched)) \
                    .replace("__COUNT__", str(len(games))) \
                    .replace("__DATA__", payload)
    Path(path).write_text(html, encoding="utf-8")


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render(games, stem="lines", outdir="."):
    out = Path(outdir)
    csv_path, html_path = out / f"{stem}.csv", out / f"{stem}.html"
    write_csv(games, csv_path)
    write_html(games, html_path)
    return csv_path, html_path


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__BOOK__ lines</title>
<style>
  :root {
    --bg: #fbfbfa; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
    --row: #ffffff; --row-alt: #f5f5f4; --accent: #1d4ed8; --fav: #b91c1c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16171a; --fg: #e8e8e6; --muted: #9aa0a6; --line: #2b2d31;
      --row: #1c1d21; --row-alt: #202226; --accent: #7aa2ff; --fav: #ff7a7a;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 900px; margin: 0 auto; padding: 28px 20px 60px; }
  h1 { font-size: 20px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  .sub b { color: var(--fg); font-weight: 600; }
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  td.match, td.day, td.num { white-space: nowrap; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); }
  th { font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
  tbody tr:nth-child(odd) { background: var(--row); }
  tbody tr:nth-child(even) { background: var(--row-alt); }
  .day { color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .match { font-weight: 600; }
  .match .at { color: var(--muted); font-weight: 400; }
  .num { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .fav { color: var(--fav); font-weight: 600; }
  .px { color: var(--muted); font-size: 12px; }
  .empty { color: var(--muted); }
  footer { margin-top: 22px; color: var(--muted); font-size: 12px; }
  code { background: var(--row-alt); padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>__BOOK__ &middot; NFL lines</h1>
  <div class="sub">
    <b>__COUNT__</b> games &nbsp;·&nbsp; fetched <b id="fetched">__FETCHED__</b>
    <span id="ago"></span>
  </div>
  <div class="scroll">
  <table>
    <thead>
      <tr>
        <th>Kickoff</th><th>Matchup</th><th>Spread</th><th>Total</th><th>Moneyline</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  </div>
  <footer>
    Spread shown as the favorite's number. Source of truth: <code>lines.json</code>.
  </footer>
</div>
<script>
const GAMES = __DATA__;

const fmtKick = iso => {
  const d = new Date(iso);
  return d.toLocaleDateString([], {weekday:'short', month:'numeric', day:'numeric'})
       + ' ' + d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
};
const ago = iso => {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s/60) + ' min ago';
  if (s < 172800) return Math.round(s/3600) + ' h ago';
  return Math.round(s/86400) + ' d ago';
};
const sign = n => (n > 0 ? '+' + n : '' + n);

function spreadCell(g) {
  if (g.spread === null || g.spread === undefined) return '<span class="empty">—</span>';
  // convention: positive spread => home favored
  const homeFav = g.spread > 0;
  const favAbbr = homeFav ? (g.home_abbr || 'HOME') : (g.away_abbr || 'AWAY');
  const line = -Math.abs(g.spread);
  const px = homeFav ? g.spread_price_home : g.spread_price_away;
  return `<span class="fav">${favAbbr} ${line}</span>` +
         (px != null ? ` <span class="px">${sign(px)}</span>` : '');
}
function totalCell(g) {
  if (g.total === null || g.total === undefined) return '<span class="empty">—</span>';
  const o = g.total_over_price, u = g.total_under_price;
  return `${g.total}` +
    (o != null || u != null
      ? ` <span class="px">o ${o!=null?sign(o):'–'} / u ${u!=null?sign(u):'–'}</span>`
      : '');
}
function mlCell(g) {
  const a = g.moneyline_away, h = g.moneyline_home;
  if (a == null && h == null) return '<span class="empty">—</span>';
  return `<span class="num">${g.away_abbr||'A'} ${a!=null?sign(a):'–'}` +
         ` / ${g.home_abbr||'H'} ${h!=null?sign(h):'–'}</span>`;
}

document.getElementById('rows').innerHTML = GAMES.map(g => `
  <tr>
    <td class="day">${fmtKick(g.commence_time)}</td>
    <td class="match">${g.away_abbr||g.away_team} <span class="at">@</span> ${g.home_abbr||g.home_team}</td>
    <td class="num">${spreadCell(g)}</td>
    <td class="num">${totalCell(g)}</td>
    <td>${mlCell(g)}</td>
  </tr>`).join('');

const f = document.getElementById('fetched');
if (f.textContent.trim()) {
  const iso = f.textContent.trim();
  f.textContent = new Date(iso).toLocaleString([], {dateStyle:'medium', timeStyle:'short'});
  document.getElementById('ago').textContent = ' (' + ago(iso) + ')';
}
</script>
</body>
</html>
"""


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("lines.json")
    if not src.exists():
        sys.exit(f"{src} not found — run fetch_lines.py first, or pass a path.")
    games = json.loads(src.read_text())
    if not isinstance(games, list):
        sys.exit(f"{src} is not a normalized lines array (did you pass a --raw dump?).")
    csv_path, html_path = render(games, stem=src.stem, outdir=src.parent)
    print(f"[ok] {len(games)} games -> {csv_path}  +  {html_path}")


if __name__ == "__main__":
    main()
