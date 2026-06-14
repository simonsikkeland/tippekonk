"""
Henter ferske resultater fra api-football.com (api-sports.io) og bygger fasit.json
for en turnering. Oversetter engelske API-lagnavn til norske via team_map.no.json.
Fletter inn manuelle felt (assist, kort) fra manuell.json.

Kjøres av GitHub Action. Krever miljøvariabel API_FOOTBALL_KEY.
Hvis nøkkel mangler, hopper den pent over auto-henting.

Bruk: python scripts/fetch_results.py tournaments/vm-2026
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API_BASE = "https://v3.football.api-sports.io"
HERE = Path(__file__).resolve().parent
TEAM_MAP = json.loads((HERE / "team_map.no.json").read_text(encoding="utf-8"))


def to_no(name: str) -> str:
    if not name:
        return ""
    return TEAM_MAP.get(name.strip(), name.strip())


def _get(path: str, key: str) -> dict:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"x-apisports-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_competition(cfg: dict, key: str) -> dict:
    league = cfg["api_football_league"]
    season = cfg["api_football_season"]

    fact = {
        "matches": [], "group_winners": {}, "r16": [], "r8": [],
        "kvart": [], "semi": [], "bronse": [], "bronse_vinner": "",
        "finale": [], "vm_vinner": "", "toppscorer": "", "antall_maal": 0,
    }

    # --- Hent alle kamper ---
    data = _get(f"/fixtures?league={league}&season={season}", key)
    time.sleep(7)
    fixtures = data.get("response", [])
    print(f"  Totalt {len(fixtures)} kamper hentet fra API")

    # Debug: vis unike runder og statuser
    rounds_seen = set()
    statuses_seen = set()
    for f in fixtures[:5]:
        rounds_seen.add(f.get("league", {}).get("round", "?"))
        statuses_seen.add(f.get("fixture", {}).get("status", {}).get("short", "?"))
    if fixtures:
        print(f"  Eksempel runder: {rounds_seen}")
        print(f"  Eksempel statuser: {statuses_seen}")

    group_matches = []
    stage_teams: dict[str, set] = {
        "r16": set(), "r8": set(), "kvart": set(), "semi": set(),
        "bronse": set(), "finale": set(),
    }

    round_map = {
        "Round of 16": "r16",
        "Round of 32": "r16",
        "Quarter-finals": "r8",
        "Semi-finals": "kvart",
        "3rd Place Final": "bronse",
        "Final": "finale",
    }

    for f in fixtures:
        rnd = f.get("league", {}).get("round", "")
        status = f.get("fixture", {}).get("status", {}).get("short", "")
        home = to_no(f.get("teams", {}).get("home", {}).get("name", ""))
        away = to_no(f.get("teams", {}).get("away", {}).get("name", ""))

        # Gruppespill
        if rnd.startswith("Group Stage") and status == "FT":
            goals = f.get("goals", {})
            h, a = goals.get("home"), goals.get("away")
            if h is not None and a is not None:
                res = "H" if h > a else ("B" if a > h else "U")
                group_matches.append(res)

            # Gruppevinner via standings (hentes separat)

        # Sluttspill
        key2 = round_map.get(rnd)
        if key2 and home:
            stage_teams[key2].add(home)
            stage_teams[key2].add(away)

        if status == "FT":
            winner_home = f.get("teams", {}).get("home", {}).get("winner")
            if rnd == "Final":
                fact["vm_vinner"] = home if winner_home else away
            if rnd == "3rd Place Final":
                fact["bronse_vinner"] = home if winner_home else away

    # Kampresultater med løpenummer
    for i, res in enumerate(group_matches, 1):
        fact["matches"].append({"n": i, "result": res})

    # Sluttspill-lister (kumulativt: alle som nådde en runde eller lenger)
    all_finale = stage_teams["finale"]
    all_bronse = stage_teams["bronse"]
    all_semi = stage_teams["kvart"] | all_finale | all_bronse
    all_kvart = stage_teams["r8"] | all_semi
    all_r16 = stage_teams["r16"] | all_kvart

    fact["r16"] = sorted(all_r16)
    fact["r8"] = sorted(all_kvart)
    fact["kvart"] = sorted(all_semi)
    fact["semi"] = sorted(all_finale | all_bronse)
    fact["bronse"] = sorted(all_bronse)
    fact["finale"] = sorted(all_finale)

    # --- Gruppevinnere fra standings ---
    try:
        st = _get(f"/standings?league={league}&season={season}", key)
        time.sleep(7)
        for grp in st.get("response", [{}])[0].get("league", {}).get("standings", []):
            if grp:
                leader = grp[0]
                group_name = leader.get("group", "")
                team_name = to_no(leader.get("team", {}).get("name", ""))
                if group_name and team_name:
                    fact["group_winners"][group_name] = team_name
    except Exception as e:
        print(f"  (standings hoppet over: {e})")

    # --- Toppscorer ---
    try:
        sc = _get(f"/players/topscorers?league={league}&season={season}", key)
        scorers = sc.get("response", [])
        if scorers:
            top = scorers[0]
            fact["toppscorer"] = top["player"]["name"]
            fact["antall_maal"] = sum(
                s.get("statistics", [{}])[0].get("goals", {}).get("total", 0) or 0
                for s in scorers
            )
    except Exception as e:
        print(f"  (toppscorer hoppet over: {e})")

    return fact


def main(tournament_dir: str):
    tdir = Path(tournament_dir)
    cfg = json.loads((tdir / "tournament.json").read_text(encoding="utf-8"))
    manuell_path = tdir / "data" / "manual.json"
    manuell = json.loads(manuell_path.read_text(encoding="utf-8")) if manuell_path.exists() else {}
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()

    if key:
        print(f"Henter resultater for {cfg['kort_navn']} fra api-football.com ...")
        try:
            fact = fetch_competition(cfg, key)
        except Exception as e:
            print(f"  Auto-henting feilet ({e}); beholder eksisterende fasit.")
            fasit_path = tdir / "data" / "fasit.json"
            fact = json.loads(fasit_path.read_text(encoding="utf-8")) if fasit_path.exists() else {}
    else:
        print("  Ingen API_FOOTBALL_KEY satt - hopper over auto-henting.")
        fasit_path = tdir / "data" / "fasit.json"
        fact = json.loads(fasit_path.read_text(encoding="utf-8")) if fasit_path.exists() else {}

    # Flett inn manuelle felt
    if manuell.get("assist"):
        fact["assist"] = manuell["assist"]
    if manuell.get("antall_kort") is not None:
        fact["antall_kort"] = manuell["antall_kort"]

    out = tdir / "data" / "fasit.json"
    out.write_text(json.dumps(fact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Skrev {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")
