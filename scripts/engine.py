"""
Felles parser + poengmotor for Piggy's tippekonkurranser.
Brukes på tvers av alle turneringer (VM 2026, EM 2028, ...).

Leser et utfylt "Excely"-ark (arket "Resultater") og trekker ut alle
prediksjoner. Regner poeng mot en fasit av samme form. Ren stdlib +
openpyxl — ingen tunge avhengigheter.
"""
from __future__ import annotations
import json
from pathlib import Path
from openpyxl import load_workbook

# Faste celleadresser i "Resultater"-arket (1-indeksert: rad, kolonne).
# Disse er stabile i Excely-malen og deles av alle turneringer som bruker den.
LAYOUT = {
    "match_rows": (4, 75), "col_kamp": 2, "col_home": 3, "col_away": 4, "col_result": 5,
    "gw_rows": (78, 89), "col_gw_label": 4, "col_gw_team": 5,
    "r16_rows": (92, 123), "r8_rows": (126, 141), "kvart_rows": (144, 151),
    "semi_rows": (154, 157), "bronse_rows": (160, 161), "bronse_vinner_row": 162,
    "finale_rows": (165, 166), "vm_vinner_row": 167, "col_ko_team": 5,
    "bonus_toppscorer": (26, 11), "bonus_assist": (27, 11),
    "bonus_maal": (28, 11), "bonus_kort": (29, 11),
}


def _norm(s) -> str:
    return str(s).strip().lower() if s is not None else ""


def _col_block(ws, rows, col):
    out = []
    for r in range(rows[0], rows[1] + 1):
        v = ws.cell(r, col).value
        if v is not None and str(v).strip():
            out.append(str(v).strip())
    return out


def parse_sheet(path: str | Path) -> dict:
    """Trekk ut alle prediksjoner fra ett utfylt ark."""
    wb = load_workbook(path, data_only=True)
    if "Resultater" not in wb.sheetnames:
        raise ValueError(f'{Path(path).name}: fant ikke arket "Resultater"')
    ws = wb["Resultater"]
    L = LAYOUT

    matches = []
    for r in range(L["match_rows"][0], L["match_rows"][1] + 1):
        n = ws.cell(r, L["col_kamp"]).value
        if n is None:
            continue
        matches.append({
            "n": int(n),
            "home": str(ws.cell(r, L["col_home"]).value or "").strip(),
            "away": str(ws.cell(r, L["col_away"]).value or "").strip(),
            "pick": str(ws.cell(r, L["col_result"]).value or "").strip().upper(),
        })

    gw = {}
    for r in range(L["gw_rows"][0], L["gw_rows"][1] + 1):
        label = ws.cell(r, L["col_gw_label"]).value
        team = ws.cell(r, L["col_gw_team"]).value
        if label and team:
            gw[str(label).strip()] = str(team).strip()

    def cell(rc):
        return ws.cell(rc[0], rc[1]).value

    maal = cell(L["bonus_maal"])
    kort = cell(L["bonus_kort"])
    return {
        "matches": matches,
        "group_winners": gw,
        "r16": _col_block(ws, L["r16_rows"], L["col_ko_team"]),
        "r8": _col_block(ws, L["r8_rows"], L["col_ko_team"]),
        "kvart": _col_block(ws, L["kvart_rows"], L["col_ko_team"]),
        "semi": _col_block(ws, L["semi_rows"], L["col_ko_team"]),
        "bronse": _col_block(ws, L["bronse_rows"], L["col_ko_team"]),
        "bronse_vinner": str(cell((L["bronse_vinner_row"], L["col_ko_team"])) or "").strip(),
        "finale": _col_block(ws, L["finale_rows"], L["col_ko_team"]),
        "vm_vinner": str(cell((L["vm_vinner_row"], L["col_ko_team"])) or "").strip(),
        "toppscorer": str(cell(L["bonus_toppscorer"]) or "").strip(),
        "assist": str(cell(L["bonus_assist"]) or "").strip(),
        "antall_maal": int(maal) if maal not in (None, "") else None,
        "antall_kort": int(kort) if kort not in (None, "") else None,
    }


def _count_set(preds, fact_set):
    hits, used = 0, set()
    for p in preds:
        k = _norm(p)
        if k in fact_set and k not in used:
            hits += 1
            used.add(k)
    return hits


def score(pred: dict, fact: dict, rules: dict) -> dict:
    """Regn poeng for én prediksjon mot fasit. `rules` = poengverdier."""
    lines, total = [], 0

    def add(label, pts, detail):
        nonlocal total
        lines.append({"label": label, "pts": pts, "detail": detail})
        total += pts

    # Gruppespill (H/U/B). Fasit-kamper bruker feltet "result".
    if fact.get("matches"):
        fact_by_n = {m["n"]: _norm(m.get("result")) for m in fact["matches"]}
        correct = sum(
            1 for m in pred["matches"]
            if fact_by_n.get(m["n"]) and _norm(m["pick"]) == fact_by_n[m["n"]]
        )
        add(f"Gruppespill ({correct} riktige)", correct * rules["kamp"], f'{correct} x {rules["kamp"]}p')

    if fact.get("group_winners"):
        correct = sum(
            1 for g, t in pred["group_winners"].items()
            if fact["group_winners"].get(g) and _norm(fact["group_winners"][g]) == _norm(t)
        )
        add(f"Gruppevinnere ({correct} riktige)", correct * rules["gruppevinner"], f'{correct} x {rules["gruppevinner"]}p')

    rounds = [
        ("16-dels finale", "r16", "r16"), ("8-dels finale", "r8", "r8"),
        ("Kvartfinale", "kvart", "kvart"), ("Semifinale", "semi", "semi"),
        ("Bronsefinale (lag)", "bronse", "bronse_lag"), ("Finale (lag)", "finale", "finale_lag"),
    ]
    for label, key, rule_key in rounds:
        if not fact.get(key):
            continue
        hits = _count_set(pred.get(key, []), {_norm(x) for x in fact[key]})
        add(f"{label} ({hits} riktige)", hits * rules[rule_key], f'{hits} x {rules[rule_key]}p')

    if fact.get("bronse_vinner"):
        ok = _norm(pred.get("bronse_vinner")) == _norm(fact["bronse_vinner"])
        add("Bronsevinner", rules["bronse_vinner"] if ok else 0, "riktig" if ok else f'tippet {pred.get("bronse_vinner") or "-"}')
    if fact.get("vm_vinner"):
        ok = _norm(pred.get("vm_vinner")) == _norm(fact["vm_vinner"])
        add("Vinner", rules["vm_vinner"] if ok else 0, "riktig" if ok else f'tippet {pred.get("vm_vinner") or "-"}')

    for fkey, rule_key, label in [
        ("toppscorer", "toppscorer", "Toppscorer"),
        ("assist", "flest_assist", "Flest assist"),
    ]:
        if fact.get(fkey):
            ok = _norm(pred.get(fkey)) == _norm(fact[fkey])
            add(label, rules[rule_key] if ok else 0, "riktig" if ok else f'tippet {pred.get(fkey) or "-"}')

    for fkey, rule_key, label in [
        ("antall_maal", "antall_maal", "Antall mål"),
        ("antall_kort", "antall_kort", "Antall kort"),
    ]:
        if fact.get(fkey) is not None:
            ok = pred.get(fkey) == fact[fkey]
            add(label, rules[rule_key] if ok else 0, "riktig" if ok else f'tippet {pred.get(fkey)} (fasit {fact[fkey]})')

    return {"total": total, "lines": lines}


# Standard poengregler (fra "Resultater"-arket). Overstyres per turnering
# via tournament.json hvis reglene endres for et framtidig mesterskap.
DEFAULT_RULES = {
    "kamp": 1, "gruppevinner": 3, "r16": 1, "r8": 2, "kvart": 3, "semi": 4,
    "bronse_lag": 1, "bronse_vinner": 3, "finale_lag": 5, "vm_vinner": 10,
    "toppscorer": 10, "flest_assist": 7, "antall_maal": 20, "antall_kort": 20,
}