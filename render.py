"""
render.py — turn lines.json into a CSV and a standalone HTML viewer.

No network. Safe to run any time; it only reads the JSON file.

    python render.py                       # lines.json -> lines.csv + lines.html
    python render.py week1.json             # week1.json -> week1.csv + week1.html
    open lines.html                         # double-click, no server needed

fetch_lines.py calls this automatically after a successful --out write
(disable with --no-render).

Accepts either the wrapped shape { "_meta": {...}, "games": [...] } or a bare
list of games. lines.csv is the games only (same field names / sign convention),
~70% fewer bytes than the JSON.
"""

import csv
import json
import sys
from pathlib import Path

FIELDS = [
    "commence_time", "away_team", "away_abbr", "home_team", "home_abbr", "book",
    "moneyline_home", "moneyline_away", "moneyline_at",
    "spread", "spread_price_home", "spread_price_away", "spread_at",
    "total", "total_over_price", "total_under_price", "total_at",
    "book_last_update",
]


def write_csv(games, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for g in games:
            w.writerow(g)


def write_html(games, path, meta=None):
    # Data is embedded; all formatting (local time, "x ago", favorite side)
    # happens in the browser so the file is fully self-contained.
    meta = meta or {}
    payload = json.dumps(games, separators=(",", ":"))
    book = meta.get("book") or (games[0].get("book") if games else "—") or "—"
    fetched = meta.get("fetched_at") or ""
    present = meta.get("markets_present") or []
    present_lbl = ", ".join({"h2h": "ML", "spreads": "spread", "totals": "total"}.get(m, m)
                            for m in present)
    usage = meta.get("usage") or {}
    if usage:
        credits = (f"{usage.get('credits_used', '?')} / {usage.get('monthly_cap', 500)} "
                   f"credits this month · ~{usage.get('pulls_left_est', '?')} pulls left")
    else:
        credits = ""
    html = _TEMPLATE.replace("__BOOK__", _esc(book)) \
                    .replace("__FETCHED__", _esc(fetched)) \
                    .replace("__PRESENT__", _esc(present_lbl)) \
                    .replace("__COUNT__", str(len(games))) \
                    .replace("__CREDITS__", _esc(credits)) \
                    .replace("__DATA__", payload)
    Path(path).write_text(html, encoding="utf-8")


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render(games, meta=None, stem="lines", outdir="."):
    out = Path(outdir)
    csv_path, html_path = out / f"{stem}.csv", out / f"{stem}.html"
    write_csv(games, csv_path)
    write_html(games, html_path, meta=meta)
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
  #credits { display: block; margin-top: 3px; }
  #credits:empty { display: none; }
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
  .upd { color: var(--muted); font-size: 11px; white-space: nowrap; }
  .upd.stale { color: var(--fav); }
  .empty { color: var(--muted); }
  footer { margin-top: 22px; color: var(--muted); font-size: 12px; }
  code { background: var(--row-alt); padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>__BOOK__ &middot; NFL lines</h1>
  <div class="sub">
    <b>__COUNT__</b> games &nbsp;·&nbsp; markets: <b>__PRESENT__</b>
    &nbsp;·&nbsp; last pull <b id="fetched">__FETCHED__</b><span id="ago"></span>
    <span id="credits">__CREDITS__</span>
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
    Spread shown as the favorite's number. The small age after each price is
    when that market was last pulled (<span class="upd"><span class="stale">red</span></span>
    = over a day old) — markets refresh on their own cadences. Full data + field
    notes in the source <code>.json</code> (<code>_meta</code> block).
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
const agoC = iso => {                       // compact: 4m / 3h / 2d
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 3600) return Math.max(1, Math.round(s/60)) + 'm';
  if (s < 86400) return Math.round(s/3600) + 'h';
  return Math.round(s/86400) + 'd';
};
const STALE_S = 26 * 3600;
const sign = n => (n > 0 ? '+' + n : '' + n);

function age(iso) {                          // " · 2h" tag, red if > ~1 day
  if (!iso) return '';
  const stale = (Date.now() - new Date(iso)) / 1000 > STALE_S;
  return ` <span class="upd${stale ? ' stale' : ''}">· ${agoC(iso)}</span>`;
}

function spreadCell(g) {
  if (g.spread === null || g.spread === undefined)
    return g.spread_at ? '<span class="empty">— (none posted)</span>' : '<span class="empty">—</span>';
  // convention: positive spread => home favored
  const homeFav = g.spread > 0;
  const favAbbr = homeFav ? (g.home_abbr || 'HOME') : (g.away_abbr || 'AWAY');
  const line = -Math.abs(g.spread);
  const px = homeFav ? g.spread_price_home : g.spread_price_away;
  return `<span class="fav">${favAbbr} ${line}</span>` +
         (px != null ? ` <span class="px">${sign(px)}</span>` : '') + age(g.spread_at);
}
function totalCell(g) {
  if (g.total === null || g.total === undefined)
    return g.total_at ? '<span class="empty">— (none posted)</span>' : '<span class="empty">—</span>';
  const o = g.total_over_price, u = g.total_under_price;
  return `${g.total}` +
    (o != null || u != null
      ? ` <span class="px">o ${o!=null?sign(o):'–'} / u ${u!=null?sign(u):'–'}</span>`
      : '') + age(g.total_at);
}
function mlCell(g) {
  const a = g.moneyline_away, h = g.moneyline_home;
  if (a == null && h == null)
    return g.moneyline_at ? '<span class="empty">— (none posted)</span>' : '<span class="empty">—</span>';
  return `<span class="num">${g.away_abbr||'A'} ${a!=null?sign(a):'–'}` +
         ` / ${g.home_abbr||'H'} ${h!=null?sign(h):'–'}</span>` + age(g.moneyline_at);
}

document.getElementById('rows').innerHTML = GAMES.map(g => `
  <tr>
    <td class="day">${fmtKick(g.commence_time)}</td>
    <td class="match">${g.away_abbr||g.away_team} <span class="at">@</span> ${g.home_abbr||g.home_team}</td>
    <td class="num">${spreadCell(g)}</td>
    <td class="num">${totalCell(g)}</td>
    <td class="num">${mlCell(g)}</td>
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


def load(src):
    """Return (games, meta) from either the wrapped object or a bare list."""
    data = json.loads(Path(src).read_text())
    if isinstance(data, dict) and "games" in data:
        return data["games"], data.get("_meta", {})
    if isinstance(data, list):
        return data, {}
    raise ValueError("not a lines file (wrapped object or list expected; "
                     "did you pass a --raw dump?)")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("lines.json")
    if not src.exists():
        sys.exit(f"{src} not found — run fetch_lines.py first, or pass a path.")
    try:
        games, meta = load(src)
    except ValueError as e:
        sys.exit(f"{src}: {e}")
    csv_path, html_path = render(games, meta=meta, stem=src.stem, outdir=src.parent)
    print(f"[ok] {len(games)} games -> {csv_path}  +  {html_path}")


if __name__ == "__main__":
    main()
