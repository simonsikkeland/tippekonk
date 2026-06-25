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

from engine import grupper_ferdig

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

    # Rangér treerne, ta de 8 beste
    treere = [(G, t) for G, t in pos3.items() if t]
    treere.sort(key=lambda x: (x[1]["_s"], x[1]["navn"]), reverse=True)
    kvalifiserte = {G for G, _ in treere[:8]}
    assign = _match_treere(kvalifiserte)

    def team(spec):
        typ, key = spec
        if typ == "1":
            return pos1.get(key)
        if typ == "2":
            return pos2.get(key)
        g = assign.get(key)  # treer
        return pos3.get(g) if g else None

    def sterkest(a, b):
        if a is None or b is None:
            return None
        ka, kb = (a["_s"], a["navn"]), (b["_s"], b["navn"])
        return a if ka >= kb else b

    # Resolve + simuler i kampnummer-rekkefølge
    kamper = {}  # nr -> {home, away, vinner, taper}
    for nr in range(73, 89):
        h, b = team(R32[nr][0]), team(R32[nr][1])
        kamper[nr] = {"home": h, "away": b, "vinner": sterkest(h, b)}
    for nr in (89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102):
        a, c = LATER[nr]
        h, b = kamper[a]["vinner"], kamper[c]["vinner"]
        v = sterkest(h, b)
        taper = (b if v is h else h) if v else None
        kamper[nr] = {"home": h, "away": b, "vinner": v, "taper": taper}
    # Bronsefinale: tapere av semifinalene
    bh, bb = kamper[101].get("taper"), kamper[102].get("taper")
    kamper[103] = {"home": bh, "away": bb, "vinner": sterkest(bh, bb)}
    # Finale
    fh, fb = kamper[101]["vinner"], kamper[102]["vinner"]
    kamper[104] = {"home": fh, "away": fb, "vinner": sterkest(fh, fb)}

    def clean(t):
        if not t:
            return None
        # «sikker» = kildegruppa (fra seed, f.eks. "1E" -> gruppe E) er
        # ferdigspilt. 1./2.-plass er da låst; en 3er vises kun hvis projisert
        # topp-8. Brukes til grønn/rød fargekoding av 16-dels-lagene på siden.
        seed = t["seed"]
        sikker = bool(gf.get("Gruppe " + seed[1], False)) if len(seed) >= 2 else False
        return {"navn": t["navn"], "seed": seed, "sikker": sikker}

    runder = []
    for navn, nrs in RUNDER:
        runder.append({"navn": navn, "kamper": [
            {"nr": nr, "home": clean(kamper[nr]["home"]),
             "away": clean(kamper[nr]["away"]), "vinner": clean(kamper[nr]["vinner"])}
            for nr in nrs]})

    return {
        "mester": clean(kamper[104]["vinner"]),
        "bronse": clean(kamper[103]["vinner"]),
        "runder": runder,
    }
