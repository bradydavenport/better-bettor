import os, requests
from dotenv import load_dotenv

load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

def push_entry(entry):
    tag = "Like" if entry["confidence"] >= 65 else "Lean" if entry["confidence"] >= 55 else "Pass"
    data = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "Matchup": {"title": [{"text": {"content": entry["matchup"]}}]},
            "Date": {"date": {"start": entry["date"]}},
            "League": {"select": {"name": entry["league"]}},
            "Market": {"select": {"name": entry["market"]}},
            "Confidence (%)": {"number": entry["confidence"]},
            "Line (offered)": {"rich_text": [{"text": {"content": entry["line"]}}]},
            "Book": {"rich_text": [{"text": {"content": entry["book"]}}]},
            "Bet Status": {"select": {"name": "Model Only"}},
            "Result": {"select": {"name": entry["result"]}} if entry["result"] else None,
            "Notes": {"rich_text": [{"text": {"content": entry["notes"]}}]}
        }
    }
    data["properties"] = {k: v for k, v in data["properties"].items() if v is not None}
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=data)
    print("  ✅", entry["matchup"] if resp.ok else f"Error {resp.status_code}: {resp.text}")

def push_top_10(top10_list):
    print("Pushing Top 10 entries to Notion...")
    for entry in top10_list:
        push_entry(entry)
    print("Done.")

top_10 = [
  {
    "matchup": "Packers @ Lions",
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Spread",
    "confidence": 72,
    "line": "Packers -2.5",
    "book": "FanDuel",
    "result": None,
    "notes": "Model projections favor Packers with Parsons bump, sharp line movement observed."
  },
  {
    "matchup": "Bengals @ Browns",
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Spread",
    "confidence": 70,
    "line": "Bengals -5.5",
    "book": "DraftKings",
    "result": None,
    "notes": "Model leans Bengals; Joe Burrow's elite metrics vs. backup QB."
  },
  {
    "matchup": "Ravens @ Bills",
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Spread",
    "confidence": 68,
    "line": "Ravens -1.5",
    "book": "Fanatics",
    "result": None,
    "notes": "Simulation models show tight lean on Ravens; tempo may push dell to under instead."
  },
  {
    "matchup": "Cardinals @ Saints",
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Spread",
    "confidence": 67,
    "line": "Cardinals -6.5",
    "book": "Caesars",
    "result": None,
    "notes": "Rare road favorite; Saints instability and new staff risk."
  },
  {
    "matchup": "49ers @ Seahawks",
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Spread",
    "confidence": 65,
    "line": "49ers -2.5",
    "book": "BetMGM",
    "result": None,
    "notes": "Seattle QB uncertainty, Niners defensive depth edge."
  },
  {
    "matchup": "Steelers @ Jets",
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Total",
    "confidence": 64,
    "line": "Under 38.5",
    "book": "FanDuel",
    "result": None,
    "notes": "Low total on opening MNF; anticipated defensive rush and low scoring."
  },
  {
    "matchup": "Raiders @ Patriots",
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Spread",
    "confidence": 63,
    "line": "Patriots -2.5",
    "book": "BetRivers",
    "result": None,
    "notes": "New HC clash; edge leaning Patriots."
  },
  {
    "matchup": "Cowboys @ Eagles",
    "date": "2025-09-04",
    "league": "NFL",
    "market": "Player Prop",
    "confidence": 62,
    "line": "George Pickens Over 55.5 Receiving Yards",
    "book": "Rebet",
    "result": None,
    "notes": "Pickens in secondary with more FG opportunities as passing game expands. Based on Fox Sports insights."
  },
  {
    "matchup": "Chiefs @ Chargers",
    "date": "2025-09-05",
    "league": "NFL",
    "market": "Total",
    "confidence": 61,
    "line": "Over 45.5",
    "book": "Fanatics",
    "result": None,
    "notes": "High-scoring Brazil opener; Fox analyst lean."
  },
  {
    "matchup": "Raiders @ Patriots",  # duplicate matchup but diff market
    "date": "2025-09-07",
    "league": "NFL",
    "market": "Player Prop",
    "confidence": 60,
    "line": "Ashton Jeanty Over 16.5 Rush Attempts",
    "book": "Caesars",
    "result": None,
    "notes": "Pete Carroll run-heavy lean; Fox Sports Betting insight."
  },
]

if __name__ == "__main__":
    push_top_10(top_10)