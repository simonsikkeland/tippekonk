"""
Engangs-backfill av poenghistorikk fra git-historikken til stilling.json.

Går gjennom hver commit som rørte stilling.json, henter innholdet
(`git show <commit>:<path>`), trekker ut {navn: poeng} + commit-dato, og
skriver tournaments/<t>/data/poenghistorikk.json. Hopper over commits der
JSON-en ikke kan parses (f.eks. en kortvarig merge-konflikt).

Etterpå holder build_standings.py historikken vedlike (appender ved endring).

Bruk: python scripts/backfill_poenghistorikk.py [tournaments/vm-2026]
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8").stdout


def _poeng_av(tekst: str) -> dict | None:
    try:
        d = json.loads(tekst)
    except (json.JSONDecodeError, ValueError):
        return None
    rader = d.get("stilling")
    if not rader:
        return None
    return {r["navn"].strip(): r["poeng"] for r in rader if r.get("navn")}


def main(tournament_dir: str):
    tdir = Path(tournament_dir)
    rel = (tdir / "data" / "stilling.json").as_posix()

    # Eldste -> nyeste: commit-hash + ISO-dato
    lines = _git("log", "--reverse", "--format=%H %cI", "--", rel).splitlines()
    historikk = []
    forrige = None
    for line in lines:
        if not line.strip():
            continue
        sha, tid = line.split(" ", 1)
        innhold = _git("show", f"{sha}:{rel}")
        poeng = _poeng_av(innhold)
        if poeng is None or poeng == forrige:
            continue          # ugyldig snapshot, eller ingen endring
        historikk.append({"tid": tid.strip(), "poeng": poeng})
        forrige = poeng

    out = tdir / "data" / "poenghistorikk.json"
    out.write_text(json.dumps(historikk, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Skrev {out} ({len(historikk)} snapshots, {len(lines)} commits gjennomgått)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")
