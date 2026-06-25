"""
Genererer en podcast-episode for en turnering:
  1. Leser dagens stilling + topp-lister (stilling.json)
  2. Ber Claude skrive et to-verts dialogmanus pa norsk
  3. Sender hver replikk til ElevenLabs (en stemme per vert)
  4. Setter sammen klippene til en MP3 med ffmpeg
  5. Skriver siste.json + oppdaterer RSS-feed (feed.xml)

Trygg som standard: lyd genereres BARE hvis ELEVENLABS_API_KEY finnes
OG --lyd er satt (eller miljovariabel LAG_LYD=1). Ellers lages kun manus.

Kjorer pa kampdager (styres av workflow). Krever ANTHROPIC_API_KEY for manus.

Bruk: python scripts/generate_podcast.py tournaments/vm-2026 [--lyd]
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech"

RSS_FEEDS = [
    "https://www.vg.no/rss/feed/?categories=sport&format=rss",
    "https://www.nrk.no/toppsaker.rss",
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
]

# To verter. Stemme-ID-ene kan overstyres i tournament.json -> podcast.stemmer.
# Standard er ElevenLabs premade stemmer (tilgjengelig pa alle planer).
DEFAULT_VOICES = {
    "Ada": "EXAVITQu4vr4xnSDxMaL",     # rolig, kvinnelig
    "Jonas": "TxGEqnHWrfWFTfGW9XjX",   # varm, mannlig
}


def hent_nyheter() -> list[str]:
    """Hent VM-relaterte overskrifter fra RSS-feeds."""
    import xml.etree.ElementTree as ET
    nyheter = []
    vm_sokeord = ["vm", "world cup", "fifa", "2026", "qatar", "usa", "mexico", "canada"]
    for url in RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TippekonkBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                xml = r.read().decode("utf-8", errors="replace")
            root = ET.fromstring(xml)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                if not title:
                    continue
                tekst = f"{title}. {desc}" if desc else title
                if any(s in tekst.lower() for s in vm_sokeord):
                    nyheter.append(tekst[:200])
        except Exception as e:
            print(f"  (RSS {url} feilet: {e})")
    return nyheter[:15]


_TALL_NO = ["null", "én", "to", "tre", "fire", "fem", "seks", "sju", "åtte",
            "ni", "ti", "elleve", "tolv", "tretten", "fjorten", "femten", "seksten"]


def _tall(n) -> str:
    """Generelle tall i tale: til og med ti som ord, over ti som siffer."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return _TALL_NO[n] if 0 <= n <= 10 else str(n)


_SCORE_ORD = {
    0: "null",
    1: "én",
    2: "to",
    3: "tre",
    4: "fire",
    5: "fem",
    6: "seks",
    7: "sju",
    8: "åtte",
    9: "ni",
    10: "ti",
    11: "elve",
    12: "tolv",
}


def _score(n) -> str:
    """Score-tall slik de bør uttales i norsk fotballprat."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return _SCORE_ORD.get(n, str(n))


def _scoreline(home_score, away_score) -> str:
    """Naturlig norsk scoreuttale: én, én / to, null / tre, én."""
    return f"{_score(home_score)}, {_score(away_score)}"


def _nn(s) -> str:
    return str(s).strip().lower() if s is not None else ""


def _fold(s: str) -> str:
    """Aksent-uavhengig nøkkel for filnavn-matching."""
    return (str(s).lower().replace("ø", "o").replace("æ", "a").replace("å", "a"))


def _finn_jingle(pod_dir: Path, navn: str):
    """Finn en jingle-fil aksent-/store-bokstav-uavhengig (så 'dagens_broler.mp3'
    matcher 'dagens_brøler.mp3'). Returnerer Path eller None."""
    if not navn:
        return None
    p = pod_dir / navn
    if p.exists():
        return p
    mål = _fold(navn)
    for f in pod_dir.glob("*.mp3"):
        if _fold(f.name) == mål:
            return f
    return None


def lag_vant_nylig(data: dict, lagnavn: str) -> bool:
    """True hvis <lagnavn> vant en kamp i dag eller i går — ferskt nok til å feire.
    Bruker samme dagvindu som de ferske resultatene vertene snakker om, så
    seiers-sangen spilles i episoden som dekker selve seieren (ikke for alltid etterpå)."""
    if not lagnavn:
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    mål = _nn(lagnavn)
    for k in (data.get("fasit") or {}).get("kamper", []):
        if k.get("status") != "FINISHED" or k.get("dato") not in (today, yesterday):
            continue
        hs, as_ = k.get("home_score"), k.get("away_score")
        if hs is None or as_ is None:
            continue
        if _nn(k.get("home")) == mål and hs > as_:
            return True
        if _nn(k.get("away")) == mål and as_ > hs:
            return True
    return False


# Tallord over ti -> TTS-vennlig form. Vi vil ha "elve" for 11 i norsk tale.
_TALLORD = {
    "elleve": "elve", "tolv": "12", "tretten": "13", "fjorten": "14", "femten": "15",
    "seksten": "16", "sytten": "17", "atten": "18", "nitten": "19",
    "tjueén": "21", "tjueen": "21", "tjueto": "22", "tjuetre": "23", "tjuefire": "24",
    "tjuefem": "25", "tjueseks": "26", "tjuesju": "27", "tjuesyv": "27",
    "tjueåtte": "28", "tjueni": "29", "tjue": "20",
    "tretti": "30", "førti": "40", "femti": "50", "seksti": "60", "sytti": "70",
    "åtti": "80", "nitti": "90", "hundre": "100",
}
# Lengste først, så "tjuefem" matcher før "tjue"
_TALLORD_RE = re.compile(
    r"(?<![a-zæøåA-ZÆØÅ])(" +
    "|".join(sorted((re.escape(k) for k in _TALLORD), key=len, reverse=True)) +
    r")(?![a-zæøåA-ZÆØÅ])", re.IGNORECASE)


def normaliser_tallord(tekst: str) -> str:
    """Gjør tallord mer TTS-vennlige, blant annet elleve -> elve."""
    return _TALLORD_RE.sub(lambda m: _TALLORD[m.group(1).lower()], tekst)


_SCORE_TIL_RE = re.compile(
    r"\b(null|én|en|to|tre|fire|fem|seks|sju|syv|åtte|ni|ti|elve|tolv|\d+)\s+til\s+"
    r"(null|én|en|to|tre|fire|fem|seks|sju|syv|åtte|ni|ti|elve|tolv|\d+)\b",
    re.IGNORECASE,
)


def normaliser_tts_tekst(tekst: str) -> str:
    """Siste vask før TTS: naturlig scoreuttale og ønsket norsk uttale av 11."""
    tekst = normaliser_tallord(tekst)

    # ElevenLabs uttaler ofte "elleve" mer bokstavlig enn ønsket.
    tekst = re.sub(r"\belleve\b", "elve", tekst, flags=re.IGNORECASE)
    tekst = re.sub(r"\b11\b", "elve", tekst)

    # Gjør "én til én" / "to til null" om til mer naturlig norsk score: "én, én" / "to, null".
    tekst = _SCORE_TIL_RE.sub(lambda m: f"{m.group(1)}, {m.group(2)}", tekst)

    # Rydd opp i ubestemt "en" når det åpenbart står som scoretall.
    tekst = re.sub(r"\ben, en\b", "én, én", tekst, flags=re.IGNORECASE)
    tekst = re.sub(r"\ben, null\b", "én, null", tekst, flags=re.IGNORECASE)
    tekst = re.sub(r"\bnull, en\b", "null, én", tekst, flags=re.IGNORECASE)

    return tekst


def spalte_kandidater(tipp: dict, kamper: list, window: tuple) -> dict:
    """Beregn grunnlag for spaltene, så vertene slipper å finne på noe:
      - modige_treff: tippere som traff på et minoritetsutfall (gikk mot flokken)
      - storste_sjokk: kamper der få/ingen traff, + hvilket lag som skuffet
      - tippere_som_bommet_mest: hvem bommet på flest utfall
    Bruker ferske kamper (window) hvis mulig, ellers alle ferdigspilte.
    """
    ferdige = [k for k in kamper if k.get("status") == "FINISHED" and k.get("dato") in window]
    if not ferdige:
        ferdige = [k for k in kamper if k.get("status") == "FINISHED"]

    modige, sjokk, bom_teller = [], [], {}
    for k in ferdige:
        pk = tipp.get((_nn(k["home"]), _nn(k["away"])))
        if not pk or not pk.get("fasit"):
            continue
        navn = pk.get("navn", {})
        fasit = pk["fasit"]
        riktige = navn.get(fasit, [])
        alle = navn.get("H", []) + navn.get("U", []) + navn.get("B", [])
        total = len(alle)
        if not total:
            continue
        andel = len(riktige) / total
        hs, as_ = k.get("home_score"), k.get("away_score")
        res = _scoreline(hs, as_) if hs is not None else ""
        utfall = (f"{k['home']} vant" if fasit == "H"
                  else f"{k['away']} vant" if fasit == "B" else "uavgjort")
        kampnavn = f"{k['home']} mot {k['away']}"

        # Modige treff: et mindretall (<= 40 %) som traff
        if riktige and andel <= 0.4:
            modige.append({"kamp": kampnavn, "utfall": utfall,
                           "modige_tippere": riktige, "andel_prosent": round(andel * 100)})

        # Sjokk: hvilket lag skuffet flertallet?
        flertall = max(("H", "U", "B"), key=lambda p: len(navn.get(p, [])))
        skuffet = ""
        if flertall != fasit:
            skuffet = k["home"] if flertall == "H" else k["away"] if flertall == "B" else ""
        sjokk.append({"kamp": kampnavn, "resultat": res, "antall_traff": len(riktige),
                      "antall_bommet": total - len(riktige), "skuffet_lag": skuffet})

        for n in alle:
            if n not in riktige:
                bom_teller[n] = bom_teller.get(n, 0) + 1

    modige.sort(key=lambda x: x["andel_prosent"])
    sjokk.sort(key=lambda x: x["antall_traff"])
    dagens_bom = sorted(({"navn": n, "antall_bom": c} for n, c in bom_teller.items()),
                        key=lambda x: -x["antall_bom"])[:4]
    return {"modige_treff": modige[:5], "storste_sjokk": sjokk[:5],
            "tippere_som_bommet_mest": dagens_bom}


def tavle_endringer(data: dict, historikk: list) -> dict:
    """Sammenlign dagens stilling mot snapshot ~24t tilbake, så oppsummeringen
    kan si hvor mange poeng lederen fikk og hva som har endret seg på tavla."""
    stilling = data.get("stilling", [])
    if not stilling:
        return {}
    try:
        naa_tid = datetime.fromisoformat((data.get("oppdatert") or "").replace("Z", "+00:00"))
    except ValueError:
        naa_tid = datetime.now(timezone.utc)
    mål = naa_tid - timedelta(hours=24)

    # Siste snapshot på eller før målet (ellers det aller første vi har).
    forrige = None
    for snap in historikk:
        try:
            t = datetime.fromisoformat(snap["tid"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if t <= mål:
            forrige = snap
    if forrige is None and historikk:
        forrige = historikk[0]
    før = (forrige or {}).get("poeng", {})

    leder = stilling[0]
    endringer = []
    for s in stilling:
        d = s["poeng"] - før.get(s["navn"], s["poeng"])
        if d:
            endringer.append({"navn": s["navn"], "pluss": d})
    forrige_leder = max(før, key=før.get) if før else leder["navn"]
    return {
        "leder": leder["navn"],
        "leder_poeng": leder["poeng"],
        "leder_fikk_siste_doegn": leder["poeng"] - før.get(leder["navn"], leder["poeng"]),
        "forrige_leder": forrige_leder,
        "ny_leder": forrige_leder != leder["navn"],
        "poengendringer_siste_doegn": sorted(endringer, key=lambda x: -x["pluss"]),
    }


def poeng_kilder(s: dict) -> dict:
    """Poeng per kategori for én deltaker, fra linjene (key -> pts)."""
    return {l.get("key"): l.get("pts", 0) for l in s.get("linjer", []) if l.get("key")}


def grupper_ferdig_status(fasit: dict) -> dict:
    """{ 'Gruppe A': bool } — bruk fasit-feltet hvis det finnes, ellers utled
    fra kampene (samme logikk som engine), så det virker på eldre fasit også."""
    if fasit.get("grupper_ferdig"):
        return fasit["grupper_ferdig"]
    g_tot, g_fin = {}, {}
    for k in fasit.get("kamper", []):
        if k.get("stage") != "GROUP_STAGE" or not k.get("group"):
            continue
        lab = "Gruppe " + k["group"].split("_")[-1]
        g_tot[lab] = g_tot.get(lab, 0) + 1
        if k.get("status") == "FINISHED" and k.get("home_score") is not None:
            g_fin[lab] = g_fin.get(lab, 0) + 1
    return {lab: g_fin.get(lab, 0) == g_tot[lab] for lab in g_tot}


def gruppevinner_oversikt(data: dict, window: tuple) -> list[dict]:
    """Ferdigspilte grupper med vinner + hvem i gjengen som tippet vinneren riktig
    (og dermed fikk 3 poeng). `nylig_avgjort` = gruppa ble ferdig i dag/i går."""
    fasit = data.get("fasit", {})
    gw = fasit.get("group_winners") or {}
    stilling = data.get("stilling", [])

    ferdig = grupper_ferdig_status(fasit)
    sist_dato = {}
    for k in fasit.get("kamper", []):
        if k.get("stage") != "GROUP_STAGE" or not k.get("group"):
            continue
        if k.get("status") == "FINISHED" and k.get("home_score") is not None:
            lab = "Gruppe " + k["group"].split("_")[-1]
            d = k.get("dato", "")
            if d > sist_dato.get(lab, ""):
                sist_dato[lab] = d

    ut = []
    for g, er_ferdig in sorted(ferdig.items()):
        if not er_ferdig:
            continue
        traff = []
        for s in stilling:
            for l in s.get("linjer", []):
                if l.get("key") != "gruppevinner":
                    continue
                for gr in l.get("grupper", []):
                    if gr.get("gruppe") == g and gr.get("hit"):
                        traff.append(s["navn"])
        ut.append({
            "gruppe": g, "vinner": gw.get(g),
            "antall_traff": len(traff), "tippere_som_traff": traff,
            "nylig_avgjort": sist_dato.get(g, "") in window,
        })
    return ut


def claude_oppsummering(api_key: str, data: dict, cfg: dict,
                        nyheter: list[str], historikk: list) -> str:
    """Egen Claude-prompt: kort tekst-oppsummering for nettsiden. Returnerer ren
    løpende tekst (ingen markdown) — resultater, highlights og tavle-endringer."""
    fasit = data.get("fasit", {})
    kamper = fasit.get("kamper", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    resultater = []
    for k in kamper:
        if k.get("status") != "FINISHED" or k.get("dato") not in (today, yesterday):
            continue
        hs, as_ = k.get("home_score"), k.get("away_score")
        if hs is None or as_ is None:
            continue
        resultater.append(f"{k['home']} {hs}-{as_} {k['away']}")

    tavle = tavle_endringer(data, historikk)
    topp_sc = [{"navn": x["navn"], "lag": x.get("lag", ""), "maal": x.get("maal", 0)}
               for x in fasit.get("topp_scorere", [])[:5]]
    stilling = []
    for s in data.get("stilling", []):
        k = poeng_kilder(s)
        stilling.append({"plass": s["plass"], "navn": s["navn"], "poeng": s["poeng"],
                         "herav_gruppespill": k.get("hub", 0),
                         "herav_gruppevinner": k.get("gruppevinner", 0)})
    gruppevinnere = gruppevinner_oversikt(data, (today, yesterday))

    sammendrag = {
        "turnering": data.get("turnering", {}).get("navn"),
        "dato": today,
        "kampresultater_siste_doegn": resultater,
        "totalt_antall_maal_i_mesterskapet": fasit.get("antall_maal"),
        "toppscorere": topp_sc,
        "ferdige_grupper_med_vinner": gruppevinnere,
        "tavle_endringer": tavle,
        "stilling": stilling,
        "nyheter_fra_media": nyheter,
    }

    system = (
        "Du skriver en kort, poengtert oppsummering på naturlig norsk bokmål av siste "
        "døgns hendelser i fotball-VM 2026, for forsiden til en privat tippekonkurranse "
        "blant norske kompiser. Teksten skal være lett å lese og lett å kopiere.\n\n"
        "INNHOLD (få med alt dette):\n"
        "- Resultatet av ALLE kampene i 'kampresultater_siste_doegn'. Nevn hver kamp med "
        "lag og siffer-resultat (f.eks. 'Mexico vant 1-0 over Sør-Korea').\n"
        "- Highlights og store øyeblikk: målfester, store seire, hat trick, dramatikk, "
        "skader/brukne bein, kontroverser. Bruk KUN det som faktisk fremgår av "
        "kampresultatene, toppscorerlista og overskriftene i 'nyheter_fra_media'.\n"
        "- Hvor mange poeng lederen fikk det siste døgnet ('tavle_endringer."
        "leder_fikk_siste_doegn') og hva som har endret seg på tavla — ny leder hvis "
        "'ny_leder' er sann, hvem som klatret mest, osv. Hvis ingenting endret seg, si "
        "det kort.\n"
        "- Gruppevinnere: hvis en gruppe nettopp ble ferdigspilt "
        "('ferdige_grupper_med_vinner' der 'nylig_avgjort' er sann), nevn hvem som vant "
        "gruppa og at det ga 3 poeng til de som tippet vinneren riktig (se "
        "'tippere_som_traff'). Poengsummene i 'stilling' INKLUDERER nå gruppevinner-poeng "
        "('herav_gruppevinner' viser hvor mange av poengene som kom derfra).\n\n"
        "REGLER:\n"
        "- HOLD DEG STRENGT TIL DATAENE. Finn ALDRI opp resultater, navn, tall eller "
        "hendelser. Ikke anta et hat trick eller en skade med mindre det fremgår av "
        "dataene/overskriftene.\n"
        "- Skriv kampresultater med siffer (4-1, 1-0), ikke som ord.\n"
        "- Ren løpende tekst i 2 til 3 korte avsnitt. INGEN markdown, ingen punktlister, "
        "ingen overskrift, ingen emoji. Ikke for langt — rundt 8 til 12 setninger.\n"
        "- Tone: engasjert og lett humoristisk, men informativ.\n"
        "- Svar KUN med selve oppsummeringsteksten, ingenting annet."
    )
    user = f"Dagens data:\n{json.dumps(sammendrag, ensure_ascii=False, indent=2)}\n\nSkriv oppsummeringen nå."

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1200,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(ANTHROPIC_URL, data=body, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return text.strip()


def claude_manus(api_key: str, data: dict, cfg: dict, nyheter: list[str]) -> list[dict]:
    """Be Claude skrive dialogmanus. Returnerer [{vert, tekst}, ...].
    `nyheter` hentes én gang i main() og deles med oppsummeringen."""
    vert_navn = list((cfg.get("podcast", {}).get("stemmer") or DEFAULT_VOICES).keys())
    a, b = (vert_navn + ["Ada", "Jonas"])[:2]

    stilling = data.get("stilling", [])[:8]
    fasit = data.get("fasit", {})
    topp_sc = fasit.get("topp_scorere", [])[:5]
    kamper = fasit.get("kamper", [])

    # Gårsdagens og dagens resultater
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    # Faktisk tipping per kamp (hvem tippet H/U/B) fra tippefordeling
    tf = (data.get("statistikk") or {}).get("tippefordeling") or {}
    tipp = {}
    for dag in tf.get("dager", []):
        for kk in dag.get("kamper", []):
            tipp[(_nn(kk["home"]), _nn(kk["away"]))] = kk

    resultater = []
    for k in kamper:
        if k.get("status") != "FINISHED" or k.get("dato") not in (today, yesterday):
            continue
        hs, as_ = k.get("home_score"), k.get("away_score")
        if hs > as_:
            rtekst = f"{k['home']} slo {k['away']} {_scoreline(hs, as_)}"
        elif hs < as_:
            rtekst = f"{k['away']} slo {k['home']} {_scoreline(as_, hs)}"
        else:
            rtekst = f"{k['home']} og {k['away']} spilte {_scoreline(hs, as_)}"
        obj = {"kamp": f"{k['home']} mot {k['away']}", "resultat": rtekst}
        pk = tipp.get((_nn(k["home"]), _nn(k["away"])))
        if pk and pk.get("fasit"):
            navn = pk.get("navn", {})
            riktig = navn.get(pk["fasit"], [])
            alle = navn.get("H", []) + navn.get("U", []) + navn.get("B", [])
            obj["tippet_riktig_utfall"] = riktig
            obj["bommet_pa_utfall"] = [n for n in alle if n not in riktig]
        resultater.append(obj)

    kommende = []
    for k in kamper:
        if k.get("status") not in ("TIMED", "SCHEDULED") or k.get("dato") not in (today, tomorrow):
            continue
        obj = {"kamp": f"{k['home']} mot {k['away']}",
               "gruppe": k.get("group", "").replace("GROUP_", "Gr. ")}
        pk = tipp.get((_nn(k["home"]), _nn(k["away"])))
        if pk:
            navn = pk.get("navn", {})
            obj["tipping"] = {
                f"{k['home']} vinner": navn.get("H", []),
                "uavgjort": navn.get("U", []),
                f"{k['away']} vinner": navn.get("B", []),
            }
        kommende.append(obj)

    # Grunnlag for spaltene (Gullhår / Dagens Brøler)
    spalte_data = spalte_kandidater(tipp, kamper, (today, yesterday))

    # Gruppevinnere (ferdige grupper, gir 3p) vs live-ledere (uavgjorte grupper).
    gruppevinnere = gruppevinner_oversikt(data, (today, yesterday))
    grupper_ferdig = grupper_ferdig_status(fasit)
    grupper = fasit.get("grupper", {})
    gruppeledere_uavgjort = {g: rows[0]["lag"] for g, rows in grupper.items()
                             if rows and not grupper_ferdig.get(g)}

    # Seiers-sang: hvis seierslaget (Norge) vant nylig spilles «Tre poeng til
    # Norge» rett etter intro-jingelen — og da MÅ vertene anerkjenne den.
    # Samme dagvindu som lyd-genereringen i main(), så manus og lyd er enige.
    seierslag = cfg.get("podcast", {}).get("seierslag", "Norge")
    seierssang_spilles = lag_vant_nylig(data, seierslag)
    if seierssang_spilles:
        seiers_instruks = (
            f"- SEIERS-SANG: Rett etter intro-jingelen spilles seiers-sangen "
            f"«Tre poeng til {seierslag}» fordi {seierslag} nettopp vant en kamp "
            f"(en seier = tre poeng). I den aller FØRSTE replikken MÅ vertene "
            f"anerkjenne sangen og feire kort at {seierslag} tok tre poeng, før de "
            f"går videre med velkomsten.\n"
        )
        intro_seier_note = (
            f" Anerkjenn først seiers-sangen «Tre poeng til {seierslag}» som nettopp "
            f"spilte — {seierslag} vant og tok tre poeng — og feire det kort."
        )
    else:
        seiers_instruks = ""
        intro_seier_note = ""

    sammendrag = {
        "turnering": data.get("turnering", {}).get("navn"),
        "oppdatert": data.get("oppdatert"),
        "stilling_konkurranse": [
            {"plass": s["plass"], "navn": s["navn"], "poeng": s["poeng"],
             "herav_gruppespill": poeng_kilder(s).get("hub", 0),
             "herav_gruppevinner": poeng_kilder(s).get("gruppevinner", 0)}
            for s in stilling],
        "ferske_resultater": resultater,
        "kommende_kamper": kommende,
        "spalte_data": spalte_data,
        "toppscorere": [{"navn": x["navn"], "lag": x.get("lag",""), "maal": x.get("maal",0)} for x in topp_sc],
        "ferdige_grupper_med_vinner": gruppevinnere,
        "gruppeledere_uavgjorte_grupper": gruppeledere_uavgjort,
        "nyheter_fra_media": nyheter,
    }

    system = (
        f"Du skriver manus til en morsom norsk fotballpodcast om fotball-VM 2026, "
        f"med fokus på en privat tippekonkurranse blant en gjeng norske kompiser. "
        f"Deltakernes navn er ekte venner — bruk navnene aktivt og personlig "
        f"(ert dem, hyll dem, sammenlign tippingen deres). Skriv alt på naturlig norsk bokmål.\n\n"
        f"VERTENE (begge er høyenergiske og entusiastiske — det er et heseblesende, gira show):\n"
        f"- {a}: Ivrig og optimistisk, full av energi og blir lett hypet på mål, "
        f"overraskelser og dramatikk. Heier høylytt og tror alltid det beste om favorittlagene sine.\n"
        f"- {b}: Like energisk og høylytt, men med en tørrvittig, sarkastisk kant. Står for den "
        f"taktiske analysen og pirker borti {a} sin overdrevne optimisme — men med innlevelse og driv. "
        f"Tenk på dynamikken som et gira radarpar: to ivrige stemmer som overgår hverandre.\n\n"
        f"VIKTIGE REGLER:\n"
        f"- Vertene skal ALDRI synge. Ingen sangtekster, noter eller synging i dialogen.\n"
        f"- En intro-jingle spilles automatisk før vertene snakker, og en outro-jingle etterpå. "
        f"Vertene trenger IKKE lage disse lydene selv, og skal IKKE kommentere eller anerkjenne "
        f"selve jingelen — bare gå rett på velkomsten.\n"
        f"{seiers_instruks}"
        f"- HOLD DEG STRENGT TIL DATAENE. Finn ALDRI opp resultater, tips, navn eller tall. "
        f"Bruk kun det som faktisk står i dataene under.\n"
        f"- Deltakerne tipper KUN utfall: hjemmeseier, uavgjort eller borteseier — IKKE eksakt "
        f"resultat. Si derfor aldri at noen 'tippet to til én' e.l. Si f.eks. 'Jonas tror Norge "
        f"vinner' eller 'Bjørn tippet uavgjort'. Feltet 'tipping' viser nøyaktig hvem som tippet hva.\n"
        f"- IKKE rams opp lange navnelister. Bruk heller gruppebegreper: 'gjengen', 'folket', "
        f"'majoriteten', 'flertallet', 'de fleste', 'halve gjengen', 'en håndfull'. Nevn enkeltnavn "
        f"sparsomt — kun for å fremheve én eller to som skiller seg ut (lederen, den eneste som "
        f"traff, en som bommet stygt). Heller 'nesten hele gjengen tror Norge vinner' enn å lese "
        f"opp elleve navn.\n"
        f"- TTS-VENNLIG FORMAT: Bruk ALDRI bindestrek eller tankestrek; bruk "
        f"komma og punktum for pauser. Kampresultater skal skrives slik de sies "
        f"i norsk fotballprat: 'én, én', 'to, null', 'tre, én', ikke 'én til én' "
        f"og ikke 'tre-null'. Tallet 11 skal skrives som 'elve', ikke 'elleve' og ikke '11'. "
        f"Andre tall over 12 kan skrives med siffer der det er mest naturlig.\n\n"
        f"STRUKTUR (følg denne rekkefølgen):\n"
        f"1. INTRO — Kort, energisk velkomst rett etter at intro-jingelen har spilt. "
        f"IKKE kommenter eller anerkjenn selve jingelen; ønsk heller velkommen og sett "
        f"stemningen.{intro_seier_note}\n"
        f"2. OPPSUMMERING — Gå gjennom de ferske kampresultatene. "
        f"Hvem i kompisgjengen traff blink på tippingen? Hvem bommet fullstendig? "
        f"Trekk fram morsomme fakta fra kampene (storseire, sjokkresultater, målfester). "
        f"Sammenlign tippingen mot fasit — vær konkret med navn og tips. "
        f"{a} blir gira på mål og overraskelser, {b} kommer med den tørre analysen. "
        f"GRUPPEVINNERE: hvis en gruppe nettopp ble ferdigspilt ('ferdige_grupper_med_vinner' "
        f"der 'nylig_avgjort' er sann), feir hvem som vant gruppa, og at de som tippet vinneren "
        f"riktig ('tippere_som_traff') nettopp sikret seg tre poeng hver. "
        f"POENG: oppdater lytterne på tavla, og husk at poengsummene i 'stilling_konkurranse' nå "
        f"INKLUDERER gruppevinner-poeng — 'herav_gruppevinner' er hvor mange av poengene hver har "
        f"fra gruppevinnere, 'herav_gruppespill' fra kamputfall. Trekk fram om noen klatret takket "
        f"være gruppevinnerne (f.eks. at en tok igjen forspranget ved å treffe på gruppevinnere).\n"
        f"3. GULLHÅR I RÆVA! (fast spalte) — Innled med at en vert kreativt ANNONSERER spalten "
        f"ved navn (f.eks. 'Da er det tid for vår faste spalte: Gullhår i Ræva!' eller 'Vi går "
        f"rett over til Gullhår i Ræva!'). Spalten er en hyllest til å gå mot flokken: bruk "
        f"'spalte_data.modige_treff' (tippere som traff på et utfall få andre turte å satse på). "
        f"Nominer 1 til 3 slike modige sjeler, drodle om hvem som er mest fortjent, og LAND til "
        f"slutt på ÉN verdig vinner (en deltaker). Sett feltet 'jingle_foran' til \"gull\" på den "
        f"FØRSTE INNHOLDSREPLIKKEN — altså replikken RETT ETTER annonseringen — slik at jingelen "
        f"spilles mellom annonsering og innhold.\n"
        f"4. DAGENS BRØLER (fast spalte) — Innled med at en vert kreativt ANNONSERER spalten ved "
        f"navn (f.eks. 'Og så til en annen fast post: Dagens Brøler!'). Spalten tar for seg "
        f"dagens største tabbe: bruk 'spalte_data.storste_sjokk' (kamper der få/ingen traff, og "
        f"hvilket lag som skuffet) og 'spalte_data.tippere_som_bommet_mest'. Skjell gjerne ut "
        f"både lag og tippere med frekk snert. Nominer kandidater og KÅR til slutt ÉN 'Dagens "
        f"Brøler' — det kan være enten et fotballag eller en deltaker. Sett 'jingle_foran' til "
        f"\"broler\" på den FØRSTE INNHOLDSREPLIKKEN (rett etter annonseringen), slik at jingelen "
        f"spilles mellom annonsering og innhold.\n"
        f"5. NESTE RUNDE — Kommende kamper og hva man skal følge med på. "
        f"Hvem har gjort smarte/dumme tips som kan slå til eller smelle? "
        f"Stjernespillere å følge med på, spennende oppgjør, bygg forventning. "
        f"Nevn gjerne hvilke grupper som fortsatt er åpne ('gruppeledere_uavgjorte_grupper') — "
        f"der er det fremdeles tre gruppevinner-poeng å hente for de som tippet lederen. "
        f"Vev inn relevante nyheter (skader, favoritter, kontroverser) fra overskriftene som er gitt.\n"
        f"6. OUTRO — Aller siste replikk skal være en naturlig, varm avskjed: takk for nå og "
        f"at vi høres neste gang (gjerne med en frekk vri). IKKE skriv 'Spill av outroen!' eller "
        f"andre lyd-signaler — outro-jingelen spilles automatisk etterpå.\n\n"
        f"TONE: Kul, frekk og høyt tempo. Vertene er selvsikre og lar munnen gå — frekke jokes, "
        f"syrlige stikk og lett politisk ukorrekt humor. De roaster kompisene og lagene uten "
        f"filter, og er ikke redde for å være litt drøye. (Grense: ikke hatefullt eller "
        f"diskriminerende — ikke angrip folk på etnisitet, religion, legning eller kjønn. "
        f"Hold brodden til ertende kompis-roast og fotball.) "
        f"Samspillet mellom de to vertene er hjertet i showet. "
        f"Vev inn konkrete data — lagnavn, spillernavn, resultater, stillinger, poeng. "
        f"Lengde: rundt 20 til 28 replikker totalt (cirka seks til ni minutter tale). "
        f"Svar KUN med gyldig JSON: en liste med objekter med feltene 'vert' (enten '{a}' eller "
        f"'{b}') og 'tekst'. Et objekt kan i tillegg ha feltet 'jingle_foran' med verdien \"gull\" "
        f"eller \"broler\" — sett det KUN på den aller første replikken i hver av de to spaltene. "
        f"Ingen markdown, ingen forklaring. Skriv tall som ord der det er naturlig for tale."
    )
    user = f"Dagens data:\n{json.dumps(sammendrag, ensure_ascii=False, indent=2)}\n\nSkriv manuset nå."

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")

    req = urllib.request.Request(ANTHROPIC_URL, data=body, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read().decode("utf-8"))

    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    manus = json.loads(text)
    # valider + normaliser tekst; behold ev. jingle_foran
    out = []
    for m in manus:
        if m.get("vert") and m.get("tekst"):
            replikk = {"vert": m["vert"], "tekst": normaliser_tts_tekst(m["tekst"])}
            jf = m.get("jingle_foran")
            if jf in ("gull", "broler"):
                replikk["jingle_foran"] = jf
            out.append(replikk)
    return out


def eleven_tts(api_key: str, voice_id: str, tekst: str, ut: Path,
               modell: str = "eleven_turbo_v2_5", speed: float = 1.0):
    body = json.dumps({
        "text": tekst,
        "model_id": modell,
        # Energisk levering: lav stability gir mer variasjon/innlevelse,
        # style skrur opp uttrykksfullheten, speaker_boost gir mer nærvær,
        # speed styrer taletempo (0.7–1.2; 1.0 = normalt).
        "voice_settings": {
            "stability": 0.3,
            "similarity_boost": 0.8,
            "style": 0.6,
            "use_speaker_boost": True,
            "speed": max(0.7, min(1.2, speed)),
        },
    }).encode("utf-8")
    req = urllib.request.Request(f"{ELEVEN_URL}/{voice_id}", data=body, headers={
        "xi-api-key": api_key,
        "content-type": "application/json",
        "accept": "audio/mpeg",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
            ut.write_bytes(data)
            print(f"    TTS ok: {len(data)} bytes, voice={voice_id}")
            if len(data) < 1000:
                print(f"    ADVARSEL: veldig liten fil ({len(data)} bytes) — mulig tom lyd")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"    TTS FEIL {e.code}: {err_body}")
        raise


def sett_sammen(klipp: list[Path], ut: Path):
    """Konkatener MP3-klipp med ffmpeg, re-encode til konsistent format."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for k in klipp:
            f.write(f"file '{k.resolve()}'\n")
        liste = f.name
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", liste,
         "-ar", "44100", "-ac", "1", "-b:a", "128k", str(ut)],
        capture_output=True,
    )
    os.unlink(liste)
    if result.returncode != 0:
        print(f"  ffmpeg feil: {result.stderr.decode('utf-8', errors='replace')[-500:]}")
        result.check_returncode()


def lag_feed(pod_dir: Path, cfg: dict, episoder: list, base_url: str):
    """Bygg enkel RSS 2.0-feed for Spotify/Apple. base_url = full URL til podcast-mappa."""
    navn = cfg.get("navn", "Tippepodcast")
    items = []
    for ep in episoder:
        pub = format_datetime(datetime.fromisoformat(ep["dato"]))
        lyd_url = f"{base_url.rstrip('/')}/{ep['lyd']}"
        lengde = ep.get("bytes", 0)
        items.append(f"""    <item>
      <title>{ep['tittel']}</title>
      <description>{ep['ingress']}</description>
      <enclosure url="{lyd_url}" type="audio/mpeg" length="{lengde}"/>
      <guid isPermaLink="false">{ep['id']}</guid>
      <pubDate>{pub}</pubDate>
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{navn} - podcast</title>
    <description>Daglig oppdatering fra tippekonkurransen.</description>
    <language>no</language>
{chr(10).join(items)}
  </channel>
</rss>"""
    (pod_dir / "feed.xml").write_text(feed, encoding="utf-8")


def main(tournament_dir: str, lag_lyd_flag: bool, force: bool = False):
    tdir = Path(tournament_dir)
    cfg = json.loads((tdir / "tournament.json").read_text(encoding="utf-8"))
    stilling_p = tdir / "data" / "stilling.json"
    if not stilling_p.exists():
        print("  Ingen stilling.json - hopper over podcast.")
        return
    data = json.loads(stilling_p.read_text(encoding="utf-8"))

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not anthropic_key:
        print("  Ingen ANTHROPIC_API_KEY - kan ikke lage manus.")
        return

    pod_dir = tdir / "data" / "podcast"
    pod_dir.mkdir(parents=True, exist_ok=True)
    dato = datetime.now(timezone.utc)
    ep_id = dato.strftime("%Y%m%d")

    # En episode per dag: hopp over hvis dagens allerede finnes (--force overstyrer).
    if not force and (pod_dir / f"manus-{ep_id}.json").exists():
        print(f"  Episode for {ep_id} finnes allerede - hopper over (bruk --force for a regenerere).")
        return

    # RSS hentes én gang og deles av manus + oppsummering.
    nyheter = hent_nyheter()
    print(f"  {len(nyheter)} VM-nyheter hentet fra RSS")

    print("Skriver manus med Claude ...")
    manus = claude_manus(anthropic_key, data, cfg, nyheter)
    print(f"  {len(manus)} replikker.")

    # Lagre manuset alltid (gratis)
    (pod_dir / f"manus-{ep_id}.json").write_text(
        json.dumps(manus, ensure_ascii=False, indent=2), encoding="utf-8")

    # Oppsummering for nettsiden — bygges hver gang podcasten kjøres (egen prompt).
    oppsummering = ""
    try:
        hist_p = tdir / "data" / "poenghistorikk.json"
        historikk = json.loads(hist_p.read_text(encoding="utf-8")) if hist_p.exists() else []
        oppsummering = claude_oppsummering(anthropic_key, data, cfg, nyheter, historikk)
        (pod_dir / f"oppsummering-{ep_id}.json").write_text(
            json.dumps({"tekst": oppsummering, "id": ep_id, "dato": dato.isoformat()},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  Oppsummering: {len(oppsummering)} tegn")
    except Exception as e:
        print(f"  (oppsummering hoppet over: {e})")

    eleven_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    lyd_fil = None
    if lag_lyd_flag and eleven_key:
        tts_modell = cfg.get("podcast", {}).get("tts_modell", "eleven_turbo_v2_5")
        print(f"Genererer lyd med ElevenLabs ({tts_modell}) ...")
        stemmer = cfg.get("podcast", {}).get("stemmer") or DEFAULT_VOICES
        tempo = cfg.get("podcast", {}).get("tempo", {})
        tempo_std = tempo.get("_standard", 1.08)
        # Spalte-id -> jingle-filnavn
        spalte_jingle = {s["id"]: s.get("jingle") for s in cfg.get("podcast", {}).get("spalter", [])}
        with tempfile.TemporaryDirectory() as tmp:
            klipp = []
            # Intro-jingle
            jingle_intro = pod_dir / "jingle-intro.mp3"
            if jingle_intro.exists():
                klipp.append(jingle_intro)
                print("  Intro-jingle lagt til")
            # Seiers-sang: spilles rett etter intro-jingelen hvis Norge vant nylig.
            seierslag = cfg.get("podcast", {}).get("seierslag", "Norge")
            seiersfil = cfg.get("podcast", {}).get("seierssang", "tre-poeng-til-norge.mp3")
            if seiersfil and lag_vant_nylig(data, seierslag):
                sp = _finn_jingle(pod_dir, seiersfil)
                if sp:
                    klipp.append(sp)
                    print(f"  Seiers-sang lagt til ({sp.name}) — {seierslag} vant nylig!")
                else:
                    print(f"  ADVARSEL: {seierslag} vant, men fant ikke {seiersfil}")
            for i, m in enumerate(manus):
                # Spalte-jingle foran første replikk i en spalte
                jf = m.get("jingle_foran")
                if jf and spalte_jingle.get(jf):
                    jp = _finn_jingle(pod_dir, spalte_jingle[jf])
                    if jp:
                        klipp.append(jp)
                        print(f"  Spalte-jingle '{jf}' lagt til ({jp.name})")
                    else:
                        print(f"  ADVARSEL: fant ikke jingle for spalte '{jf}' ({spalte_jingle[jf]})")
                vid = stemmer.get(m["vert"]) or list(stemmer.values())[i % len(stemmer)]
                spd = tempo.get(m["vert"], tempo_std)
                kp = Path(tmp) / f"{i:03d}.mp3"
                eleven_tts(eleven_key, vid, m["tekst"], kp, tts_modell, spd)
                klipp.append(kp)
            # Outro-jingle
            jingle_outro = pod_dir / "jingle-outro.mp3"
            if jingle_outro.exists():
                klipp.append(jingle_outro)
                print("  Outro-jingle lagt til")
            lyd_fil = f"episode-{ep_id}.mp3"
            sett_sammen(klipp, pod_dir / lyd_fil)
        print(f"  Lyd: {lyd_fil}")
    elif lag_lyd_flag:
        print("  --lyd satt, men ingen ELEVENLABS_API_KEY. Hopper over lyd.")
    else:
        print("  Lyd av (kjor med --lyd for a generere). Kun manus laget.")

    leder = data.get("stilling", [{}])[0]
    ingress = f"{leder.get('navn','?')} leder med {leder.get('poeng','?')} poeng." if data.get("har_fasit") else "Forhandsomtale for mesterskapet."
    siste = {
        "tittel": f"{cfg['kort_navn']} - {dato.strftime('%d.%m')}",
        "ingress": ingress,
        "oppsummering": oppsummering,
        "lyd": lyd_fil,
        "dato": dato.isoformat(),
        "id": ep_id,
        "feed": True,
    }
    (pod_dir / "siste.json").write_text(json.dumps(siste, ensure_ascii=False, indent=2), encoding="utf-8")

    # Oppdater feed-liste
    feed_data = pod_dir / "episoder.json"
    eps = json.loads(feed_data.read_text(encoding="utf-8")) if feed_data.exists() else []
    eps = [e for e in eps if e["id"] != ep_id]
    if lyd_fil:
        eps.insert(0, {**siste, "lyd": lyd_fil, "bytes": (pod_dir / lyd_fil).stat().st_size})
        feed_data.write_text(json.dumps(eps, ensure_ascii=False, indent=2), encoding="utf-8")
        base = cfg.get("podcast", {}).get("base_url", f"./{cfg['id']}/podcast")
        lag_feed(pod_dir, cfg, eps[:20], base)
    print("Ferdig.")


if __name__ == "__main__":
    args = sys.argv[1:]
    lyd = "--lyd" in args or os.environ.get("LAG_LYD") == "1"
    force = "--force" in args or os.environ.get("FORCE_PODCAST") == "1"
    dirs = [a for a in args if not a.startswith("--")]
    main(dirs[0] if dirs else "tournaments/vm-2026", lyd, force)
