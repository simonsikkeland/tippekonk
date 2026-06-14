"""
Henter ferske resultater fra football-data.org og bygger fasit.json
for en turnering. Oversetter engelske API-lagnavn til norske via
team_map.no.json. Fletter inn manuelle felt (assist, kort) fra manuell.json.

Kjøres av GitHub Action. Krever miljøvariabel FOOTBALL_DATA_TOKEN.
Hvis token mangler, hopper den pent over auto-henting og beholder
det som ligger i manuell.json — så bygget aldri kræsjer.

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
    """Engelsk API-navn -> norsk arknavn. Faller tilbake til original."""
    if not name:
        return ""
    return TEAM_MAP.get(name.strip(), name.strip())


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{API_BASE}{path}", headers={"X-Auth-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_competition(cfg: dict, token: str) -> dict:
    comp = cfg["football_data_competition"]
    fact = {
        "matches": [], "group_winners": {}, "r16": [], "r8": [],
        "kvart": [], "semi": [], "bronse": [], "bronse_vinner": "",
        "finale": [], "vm_vinner": "", "toppscorer": "",
    }

    # --- Kamper (gruppespill) -> H/U/B ---
    data = _get(f"/competitions/{comp}/matches", token)
    time.sleep(6)  # respekter 10 req/min
    n = 0
    for m in data.get("matches", []):
        if m.get("stage") != "GROUP_STAGE" or m.get("status") != "FINISHED":
            continue
        n += 1
        ft = m["score"]["fullTime"]
        h, a = ft.get("home"), ft.get("away")
        if h is None or a is None:
            continue
        res = "H" if h > a else ("B" if a > h else "U")
        fact["matches"].append({"n": n, "result": res})

    # --- Gruppevinnere fra tabeller ---
    try:
        st = _get(f"/competitions/{comp}/standings", token)
        time.sleep(6)
        for grp in st.get("standings", []):
            if grp.get("type") != "TOTAL":
                continue
            label = grp.get("group", "")
            table = grp.get("table", [])
            if label and table:
                # "GROUP_A" -> "Gruppe A"
                norm_label = label.replace("GROUP_", "Gruppe ").title().replace("Gruppe ", "Gruppe ")
                norm_label = "Gruppe " + label.split("_")[-1]
                fact["group_winners"][norm_label] = to_no(table[0]["team"]["name"])
    except Exception as e:
        print(f"  (standings hoppet over: {e})")

    # --- Sluttspill: hvilke lag nådde hver runde ---
    stage_map = {
        "LAST_16": "r16", "QUARTER_FINALS": "kvart",
        "SEMI_FINALS": "semi", "THIRD_PLACE": "bronse", "FINAL": "finale",
    }
    seen = {k: set() for k in stage_map.values()}
    for m in data.get("matches", []):
        key = stage_map.get(m.get("stage"))
        if not key:
            continue
        for side in ("homeTeam", "awayTeam"):
            t = m.get(side, {}).get("name")
            if t:
                seen[key].add(to_no(t))
    # r8 i arket = "videre til 8-dels" = de som spiller LAST_16 (16 lag).
    # r16 i arket = "videre til 16-dels" = 32 lag (alle som nådde sluttspill).
    fact["r16"] = sorted(seen["r16"] | seen["kvart"] | seen["semi"] | seen["bronse"] | seen["finale"])
    fact["r8"] = sorted(seen["kvart"] | seen["semi"] | seen["finale"])
    fact["kvart"] = sorted(seen["semi"] | seen["finale"] | seen["bronse"])
    fact["semi"] = sorted(seen["finale"] | seen["bronse"])
    fact["bronse"] = sorted(seen["bronse"])
    fact["finale"] = sorted(seen["finale"])

    # Vinnere av finale/bronsefinale
    for m in data.get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        if m.get("stage") == "FINAL":
            w = m.get("score", {}).get("winner")
            if w == "HOME_TEAM":
                fact["vm_vinner"] = to_no(m["homeTeam"]["name"])
            elif w == "AWAY_TEAM":
                fact["vm_vinner"] = to_no(m["awayTeam"]["name"])
        if m.get("stage") == "THIRD_PLACE":
            w = m.get("score", {}).get("winner")
            if w == "HOME_TEAM":
                fact["bronse_vinner"] = to_no(m["homeTeam"]["name"])
            elif w == "AWAY_TEAM":
                fact["bronse_vinner"] = to_no(m["awayTeam"]["name"])

    # --- Toppscorer ---
    try:
        sc = _get(f"/competitions/{comp}/scorers?limit=1", token)
        time.sleep(6)
        scorers = sc.get("scorers", [])
        if scorers:
            fact["toppscorer"] = scorers[0]["player"]["name"]
            fact["antall_maal"] = sum(s.get("goals", 0) for s in _get(
                f"/competitions/{comp}/scorers?limit=100", token).get("scorers", []))
    except Exception as e:
        print(f"  (scorers hoppet over: {e})")

    return fact


def main(tournament_dir: str):
    tdir = Path(tournament_dir)
    cfg = json.loads((tdir / "tournament.json").read_text(encoding="utf-8"))
    manuell = json.loads((tdir / "data" / "manuell.json").read_text(encoding="utf-8"))
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()

    if token:
        print(f"Henter resultater for {cfg['kort_navn']} fra football-data.org ...")
        try:
            fact = fetch_competition(cfg, token)
        except Exception as e:
            print(f"  Auto-henting feilet ({e}); bygger tom fasit.")
            fact = {}
    else:
        print("  Ingen FOOTBALL_DATA_TOKEN satt - hopper over auto-henting.")
        fact = {}

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
