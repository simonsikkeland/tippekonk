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
        "kamper": [], "grupper": {}, "flagg": {}, "topp_scorere": [],
    }

    # --- Lag med flagg/crest ---
    try:
        teams_data = _get(f"/competitions/{comp}/teams?season={season}", token)
        time.sleep(6)
        for t in teams_data.get("teams", []):
            norsk = to_no(t.get("name", ""))
            crest = t.get("crest", "")
            if norsk and crest:
                fact["flagg"][norsk] = crest
        print(f"  Flagg: {len(fact['flagg'])} lag")
    except Exception as e:
        print(f"  (flagg hoppet over: {e})")

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
    group_finished_count = 0
    group_total_count = 0
    for m in all_matches:
        stage = m.get("stage", "")
        status = m.get("status", "")
        home = to_no(m.get("homeTeam", {}).get("name", ""))
        away = to_no(m.get("awayTeam", {}).get("name", ""))
        dato = m.get("utcDate", "")[:10]
        grp = m.get("group", "")

        ft = m.get("score", {}).get("fullTime", {})
        h_goals = ft.get("home")
        a_goals = ft.get("away")

        kamp = {
            "home": home, "away": away, "dato": dato,
            "home_score": h_goals, "away_score": a_goals,
            "status": status, "stage": stage, "group": grp,
        }
        fact["kamper"].append(kamp)

        if stage == "GROUP_STAGE":
            group_total_count += 1
            if status == "FINISHED":
                group_finished_count += 1
                if h_goals is not None and a_goals is not None:
                    res = "H" if h_goals > a_goals else ("B" if a_goals > h_goals else "U")
                    fact["matches"].append({"home": home, "away": away, "result": res})

        key = stage_map.get(stage)
        if key and home:
            stage_teams[key].add(home)
            stage_teams[key].add(away)

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

    fact["gruppespill_ferdig"] = group_finished_count == group_total_count and group_total_count > 0
    print(f"  Gruppespill: {group_finished_count}/{group_total_count} kamper ferdig")

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

    # --- Gruppetabeller ---
    group_tables: dict[str, dict[str, list]] = {}
    for m in all_matches:
        grp = m.get("group", "")
        if not grp or m.get("stage") != "GROUP_STAGE":
            continue
        home = to_no(m.get("homeTeam", {}).get("name", ""))
        away = to_no(m.get("awayTeam", {}).get("name", ""))
        if home not in group_tables.setdefault(grp, {}):
            group_tables[grp][home] = [0, 0, 0, 0]
        if away not in group_tables[grp]:
            group_tables[grp][away] = [0, 0, 0, 0]
        if m.get("status") != "FINISHED":
            continue
        ft = m.get("score", {}).get("fullTime", {})
        h, a = ft.get("home"), ft.get("away")
        if h is None or a is None:
            continue
        group_tables[grp][home][3] += 1
        group_tables[grp][away][3] += 1
        group_tables[grp][home][2] += h
        group_tables[grp][away][2] += a
        group_tables[grp][home][1] += h - a
        group_tables[grp][away][1] += a - h
        if h > a:
            group_tables[grp][home][0] += 3
        elif a > h:
            group_tables[grp][away][0] += 3
        else:
            group_tables[grp][home][0] += 1
            group_tables[grp][away][0] += 1

    for grp_name, teams in sorted(group_tables.items()):
        sorted_teams = sorted(teams.items(), key=lambda x: (x[1][0], x[1][1], x[1][2]), reverse=True)
        label = "Gruppe " + grp_name.split("_")[-1]
        if sorted_teams:
            fact["group_winners"][label] = sorted_teams[0][0]
        fact["grupper"][label] = [
            {"lag": t, "poeng": s[0], "mf": s[1], "maal_for": s[2], "spilt": s[3]}
            for t, s in sorted_teams
        ]
    print(f"  Gruppevinnere: {len(fact['group_winners'])} beregnet")
    print(f"  Grupper med tabelldata: {len(fact['grupper'])}")

    # --- Toppscorere (topp 10 med detaljer) ---
    try:
        sc = _get(f"/competitions/{comp}/scorers?season={season}&limit=10", token)
        time.sleep(6)
        scorers = sc.get("scorers", [])
        total_goals = 0
        for s in scorers:
            goals = s.get("goals", 0) or 0
            total_goals += goals
            fact["topp_scorere"].append({
                "navn": s["player"]["name"],
                "lag": to_no(s.get("team", {}).get("name", "")),
                "maal": goals,
            })
        if scorers:
            fact["toppscorer"] = scorers[0]["player"]["name"]
        fact["antall_maal"] = total_goals
        print(f"  Toppscorere: {len(fact['topp_scorere'])} hentet, totalt {total_goals} mål")
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

    if manual.get("assist"):
        fact["assist"] = manual["assist"]
    if manual.get("antall_kort") is not None:
        fact["antall_kort"] = manual["antall_kort"]

    fasit_path.write_text(json.dumps(fact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Skrev {fasit_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")
