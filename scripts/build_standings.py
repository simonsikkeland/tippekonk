"""
Bygger stilling.json for en turnering: parser alle opplastede ark,
scorer dem mot fasit.json, og regner ut statistikk ("nærmer seg"-tall).

Bruk: python scripts/build_standings.py tournaments/vm-2026
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import parse_sheet, parse_master, score  # noqa: E402
from bracket import projiser_sluttspill  # noqa: E402


def navn_fra_filnavn(p: Path) -> str:
    s = p.stem
    for junk in ["PIGGYS-VM-KONK-2026", "PIGGY-VM-KONK-2026", "_ferdig", "ferdig"]:
        s = s.replace(junk, "")
    return s.replace("_", " ").replace("-", " ").strip() or p.stem


def main(tournament_dir: str):
    tdir = Path(tournament_dir)
    cfg = json.loads((tdir / "tournament.json").read_text(encoding="utf-8"))
    rules = cfg["regler"]
    fasit_path = tdir / "data" / "fasit.json"
    fact = json.loads(fasit_path.read_text(encoding="utf-8")) if fasit_path.exists() else {}

    ark_dir = tdir / "ark"
    master = ark_dir / "MASTER.xlsx"

    deltakere = []
    if master.exists():
        try:
            entries = parse_master(master)
            for e in entries:
                sc = score(e["pred"], fact, rules)
                deltakere.append({"navn": e["navn"], "pred": e["pred"], "poeng": sc["total"], "linjer": sc["lines"]})
            print(f"  Lastet {len(entries)} deltakere fra MASTER.xlsx")
        except Exception as e:
            print(f"  MASTER.xlsx feilet: {e}")
    else:
        sheets = sorted(ark_dir.glob("*.xlsx")) + sorted(ark_dir.glob("*.xls"))
        for sp in sheets:
            try:
                pred = parse_sheet(sp)
            except Exception as e:
                print(f"  Hoppet over {sp.name}: {e}")
                continue
            sc = score(pred, fact, rules)
            deltakere.append({"navn": navn_fra_filnavn(sp), "pred": pred, "poeng": sc["total"], "linjer": sc["lines"]})

    deltakere.sort(key=lambda d: d["poeng"], reverse=True)
    for i, d in enumerate(deltakere):
        d["plass"] = i + 1

    # "Nærmer seg"-statistikk for bonusfeltene. Felles for antall mål og kort:
    # vis fasit så langt, en projisert sluttsum (skalert opp til alle kampene),
    # og hvem som tippet nærmest både fasit og projeksjonen.
    stats = {}
    kamper = fact.get("kamper") or []
    spilt = sum(1 for k in kamper
                if k.get("status") == "FINISHED"
                and k.get("home_score") is not None and k.get("away_score") is not None)
    totalt = len(kamper)

    def projisert_stat(felt: str) -> dict:
        fasit = fact[felt]
        projeksjon = round(fasit / spilt * totalt) if spilt and totalt else None
        tipp = lambda mål: sorted(  # noqa: E731
            [{"navn": d["navn"], "tipp": d["pred"][felt],
              "diff": abs((d["pred"][felt] or 0) - mål)}
             for d in deltakere if d["pred"].get(felt) is not None],
            key=lambda x: x["diff"])
        s = {"fasit": fasit, "spilt": spilt, "totalt": totalt,
             "projeksjon": projeksjon, "tipp": tipp(fasit)}
        if projeksjon is not None:
            s["mot_projeksjon"] = tipp(projeksjon)
        return s

    if fact.get("antall_maal") is not None:
        stats["antall_maal"] = projisert_stat("antall_maal")
        if fact.get("antall_maal_inkl_straffer") is not None:
            stats["antall_maal"]["fasit_inkl_straffer"] = fact["antall_maal_inkl_straffer"]
    if fact.get("antall_kort") is not None:
        stats["antall_kort"] = projisert_stat("antall_kort")
    # Hvem tippet fortsatt-levende toppscorer/vinner
    if fact.get("toppscorer"):
        stats["toppscorer_treff"] = [d["navn"] for d in deltakere
                                     if d["pred"]["toppscorer"].strip().lower() == fact["toppscorer"].strip().lower()]
    if fact.get("vm_vinner"):
        stats["vinner_treff"] = [d["navn"] for d in deltakere
                                 if d["pred"]["vm_vinner"].strip().lower() == fact["vm_vinner"].strip().lower()]

    # Tippefordeling per kamp (H/U/B), gruppert på dato — driver stat-grafene på siden
    fordeling = tippefordeling(deltakere, fact)
    if fordeling:
        stats["tippefordeling"] = fordeling

    # Alles bonus-tips (mester/toppscorer/antall mål/kort) — status regnes på siden.
    stats["bonustips"] = [
        {"navn": d["navn"],
         "mester": d["pred"].get("vm_vinner") or "",
         "toppscorer": d["pred"].get("toppscorer") or "",
         "antall_maal": d["pred"].get("antall_maal"),
         "antall_kort": d["pred"].get("antall_kort")}
        for d in deltakere
    ]

    # Projisert sluttspill ut fra dagens gruppetabeller
    sluttspill = projiser_sluttspill(fact)

    # Sluttspill-tips: sammenlign hver deltakers tippede lag per runde.
    # R16 hentes fra bracket (sikre lag) når fasit ikke er satt, resten fra fasit direkte.
    sluttspill_runder = [
        ("r16", "16-delsfinale", 32),
        ("r8", "8-delsfinale", 16),
        ("kvart", "Kvartfinale", 8),
        ("semi", "Semifinale", 4),
        ("bronse", "Bronsefinale", 2),
        ("finale", "Finale", 2),
    ]
    sluttspill_tips = []
    for key, label, forventet in sluttspill_runder:
        fasit_lag = set()
        if fact.get(key):
            fasit_lag = {_norm(t) for t in fact[key] if t and str(t).strip()}
        if key == "r16" and sluttspill and sluttspill.get("runder"):
            r16_runde = sluttspill["runder"][0]
            for k in r16_runde.get("kamper", []):
                for side in ("home", "away"):
                    t = k.get(side)
                    if t and not t.get("placeholder") and t.get("sikker"):
                        fasit_lag.add(_norm(t["navn"]))
        if not fasit_lag:
            continue
        tips = []
        for d in deltakere:
            pred_key = key
            tippet = d["pred"].get(pred_key, [])
            if isinstance(tippet, str):
                tippet = [tippet] if tippet else []
            riktige = [t for t in tippet if _norm(t) in fasit_lag]
            tips.append({
                "navn": d["navn"],
                "riktige": len(riktige),
                "antall_bekreftet": len(fasit_lag),
                "forventet": forventet,
                "riktige_lag": riktige,
            })
        tips.sort(key=lambda x: x["riktige"], reverse=True)
        sluttspill_tips.append({"key": key, "label": label, "tips": tips})
    if sluttspill_tips:
        stats["sluttspill_tips"] = sluttspill_tips

    # Avansement-tips: for hvert lag, hvem har tippet det videre til hver runde.
    # Brukes av siden (badge + duell-grafikk på kommende kamper) og podcasten.
    avansement = {}  # _norm(lag) -> {"navn": <visningsnavn>, "runder": {key: [navn,...]}}
    runde_keys = ["r16", "r8", "kvart", "semi", "bronse", "finale"]
    for d in deltakere:
        for key in runde_keys:
            for lag in d["pred"].get(key, []) or []:
                if not lag or not str(lag).strip():
                    continue
                rec = avansement.setdefault(_norm(lag), {"navn": lag, "runder": {}})
                rec["runder"].setdefault(key, []).append(d["navn"])
        # vm_vinner (1 lag) som egen "runde" for fullstendighet
        v = d["pred"].get("vm_vinner")
        if v and str(v).strip():
            rec = avansement.setdefault(_norm(v), {"navn": v, "runder": {}})
            rec["runder"].setdefault("vm", []).append(d["navn"])
    if avansement:
        stats["avansement"] = avansement
        stats["antall_deltakere"] = len(deltakere)

    out = {
        "turnering": {"navn": cfg["navn"], "kort_navn": cfg["kort_navn"], "vert": cfg.get("vert", "")},
        "oppdatert": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "har_fasit": bool(fact),
        "fasit": fact,
        "stilling": [
            {"plass": d["plass"], "navn": d["navn"], "poeng": d["poeng"], "linjer": d["linjer"]}
            for d in deltakere
        ],
        "statistikk": stats,
        "projisert_sluttspill": sluttspill,
    }
    out_path = tdir / "data" / "stilling.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Skrev {out_path} ({len(deltakere)} deltakere)")

    oppdater_poenghistorikk(tdir, deltakere)


def oppdater_poenghistorikk(tdir: Path, deltakere: list):
    """Legg til et snapshot {tid, poeng} bare når poengene har endret seg siden
    forrige — så de hyppige 30-min-kjøringene ikke fyller fila med duplikater."""
    p = tdir / "data" / "poenghistorikk.json"
    hist = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    poeng = {d["navn"].strip(): d["poeng"] for d in deltakere}
    if hist and hist[-1].get("poeng") == poeng:
        return
    hist.append({"tid": datetime.now(timezone.utc).isoformat(timespec="seconds"), "poeng": poeng})
    p.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Poenghistorikk: {len(hist)} snapshots")


def _norm(s):
    return str(s).strip().lower() if s is not None else ""


def tippefordeling(deltakere, fact):
    """For hver gruppespillkamp: hvor mange tippet H/U/B, gruppert på dato."""
    kamper = fact.get("kamper") or []
    if not kamper or not deltakere:
        return None
    # Resultat (H/U/B) per kamp fra fasit.matches
    res_by_teams = {(_norm(m["home"]), _norm(m["away"])): _norm(m["result"]).upper()
                    for m in fact.get("matches", [])}
    # Datoer i kronologisk rekkefølge for gruppespillkamper
    kamp_meta = {(_norm(k["home"]), _norm(k["away"])): k for k in kamper}

    rader = {}      # key (home,away) -> teller
    navn_pick = {}  # key (home,away) -> {H/U/B: [navn]}
    for d in deltakere:
        for m in d["pred"]["matches"]:
            key = (_norm(m["home"]), _norm(m["away"]))
            if key not in kamp_meta:
                continue
            r = rader.setdefault(key, {"H": 0, "U": 0, "B": 0})
            np_ = navn_pick.setdefault(key, {"H": [], "U": [], "B": []})
            pick = (m["pick"] or "").strip().upper()
            if pick in r:
                r[pick] += 1
                np_[pick].append(d["navn"])

    dager = {}
    for key, teller in rader.items():
        k = kamp_meta[key]
        dato = k.get("dato", "")
        ferdig = k.get("status") == "FINISHED"
        fasit = res_by_teams.get(key) if ferdig else None
        dager.setdefault(dato, []).append({
            "home": k["home"], "away": k["away"],
            "group": (k.get("group") or "").replace("GROUP_", ""),
            "H": teller["H"], "U": teller["U"], "B": teller["B"],
            "fasit": fasit,
            # Hvem tippet hva (navn per utfall). Siden viser dette på uspilte kamper,
            # og "hvem tok riktig" (navn[fasit]) når kampen er ferdig.
            "navn": navn_pick[key],
        })

    antall = len(deltakere)
    out = []
    for dato in sorted(d for d in dager if d):
        out.append({"dato": dato, "kamper": dager[dato]})
    return {"antall_deltakere": antall, "dager": out}


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")