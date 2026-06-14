"""
Bygger den statiske nettsiden til site/dist for GitHub Pages.
Oppdager alle turneringer, kopierer deres data + ev. podcast, og
lager en manifest.json som siden bruker til å vise en velger.

Bruk: python scripts/build_site.py
"""
from __future__ import annotations
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DIST = SITE / "dist"


def main():
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # Kopier statiske filer (index.html, app.js, style.css)
    for f in SITE.glob("*.*"):
        if f.is_file():
            shutil.copy(f, DIST / f.name)

    manifest = []
    for tdir in sorted((ROOT / "tournaments").glob("*/")):
        cfgp = tdir / "tournament.json"
        if not cfgp.exists():
            continue
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
        tid = cfg["id"]
        out_dir = DIST / tid
        out_dir.mkdir(parents=True, exist_ok=True)
        stilling = tdir / "data" / "stilling.json"
        if stilling.exists():
            shutil.copy(stilling, out_dir / "stilling.json")
        # podcast-mappe hvis den finnes
        pod = tdir / "data" / "podcast"
        if pod.exists():
            shutil.copytree(pod, out_dir / "podcast", dirs_exist_ok=True)
            feed = pod / "feed.xml"
            if feed.exists():
                shutil.copy(feed, out_dir / "feed.xml")
        manifest.append({
            "id": tid, "navn": cfg["navn"], "kort_navn": cfg["kort_navn"],
            "start": cfg.get("start_dato", ""), "har_stilling": stilling.exists(),
        })

    (DIST / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Bygde site/dist med {len(manifest)} turnering(er)")


if __name__ == "__main__":
    main()