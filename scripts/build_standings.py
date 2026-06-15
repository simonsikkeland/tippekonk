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

    # "Nærmer seg"-statistikk for bonusfeltene
    stats = {}
    if fact.get("antall_maal") is not None:
        stats["antall_maal"] = {
            "fasit": fact["antall_maal"],
            "tipp": sorted(
                [{"navn": d["navn"], "tipp": d["pred"]["antall_maal"],
                  "diff": abs((d["pred"]["antall_maal"] or 0) - fact["antall_maal"])}
                 for d in deltakere if d["pred"]["antall_maal"] is not None],
                key=lambda x: x["diff"]),
        }
    if fact.get("antall_kort") is not None:
        stats["antall_kort"] = {
            "fasit": fact["antall_kort"],
            "tipp": sorted(
                [{"navn": d["navn"], "tipp": d["pred"]["antall_kort"],
                  "diff": abs((d["pred"]["antall_kort"] or 0) - fact["antall_kort"])}
                 for d in deltakere if d["pred"]["antall_kort"] is not None],
                key=lambda x: x["diff"]),
        }
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
    }
    out_path = tdir / "data" / "stilling.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Skrev {out_path} ({len(deltakere)} deltakere)")


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
            # Hvem tippet riktig utfall (kun ferdigspilte kamper)
            "riktig": navn_pick[key].get(fasit, []) if fasit else None,
        })

    antall = len(deltakere)
    out = []
    for dato in sorted(d for d in dager if d):
        out.append({"dato": dato, "kamper": dager[dato]})
    return {"antall_deltakere": antall, "dager": out}


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")