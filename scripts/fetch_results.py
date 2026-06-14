"""
Henter ferske resultater fra football-data.org og bygger fasit.json.
Oversetter engelske API-lagnavn til norske via team_map.no.json.
Fletter inn manuelle felt (assist, kort) fra manual.json.

Kjøres av GitHub Action. Krever miljøvariabel FOOTBALL_DATA_TOKEN.
Bruk: python scripts/fetch_results.py tournaments/vm-2026
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API_BASE = "https://api.football-data.org/v4"
HERE = Path(__file__).resolve().parent
TEAM_MAP = json.loads((HERE / "team_map.no.json").read_text(encoding="utf-8"))


def to_no(name: str) -> str:
    if not name:
        return ""
    return TEAM_MAP.get(name.strip(), name.strip())


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"X-Auth-Token": token}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_competition(cfg: dict, token: str) -> dict:
    comp = cfg["football_data_competition"]
    season = cfg.get("api_football_season", 2026)

    fact = {
        "matches": [], "group_winners": {}, "r16": [], "r8": [],
        "kvart": [], "semi": [], "bronse": [], "bronse_vinner": "",
        "finale": [], "vm_vinner": "", "toppscorer": "", "antall_maal": 0,
    }

    # --- Alle kamper ---
    data = _get(f"/competitions/{comp}/matches?season={season}", token)
    time.sleep(6)
    all_matches = data.get("matches", [])
    print(f"  Totalt {len(all_matches)} kamper hentet")

    stage_teams: dict[str, set] = {
        "r16": set(), "r8": set(), "kvart": set(), "semi": set(),
        "bronse": set(), "finale": set(),
    }

    stage_map = {
        "ROUND_OF_32": "r16",
        "ROUND_OF_16": "r8",
        "QUARTER_FINALS": "kvart",
        "SEMI_FINALS": "semi",
        "THIRD_PLACE": "bronse",
        "FINAL": "finale",
    }

    group_matches = []
    for m in all_matches:
        stage = m.get("stage", "")
        status = m.get("status", "")
        home = to_no(m.get("homeTeam", {}).get("name", ""))
        away = to_no(m.get("awayTeam", {}).get("name", ""))

        # Gruppespill
        if stage == "GROUP_STAGE" and status == "FINISHED":
            ft = m.get("score", {}).get("fullTime", {})
            h, a = ft.get("home"), ft.get("away")
            if h is not None and a is not None:
                res = "H" if h > a else ("B" if a > h else "U")
                group_matches.append(res)

        # Sluttspill
        key = stage_map.get(stage)
        if key and home:
            stage_teams[key].add(home)
            stage_teams[key].add(away)

        # Vinnere
        if status == "FINISHED":
            winner = m.get("score", {}).get("winner")
            if stage == "FINAL":
                if winner == "HOME_TEAM":
                    fact["vm_vinner"] = home
                elif winner == "AWAY_TEAM":
                    fact["vm_vinner"] = away
            if stage == "THIRD_PLACE":
                if winner == "HOME_TEAM":
                    fact["bronse_vinner"] = home
                elif winner == "AWAY_TEAM":
                    fact["bronse_vinner"] = away

    for i, res in enumerate(group_matches, 1):
        fact["matches"].append({"n": i, "result": res})

    # Sluttspill-lister (kumulativt)
    all_finale = stage_teams["finale"]
    all_bronse = stage_teams["bronse"]
    all_semi = stage_teams["semi"] | all_finale | all_bronse
    all_kvart = stage_teams["kvart"] | all_semi
    all_r8 = stage_teams["r8"] | all_kvart
    all_r16 = stage_teams["r16"] | all_r8

    fact["r16"] = sorted(all_r16)
    fact["r8"] = sorted(all_r8)
    fact["kvart"] = sorted(all_kvart)
    fact["semi"] = sorted(all_semi)
    fact["bronse"] = sorted(all_bronse)
    fact["finale"] = sorted(all_finale)

    # --- Gruppevinnere fra standings ---
    try:
        st = _get(f"/competitions/{comp}/standings?season={season}", token)
        time.sleep(6)
        standings = st.get("standings", [])
        print(f"  Standings typer: {[g.get('type') for g in standings]}")
        # Debug: vis strukturen på første TOTAL-oppføring
        for grp in standings:
            if grp.get("type") == "TOTAL":
                table = grp.get("table", [])
                if table:
                    first = table[0]
                    print(f"  Første rad-nøkler: {list(first.keys())}")
                    print(f"  gruppe-felt: {first.get('group','INGEN')}, team: {first.get('team',{}).get('name','?')}")
                break
        for grp in standings:
            if grp.get("type") != "TOTAL":
                continue
            table = grp.get("table", [])
            # Grupper kan ligge som `group` på hvert lag i tabellen
            seen_groups: dict[str, str] = {}
            for row in table:
                grp_name = row.get("group", "")
                team_name = to_no(row.get("team", {}).get("name", ""))
                if grp_name and grp_name not in seen_groups:
                    seen_groups[grp_name] = team_name
            fact["group_winners"].update(seen_groups)
        print(f"  Gruppevinnere: {len(fact['group_winners'])} hentet")
    except Exception as e:
        print(f"  (standings hoppet over: {e})")

    # --- Toppscorer ---
    try:
        sc = _get(f"/competitions/{comp}/scorers?season={season}&limit=20", token)
        scorers = sc.get("scorers", [])
        if scorers:
            fact["toppscorer"] = scorers[0]["player"]["name"]
            fact["antall_maal"] = sum(s.get("goals", 0) or 0 for s in scorers)
            print(f"  Toppscorer: {fact['toppscorer']} ({fact['antall_maal']} mål totalt)")
    except Exception as e:
        print(f"  (toppscorer hoppet over: {e})")

    return fact


def main(tournament_dir: str):
    tdir = Path(tournament_dir)
    cfg = json.loads((tdir / "tournament.json").read_text(encoding="utf-8"))
    manual_path = tdir / "data" / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8")) if manual_path.exists() else {}
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()

    fasit_path = tdir / "data" / "fasit.json"
    existing = json.loads(fasit_path.read_text(encoding="utf-8")) if fasit_path.exists() else {}

    if token and cfg.get("football_data_competition"):
        print(f"Henter resultater for {cfg['kort_navn']} fra football-data.org ...")
        try:
            fact = fetch_competition(cfg, token)
        except Exception as e:
            print(f"  Auto-henting feilet ({e}); beholder eksisterende fasit.")
            fact = existing
    else:
        print("  Ingen FOOTBALL_DATA_TOKEN eller competition-kode - hopper over.")
        fact = existing

    # Flett inn manuelle felt
    if manual.get("assist"):
        fact["assist"] = manual["assist"]
    if manual.get("antall_kort") is not None:
        fact["antall_kort"] = manual["antall_kort"]

    fasit_path.write_text(json.dumps(fact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Skrev {fasit_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")
