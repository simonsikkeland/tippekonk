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

sys.path.insert(0, str(HERE))
from fetch_cards import hent_kort  # noqa: E402


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


def fetch_competition(cfg: dict, token: str, existing: dict | None = None) -> dict:
    comp = cfg["football_data_competition"]
    season = cfg.get("api_football_season", 2026)

    # Hardt lås på ferdigspilte kamper. Når en kamp først er sett som
    # FINISHED med begge måltall i lagret fasit.json, fryses resultatet
    # permanent: senere API-svar med null, et annet tall, eller en flippet
    # status ignoreres for den kampen. Bare kamper som ennå ikke er låst
    # oppdateres fra API-et. Dette gjør tabellen stabil selv om API-et er
    # ustabilt — vi henter alt, men beholder kun det nyeste for u-låste kamper.
    locked: dict[tuple, dict] = {}
    for k in (existing or {}).get("kamper", []):
        if (k.get("status") == "FINISHED"
                and k.get("home_score") is not None
                and k.get("away_score") is not None):
            locked[(k.get("home"), k.get("away"))] = k

    def apply_lock(home, away, h, a, status, winner=None):
        lk = locked.get((home, away))
        if lk:
            return lk["home_score"], lk["away_score"], "FINISHED", lk.get("winner", winner)
        return h, a, status, winner

    fact = {
        "matches": [], "group_winners": {}, "r16": [], "r8": [],
        "kvart": [], "semi": [], "bronse": [], "bronse_vinner": "",
        "finale": [], "vm_vinner": "", "toppscorer": "", "antall_maal": 0,
        "kamper": [], "grupper": {}, "flagg": {}, "topp_scorere": [],
    }

    # --- Lag med flagg/crest ---
    # Start fra eksisterende flagg slik at en feilet henting ikke nuller dem.
    fact["flagg"] = dict((existing or {}).get("flagg", {}))
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
        print(f"  (flagg hoppet over, beholder forrige: {e})")

    # --- Alle kamper ---
    data = _get(f"/competitions/{comp}/matches?season={season}", token)
    time.sleep(6)
    all_matches = data.get("matches", [])
    print(f"  Totalt {len(all_matches)} kamper hentet")

    stage_teams: dict[str, set] = {
        "r16": set(), "r8": set(), "kvart": set(), "semi": set(),
        "bronse": set(), "finale": set(),
    }

    # football-data.org bruker LAST_32/LAST_16; eldre/andre kilder ROUND_OF_*.
    # Godta begge så sluttspillet fanges opp uansett navnekonvensjon.
    stage_map = {
        "LAST_32": "r16", "ROUND_OF_32": "r16",
        "LAST_16": "r8", "ROUND_OF_16": "r8",
        "QUARTER_FINALS": "kvart", "QUARTER_FINAL": "kvart",
        "SEMI_FINALS": "semi", "SEMI_FINAL": "semi",
        "THIRD_PLACE": "bronse", "3RD_PLACE": "bronse",
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
        winner = m.get("score", {}).get("winner")

        # Bruk låst resultat hvis kampen allerede er ferdigspilt og lagret.
        h_goals, a_goals, status, winner = apply_lock(
            home, away, h_goals, a_goals, status, winner)

        kamp = {
            "home": home, "away": away, "dato": dato,
            "home_score": h_goals, "away_score": a_goals,
            "status": status, "stage": stage, "group": grp,
            "winner": winner,
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
            # Kreditér avansement fra faktisk resultat, ikke bare fra API-ets
            # neste-runde-oppsett: vinneren av en sluttspillkamp går videre til
            # neste runde med en gang kampen er ferdig (API-et kan henge etter
            # med å trekke neste fixture). Taperen av en semifinale går til
            # bronsefinalen.
            adv = home if winner == "HOME_TEAM" else (away if winner == "AWAY_TEAM" else None)
            tap = away if winner == "HOME_TEAM" else (home if winner == "AWAY_TEAM" else None)
            if adv:
                if stage in ("LAST_32", "ROUND_OF_32"):
                    stage_teams["r8"].add(adv)
                elif stage in ("LAST_16", "ROUND_OF_16"):
                    stage_teams["kvart"].add(adv)
                elif stage in ("QUARTER_FINALS", "QUARTER_FINAL"):
                    stage_teams["semi"].add(adv)
                elif stage in ("SEMI_FINALS", "SEMI_FINAL"):
                    stage_teams["finale"].add(adv)
                    if tap:
                        stage_teams["bronse"].add(tap)
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

    # Hele turneringen ferdig = alle kamper spilt. Styrer når bonusfeltene
    # (toppscorer/assist/antall mål) gir poeng i engine.score().
    finished_all = sum(1 for k in fact["kamper"] if k.get("status") == "FINISHED")
    fact["turnering_ferdig"] = len(fact["kamper"]) > 0 and finished_all == len(fact["kamper"])

    # Monotont resultatsett: en kamp som en gang er registrert som ferdig skal
    # aldri forsvinne, selv om API-et midlertidig utelater den eller nuller
    # resultatet. Fyll på med eventuelle gamle kamper som mangler i nytt svar.
    def _n(s):
        return str(s).strip().lower()
    have = {(_n(m["home"]), _n(m["away"])) for m in fact["matches"]}
    for m in (existing or {}).get("matches", []):
        if (_n(m["home"]), _n(m["away"])) not in have:
            fact["matches"].append(m)
            have.add((_n(m["home"]), _n(m["away"])))

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
        ft = m.get("score", {}).get("fullTime", {})
        h, a = ft.get("home"), ft.get("away")
        h, a, gstatus, _ = apply_lock(home, away, h, a, m.get("status", ""))
        if gstatus != "FINISHED" or h is None or a is None:
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

    # Per-gruppe ferdig-status (alle 6 kamper spilt). Styrer at gruppevinner-
    # poeng gis per ferdigspilt gruppe i engine.score().
    g_tot, g_fin = {}, {}
    for k in fact["kamper"]:
        if k.get("stage") != "GROUP_STAGE" or not k.get("group"):
            continue
        lab = "Gruppe " + k["group"].split("_")[-1]
        g_tot[lab] = g_tot.get(lab, 0) + 1
        if k.get("status") == "FINISHED" and k.get("home_score") is not None:
            g_fin[lab] = g_fin.get(lab, 0) + 1
    fact["grupper_ferdig"] = {lab: g_fin.get(lab, 0) == g_tot[lab] for lab in g_tot}
    print(f"  Ferdige grupper: {sum(1 for v in fact['grupper_ferdig'].values() if v)}/{len(g_tot)}")

    # --- Antall mål totalt fra alle ferdige kamper ---
    total_goals = 0
    for k in fact["kamper"]:
        if k.get("status") == "FINISHED" and k.get("home_score") is not None:
            total_goals += (k["home_score"] or 0) + (k["away_score"] or 0)
    # Monotont: målsummen kan bare øke. En transient API-dipp skal ikke senke den.
    existing_goals = (existing or {}).get("antall_maal") or 0
    fact["antall_maal"] = max(total_goals, existing_goals)
    print(f"  Totalt antall mål: {fact['antall_maal']} (denne henting: {total_goals})")

    # --- Toppscorere (topp 10 med detaljer) ---
    prev_top = (existing or {}).get("toppscorer", "")
    prev_liste = (existing or {}).get("topp_scorere", [])
    try:
        sc = _get(f"/competitions/{comp}/scorers?season={season}&limit=10", token)
        time.sleep(6)
        scorers = sc.get("scorers", [])
        for s in scorers:
            goals = s.get("goals", 0) or 0
            fact["topp_scorere"].append({
                "navn": s["player"]["name"],
                "lag": to_no(s.get("team", {}).get("name", "")),
                "maal": goals,
            })
        if fact["topp_scorere"]:
            ny_leder = fact["topp_scorere"][0]["navn"]
            ny_maal = fact["topp_scorere"][0]["maal"]
            # "Sticky" leder: behold forrige toppscorer med mindre noen nå har
            # STRENGT flere mål. Hindrer flip-flop på likt antall mål (f.eks.
            # Messi <-> Undav) som ellers flytter 10 poeng mellom kjøringene.
            prev_maal_now = next(
                (p["maal"] for p in fact["topp_scorere"] if p["navn"] == prev_top), None)
            if prev_top and prev_maal_now is not None and ny_maal <= prev_maal_now:
                fact["toppscorer"] = prev_top
            else:
                fact["toppscorer"] = ny_leder
        else:
            # Tomt svar -> behold forrige i stedet for å nulle.
            fact["toppscorer"] = prev_top
            fact["topp_scorere"] = prev_liste
        print(f"  Toppscorere: {len(fact['topp_scorere'])} hentet (leder: {fact['toppscorer'] or '-'})")
    except Exception as e:
        # Feilet henting (rate limit e.l.) -> behold forrige verdier.
        fact["toppscorer"] = prev_top
        fact["topp_scorere"] = prev_liste
        print(f"  (toppscorer hoppet over, beholder forrige: {e})")

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
            fact = fetch_competition(cfg, token, existing)
        except Exception as e:
            print(f"  Auto-henting feilet ({e}); beholder eksisterende fasit.")
            fact = existing
    else:
        print("  Ingen FOOTBALL_DATA_TOKEN eller competition-kode - hopper over.")
        fact = existing

    # Degraderingsvakt: hvis ny fasit er dårligere enn den lagrede (færre
    # ferdige kamper eller lavere målsum), er API-svaret sannsynligvis
    # ufullstendig — behold eksisterende fasit framfor å publisere et tilbakefall.
    def _finished(d):
        return sum(1 for k in d.get("kamper", []) if k.get("status") == "FINISHED")
    if existing and fact is not existing:
        if _finished(fact) < _finished(existing) or \
                (fact.get("antall_maal") or 0) < (existing.get("antall_maal") or 0):
            print("  ADVARSEL: ny fasit er degradert (færre kamper/mål) — beholder eksisterende.")
            fact = existing

    # Kort-statistikk (gule/røde) scrapes uavhengig av football-data-API-et, så
    # det oppdateres selv om resultat-API-et er rate-limited. antall_kort = total.
    kort = hent_kort(cfg.get("kort_url", ""), existing)
    if kort:
        fact["kort"] = kort
        fact["antall_kort"] = kort.get("total")

    if manual.get("assist"):
        fact["assist"] = manual["assist"]
    if manual.get("antall_kort") is not None:   # manuell verdi overstyrer scrapingen
        fact["antall_kort"] = manual["antall_kort"]

    # Atomisk skriv: skriv til temp og bytt inn, så en avbrutt kjøring aldri
    # etterlater en halvskrevet fasit.json.
    tmp = fasit_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fact, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, fasit_path)
    print(f"Skrev {fasit_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")
