"""
Verktøy for å sjekke at poenggivningen er sunn og stabil.

Kjør:  python scripts/verify.py tournaments/vm-2026

Rapporterer:
  1. Poeng per deltaker med full linjeoppdeling (samme som siden viser).
  2. Navnefeil — lagnavn i tippene som IKKE matcher noe i fasit. Disse gir
     stille 0 poeng i dag (en feilstavet gruppevinner/sluttspill-tip teller bare
     ikke, uten feilmelding).
  3. Monotoni — sammenligner dagens poeng mot forrige snapshot i
     poenghistorikk.json og flagger eventuelle FALL.
  4. Fasit-sanity — antall ferdige kamper, ferdige kamper uten resultat, og om
     antall_maal stemmer med summen av målene.

Avslutter med kode 1 hvis noe ser galt ut (kan brukes til å stoppe CI senere).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

# Windows-konsollen er ofte cp1252; tving UTF-8 så æøå/symboler ikke krasjer.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import parse_sheet, parse_master, score, _norm  # noqa: E402
from build_standings import navn_fra_filnavn  # noqa: E402


def last_predictions(tdir: Path) -> list[dict]:
    """Hent {navn, pred} for alle deltakere — samme kilde som build_standings."""
    ark_dir = tdir / "ark"
    master = ark_dir / "MASTER.xlsx"
    ut = []
    if master.exists():
        for e in parse_master(master):
            ut.append({"navn": e["navn"], "pred": e["pred"]})
    else:
        sheets = sorted(ark_dir.glob("*.xlsx")) + sorted(ark_dir.glob("*.xls"))
        for sp in sheets:
            try:
                ut.append({"navn": navn_fra_filnavn(sp), "pred": parse_sheet(sp)})
            except Exception as e:  # noqa: BLE001
                print(f"  ! kunne ikke lese {sp.name}: {e}")
    return ut


def kjente_lag(fact: dict) -> set[str]:
    """Alle lagnavn som finnes i fasit (kanonisk fasit-stavemåte)."""
    s: set[str] = set()
    for m in fact.get("matches", []):
        s.add(_norm(m.get("home"))); s.add(_norm(m.get("away")))
    for k in fact.get("kamper", []):
        s.add(_norm(k.get("home"))); s.add(_norm(k.get("away")))
    for lst in ("r16", "r8", "kvart", "semi", "bronse", "finale"):
        for t in fact.get(lst, []):
            s.add(_norm(t))
    for t in fact.get("group_winners", {}).values():
        s.add(_norm(t))
    for grp in fact.get("grupper", {}).values():
        for row in grp:
            s.add(_norm(row.get("lag", "")))
    for t in fact.get("flagg", {}):
        s.add(_norm(t))
    s.discard("")
    return s


def main(tournament_dir: str) -> int:
    tdir = Path(tournament_dir)
    cfg = json.loads((tdir / "tournament.json").read_text(encoding="utf-8"))
    rules = cfg["regler"]
    fact = json.loads((tdir / "data" / "fasit.json").read_text(encoding="utf-8"))
    deltakere = last_predictions(tdir)
    problemer = 0

    # --- 1. Poeng per deltaker ---
    print("== Poeng per deltaker ==")
    scoret = []
    for d in deltakere:
        sc = score(d["pred"], fact, rules)
        scoret.append({"navn": d["navn"], "pred": d["pred"], "poeng": sc["total"], "linjer": sc["lines"]})
    scoret.sort(key=lambda x: x["poeng"], reverse=True)
    for d in scoret:
        sum_linjer = sum(l["pts"] for l in d["linjer"])
        flagg = "" if sum_linjer == d["poeng"] else f"  !! sum linjer={sum_linjer}"
        print(f'  {d["navn"]:12} {d["poeng"]:3}p{flagg}')
        if flagg:
            problemer += 1

    # --- 2. Navnefeil i tipp (stille nuller) ---
    print("\n== Navnefeil i tipp (matcher ikke fasit) ==")
    kjent = kjente_lag(fact)
    if not kjent:
        print("  (ingen fasit-lag ennå — hopper over)")
    else:
        funnet = False
        felt = [("group_winners", "gruppevinner"), ("r16", "16-del"), ("r8", "8-del"),
                ("kvart", "kvart"), ("semi", "semi"), ("bronse", "bronse"),
                ("finale", "finale"), ("vm_vinner", "vinner"), ("bronse_vinner", "bronsevinner")]
        for d in deltakere:
            avvik = []
            for key, label in felt:
                v = d["pred"].get(key)
                verdier = list(v.values()) if isinstance(v, dict) else (v if isinstance(v, list) else [v])
                for navn in verdier:
                    if navn and _norm(navn) not in kjent:
                        avvik.append(f"{label}:{navn}")
            if avvik:
                funnet = True
                print(f'  {d["navn"]:12} {", ".join(avvik)}')
        if not funnet:
            print("  ingen — alle lagnavn matcher fasit ✓")

    # --- 3. Monotoni mot forrige snapshot ---
    print("\n== Monotoni (fall i poeng siden forrige snapshot) ==")
    hist_path = tdir / "data" / "poenghistorikk.json"
    hist = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    if hist:
        forrige = hist[-1].get("poeng", {})
        naa = {d["navn"].strip(): d["poeng"] for d in scoret}
        fall = False
        for navn, p in naa.items():
            f = forrige.get(navn)
            if f is not None and p < f:
                print(f"  ! {navn}: {f} -> {p} (FALL)")
                fall = True
                problemer += 1
        if not fall:
            print("  ingen fall ✓")
    else:
        print("  (ingen historikk ennå)")

    # --- 4. Fasit-sanity ---
    print("\n== Fasit-sanity ==")
    kamper = fact.get("kamper", [])
    ferdige = [k for k in kamper if k.get("status") == "FINISHED"]
    null_res = [k for k in ferdige if k.get("home_score") is None or k.get("away_score") is None]
    sum_maal = sum((k.get("home_score") or 0) + (k.get("away_score") or 0)
                   for k in ferdige if k.get("home_score") is not None)
    print(f"  kamper: {len(ferdige)}/{len(kamper)} ferdige")
    print(f"  H/U/B registrert: {len(fact.get('matches', []))}")
    print(f"  turnering_ferdig: {fact.get('turnering_ferdig')}")
    print(f"  toppscorer: {fact.get('toppscorer') or '-'}")
    if null_res:
        print(f"  ! {len(null_res)} ferdige kamper uten resultat:")
        for k in null_res:
            print(f"      {k.get('home')} - {k.get('away')} ({k.get('dato')})")
        problemer += 1
    if sum_maal != fact.get("antall_maal"):
        print(f"  ! antall_maal={fact.get('antall_maal')} men sum av ferdige kamper={sum_maal}"
              f" (kan være ok hvis monoton vakt holdt et høyere tall)")

    print(f"\n{'OK ✓' if problemer == 0 else f'{problemer} problem(er) funnet'}")
    return 1 if problemer else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026"))
