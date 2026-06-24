"""
Henter kort-statistikk (gule/røde) for en turnering fra myfootballfacts.com
og bygger en strukturert `kort`-blokk til fasit.json.

Siden er statisk HTML (ingen JS), men krever browser-aktige headere ellers
svarer den 403. Vi parser med ren stdlib (re) — ingen pandas/lxml-avhengighet.

URL settes per turnering i tournament.json som "kort_url".

Stabilitet: kort kan bare øke. Hvis hentingen feiler eller gir et lavere
totaltall enn forrige, beholdes den eksisterende `kort`-blokken (aldri null).
"""
from __future__ import annotations
import re
import urllib.request
from datetime import datetime, timezone

_HDRS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
    "Referer": "https://no.myfootballfacts.com/",
}


def _txt(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ").strip()


def _parse_table(table_html: str) -> tuple[str, list[dict]]:
    """Returner (header-tekst-lowercased, [{spiller, lag, antall}]) for én tabell."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)
    header, data = "", []
    for i, row in enumerate(rows):
        celler = [_txt(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        celler = [c for c in celler if c]
        if not celler:
            continue
        if i == 0 or not header:
            header = " ".join(celler).lower()
            continue
        # Forvent: [spiller, lag, antall]. Tallet = siste rene heltall i raden.
        antall = next((int(c) for c in reversed(celler) if re.fullmatch(r"\d+", c)), None)
        if antall is None:
            continue
        data.append({
            "spiller": celler[0],
            "lag": celler[1].title() if len(celler) > 1 else "",
            "antall": antall,
        })
    return header, data


def _hent_html(url: str) -> str:
    req = urllib.request.Request(url, headers=_HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def hent_kort(url: str, existing: dict | None = None) -> dict | None:
    """Hent og bygg kort-blokken. Returnerer None hvis ingen URL.
    Ved feil/degradering beholdes `existing` (kan også være None)."""
    if not url:
        return existing
    forrige = (existing or {}).get("kort")
    try:
        html = _hent_html(url)
    except Exception as e:  # noqa: BLE001
        print(f"  (kort hoppet over, beholder forrige: {e})")
        return forrige

    gule, rode, gult_rodt = [], [], []
    for t in re.findall(r"<table.*?</table>", html, re.S | re.I):
        header, data = _parse_table(t)
        if "gult-r" in header or "gult/r" in header:
            gult_rodt = data
        elif "gult" in header or "gul" in header:
            gule = data
        elif "rødt" in header or "rodt" in header or "rød" in header:
            rode = data

    sum_gule = sum(d["antall"] for d in gule)
    sum_rode = sum(d["antall"] for d in rode)
    sum_gult_rodt = sum(d["antall"] for d in gult_rodt)
    total = sum_gule + sum_rode

    kort = {
        "gule": gule,
        "rode": rode,
        "gult_rodt": gult_rodt,
        "sum_gule": sum_gule,
        "sum_rode": sum_rode,
        "sum_gult_rodt": sum_gult_rodt,
        "total": total,
        "hentet": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kilde": url,
    }

    # Degraderingsvakt: kort kan bare øke. Lavere total = ufullstendig svar.
    if forrige and total < (forrige.get("total") or 0):
        print(f"  ADVARSEL: kort-total falt ({forrige.get('total')} -> {total}) — beholder forrige.")
        return forrige

    print(f"  Kort: {sum_gule} gule, {sum_rode} røde (total {total})")
    return kort


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path
    tdir = Path(sys.argv[1] if len(sys.argv) > 1 else "tournaments/vm-2026")
    cfg = json.loads((tdir / "tournament.json").read_text(encoding="utf-8"))
    res = hent_kort(cfg.get("kort_url", ""))
    print(json.dumps(res, ensure_ascii=False, indent=2) if res else "ingen kort_url")
