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
from engine import parse_sheet, score  # noqa: E402


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
    sheets = sorted(ark_dir.glob("*.xlsx")) + sorted(ark_dir.glob("*.xls"))

    deltakere = []
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


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")