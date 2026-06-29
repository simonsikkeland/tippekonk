"""
Projisert sluttspill for VM 2026 ut fra dagens gruppetabeller.

Seeder 16-delsfinalen (32 lag: 1.- og 2.-plass + de 8 beste treerne),
og simulerer hele veien til en projisert verdensmester ved å la det
"sterkeste" laget gå videre i hver kamp.

Styrke = poeng/kamp, så målforskjell/kamp, så mål/kamp (rettferdig når
gruppene har spilt ulikt antall kamper midt i mesterskapet). Uavgjort
styrke brytes på lagnavn (deterministisk).

Treer-allokeringen beregnes som en bipartitt matching mot FIFAs tillatte
grupper per slot. I sjeldne tvetydige kombinasjoner kan den avvike fra
FIFAs offisielle Annex C-tabell.
"""
from __future__ import annotations

from engine import grupper_ferdig, _norm

GROUPS = list("ABCDEFGHIJKL")

# Treer-slot (kampnummer) -> tillatte grupper laget kan komme fra.
THIRD_SLOTS = {
    74: "ABCDF", 77: "CDFGH", 79: "CEFHI", 80: "EHIJK",
    81: "BEFIJ", 82: "AEHIJ", 85: "EFGIJ", 87: "DEIJL",
}

# 16-delsfinalen: kampnr -> (hjemme-spec, borte-spec).
# spec ("1","E")=vinner gr. E, ("2","C")=toer gr. C, ("3",74)=treer i slot 74.
R32 = {
    73: (("2", "A"), ("2", "B")),
    74: (("1", "E"), ("3", 74)),
    75: (("1", "F"), ("2", "C")),
    76: (("1", "C"), ("2", "F")),
    77: (("1", "I"), ("3", 77)),
    78: (("2", "E"), ("2", "I")),
    79: (("1", "A"), ("3", 79)),
    80: (("1", "L"), ("3", 80)),
    81: (("1", "D"), ("3", 81)),
    82: (("1", "G"), ("3", 82)),
    83: (("2", "K"), ("2", "L")),
    84: (("1", "H"), ("2", "J")),
    85: (("1", "B"), ("3", 85)),
    86: (("1", "J"), ("2", "H")),
    87: (("1", "K"), ("3", 87)),
    88: (("2", "D"), ("2", "G")),
}

# Senere runder: kampnr -> (vinner av kamp X, vinner av kamp Y).
LATER = {
    89: (74, 77), 90: (73, 75), 91: (76, 78), 92: (79, 80),
    93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87),
    97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96),
    101: (97, 98), 102: (99, 100),
    104: (101, 102),
}

RUNDER = [
    ("16-delsfinale", range(73, 89)),
    ("8-delsfinale", range(89, 97)),
    ("Kvartfinale", range(97, 101)),
    ("Semifinale", range(101, 103)),
    ("Bronsefinale", [103]),
    ("Finale", [104]),
]


def _styrke(t: dict) -> tuple:
    sp = t.get("spilt") or 0
    if sp <= 0:
        return (0.0, 0.0, 0.0)
    return (t["poeng"] / sp, t["mf"] / sp, t["maal_for"] / sp)


def _match_treere(kvalifiserte: set) -> dict:
    """Tildel de 8 kvalifiserte treer-gruppene til de 8 slotene (bipartitt
    matching). Returnerer {slot: gruppebokstav} eller {} hvis umulig."""
    slots = [74, 77, 79, 80, 81, 82, 85, 87]
    assign, brukt = {}, set()

    def bt(i):
        if i == len(slots):
            return True
        s = slots[i]
        for g in sorted(THIRD_SLOTS[s]):
            if g in kvalifiserte and g not in brukt:
                brukt.add(g); assign[s] = g
                if bt(i + 1):
                    return True
                brukt.discard(g); del assign[s]
        return False

    return assign if bt(0) else {}


def projiser_sluttspill(fact: dict) -> dict | None:
    """Bygg projisert brakett fra fact['grupper']. None hvis ingen tabeller."""
    grupper = fact.get("grupper") or {}
    if not grupper:
        return None
    gf = grupper_ferdig(fact)

    # Gruppebokstav -> ordnet lagliste (allerede sortert i fetch_results)
    by_letter = {}
    for label, rows in grupper.items():
        if rows:
            by_letter[label.split()[-1]] = rows

    def seeded(rows, pos, G):
        if len(rows) <= pos:
            return None
        t = rows[pos]
        return {"navn": t["lag"], "seed": f"{pos + 1}{G}", "_s": _styrke(t)}

    pos1 = {G: seeded(r, 0, G) for G, r in by_letter.items()}
    pos2 = {G: seeded(r, 1, G) for G, r in by_letter.items()}
    pos3 = {G: seeded(r, 2, G) for G, r in by_letter.items()}

    # Faktiske 16-delsfinale-lag fra API-et (når de er satt). Hver satte
    # LAST_32-kamp kartlegges til riktig kampnr via seedingen (1A/2B/3X), så
    # braketten matcher den offisielle fordelingen og oppdateres automatisk.
    name2seed, strength_by_name = {}, {}
    for G, rows in by_letter.items():
        for i, row in enumerate(rows[:3]):
            name2seed[_norm(row["lag"])] = f"{i + 1}{G}"
            strength_by_name[_norm(row["lag"])] = _styrke(row)

    def _spec_seed_match(spec, seed):
        if not seed:
            return False
        typ, key = spec
        if typ in ("1", "2"):
            return seed == f"{typ}{key}"
        return len(seed) >= 2 and seed[0] == "3" and seed[1] in THIRD_SLOTS[key]

    def _finn_r32_nr(sh, sa):
        for nr, (sA, sB) in R32.items():
            if (_spec_seed_match(sA, sh) and _spec_seed_match(sB, sa)) or \
               (_spec_seed_match(sA, sa) and _spec_seed_match(sB, sh)):
                return nr
        return None

    actual_r32 = {}  # nr -> {"home": teamobj, "away": teamobj}
    for k in fact.get("kamper", []):
        if k.get("stage") not in ("LAST_32", "ROUND_OF_32"):
            continue
        h, a = k.get("home"), k.get("away")
        if not h or not a:
            continue
        sh, sa = name2seed.get(_norm(h)), name2seed.get(_norm(a))
        nr = _finn_r32_nr(sh, sa)
        if not nr:
            continue
        actual_r32[nr] = {
            "home": {"navn": h, "seed": sh or "?", "_s": strength_by_name.get(_norm(h), (0, 0, 0)), "sikker": True},
            "away": {"navn": a, "seed": sa or "?", "_s": strength_by_name.get(_norm(a), (0, 0, 0)), "sikker": True},
        }

    def resolve(spec):
        """Projisert lag for et 16-dels-slot når kampen ikke er satt enda.
        1./2.-plass: konkret lag (grønn hvis gruppa er ferdig). 3.-plass: en
        plassholder med tillatte grupper (som Wikipedia) — vi gjetter ikke."""
        typ, key = spec
        if typ in ("1", "2"):
            t = (pos1 if typ == "1" else pos2).get(key)
            return {**t, "sikker": bool(gf.get("Gruppe " + key))} if t else None
        return {"navn": "Nr. 3 gr. " + "/".join(THIRD_SLOTS[key]), "seed": "3",
                "placeholder": True, "_s": (0, 0, 0)}

    def sterkest(a, b):
        if a is None or b is None:
            return None
        ka, kb = (a["_s"], a["navn"]), (b["_s"], b["navn"])
        w = a if ka >= kb else b
        # En PROJISERT vinner skal aldri arve "faktisk" fra en tidligere runde —
        # ellers vises et lag som faktisk videre i en runde det ikke har spilt.
        return {k: v for k, v in w.items() if k != "faktisk"} if w.get("faktisk") else w

    # Faktiske sluttspillresultater (lag mot lag -> vinnernavn), så en vinner
    # populeres videre i braketten med en gang kampen er endelig — uavhengig av
    # om API-et har trukket neste fixture ennå. Nøkkel er settet av lagnavn.
    ko_resultat = {}
    for k in fact.get("kamper", []):
        if k.get("stage") in (None, "GROUP_STAGE") or k.get("status") != "FINISHED":
            continue
        h, a, w = k.get("home"), k.get("away"), k.get("winner")
        if not h or not a:
            continue
        wn = h if w == "HOME_TEAM" else (a if w == "AWAY_TEAM" else None)
        if not wn:
            hs, as_ = k.get("home_score"), k.get("away_score")
            if hs is not None and as_ is not None and hs != as_:
                wn = h if hs > as_ else a
        if wn:
            ko_resultat[frozenset((_norm(h), _norm(a)))] = wn

    def faktisk_vinner(h, b):
        """Faktisk vinner (markert sikker+faktisk) hvis kampen mellom h og b er
        ferdigspilt, ellers None."""
        if not h or not b or h.get("placeholder") or b.get("placeholder"):
            return None
        wn = ko_resultat.get(frozenset((_norm(h["navn"]), _norm(b["navn"]))))
        if not wn:
            return None
        w = h if _norm(h["navn"]) == _norm(wn) else b
        return {**w, "sikker": True, "faktisk": True}

    def _taper(v, h, b):
        if not v:
            return None
        loser = None
        if h and _norm(v["navn"]) == _norm(h.get("navn", "")):
            loser = b
        elif b and _norm(v["navn"]) == _norm(b.get("navn", "")):
            loser = h
        if loser is None:
            return None
        # Når vinneren er faktisk, er taperen også endelig kjent.
        if v.get("faktisk") and not loser.get("placeholder"):
            return {**loser, "sikker": True, "faktisk": True}
        return loser

    # Resolve 16-delsfinalen: faktiske lag der de er satt, ellers projeksjon.
    kamper = {}  # nr -> {home, away, vinner, taper}
    for nr in range(73, 89):
        if nr in actual_r32:
            h, b = actual_r32[nr]["home"], actual_r32[nr]["away"]
        else:
            h, b = resolve(R32[nr][0]), resolve(R32[nr][1])
        kamper[nr] = {"home": h, "away": b, "vinner": faktisk_vinner(h, b) or sterkest(h, b)}
    for nr in (89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102):
        a, c = LATER[nr]
        h, b = kamper[a]["vinner"], kamper[c]["vinner"]
        v = faktisk_vinner(h, b) or sterkest(h, b)
        kamper[nr] = {"home": h, "away": b, "vinner": v, "taper": _taper(v, h, b)}
    # Bronsefinale: tapere av semifinalene
    bh, bb = kamper[101].get("taper"), kamper[102].get("taper")
    kamper[103] = {"home": bh, "away": bb, "vinner": faktisk_vinner(bh, bb) or sterkest(bh, bb)}
    # Finale
    fh, fb = kamper[101]["vinner"], kamper[102]["vinner"]
    kamper[104] = {"home": fh, "away": fb, "vinner": faktisk_vinner(fh, fb) or sterkest(fh, fb)}

    def clean(t):
        if not t:
            return None
        out = {"navn": t["navn"], "seed": t.get("seed", "")}
        if t.get("sikker"):
            out["sikker"] = True
        if t.get("faktisk"):
            out["faktisk"] = True
        if t.get("placeholder"):
            out["placeholder"] = True
        return out

    runder = []
    for navn, nrs in RUNDER:
        kamp_liste = []
        for nr in nrs:
            obj = {"nr": nr, "home": clean(kamper[nr]["home"]),
                   "away": clean(kamper[nr]["away"]), "vinner": clean(kamper[nr]["vinner"])}
            if kamper[nr].get("taper"):
                obj["taper"] = clean(kamper[nr]["taper"])
            kamp_liste.append(obj)
        runder.append({"navn": navn, "kamper": kamp_liste})

    return {
        "mester": clean(kamper[104]["vinner"]),
        "bronse": clean(kamper[103]["vinner"]),
        "runder": runder,
    }
