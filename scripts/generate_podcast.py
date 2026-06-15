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


def claude_manus(api_key: str, data: dict, cfg: dict) -> list[dict]:
    """Be Claude skrive dialogmanus. Returnerer [{vert, tekst}, ...]."""
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

    resultater = [
        f"{k['home']} {k['home_score']}-{k['away_score']} {k['away']}"
        for k in kamper
        if k.get("status") == "FINISHED" and k.get("dato") in (today, yesterday)
    ]
    kommende = [
        f"{k['home']} vs {k['away']} ({k.get('group','').replace('GROUP_','Gr. ')})"
        for k in kamper
        if k.get("status") in ("TIMED", "SCHEDULED") and k.get("dato") in (today, tomorrow)
    ]

    # Gruppetabeller - hvem leder?
    grupper = fasit.get("grupper", {})
    gruppeledere = {g: rows[0]["lag"] for g, rows in grupper.items() if rows}

    # Nyheter
    nyheter = hent_nyheter()
    print(f"  {len(nyheter)} VM-nyheter hentet fra RSS")

    sammendrag = {
        "turnering": data.get("turnering", {}).get("navn"),
        "oppdatert": data.get("oppdatert"),
        "stilling_konkurranse": [{"plass": s["plass"], "navn": s["navn"], "poeng": s["poeng"]} for s in stilling],
        "ferske_resultater": resultater,
        "kommende_kamper": kommende,
        "toppscorere": [{"navn": x["navn"], "lag": x.get("lag",""), "maal": x.get("maal",0)} for x in topp_sc],
        "gruppeledere": gruppeledere,
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
        f"Vertene trenger IKKE lage disse lydene selv.\n\n"
        f"STRUKTUR (følg denne rekkefølgen):\n"
        f"1. INTRO — Kort, energisk velkomst. Kommenter intro-jingelen på en morsom måte "
        f"(f.eks. 'For en intro!', 'Den jingelen blir aldri gammel!'). Sett stemningen.\n"
        f"2. OPPSUMMERING — Gå gjennom de ferske kampresultatene. "
        f"Hvem i kompisgjengen traff blink på tippingen? Hvem bommet fullstendig? "
        f"Trekk fram morsomme fakta fra kampene (storseire, sjokkresultater, målfester). "
        f"Sammenlign tippingen mot fasit — vær konkret med navn og tips. "
        f"{a} blir gira på mål og overraskelser, {b} kommer med den tørre analysen.\n"
        f"3. NESTE RUNDE — Kommende kamper og hva man skal følge med på. "
        f"Hvem har gjort smarte/dumme tips som kan slå til eller smelle? "
        f"Stjernespillere å følge med på, spennende oppgjør, bygg forventning. "
        f"Vev inn relevante nyheter (skader, favoritter, kontroverser) fra overskriftene som er gitt.\n"
        f"4. OUTRO — Aller siste replikk skal ALLTID avsluttes med ordene 'Spill av outroen!' "
        f"som et signal til outro-jingelen. Gjør det naturlig og morsomt.\n\n"
        f"TONE: Morsom, energisk, personlig. Samspillet mellom de to vertene er hjertet i showet. "
        f"Vev inn konkrete data — lagnavn, spillernavn, resultater, stillinger, poeng. "
        f"Lengde: rundt 14-20 replikker totalt (ca. 5-7 minutter tale). "
        f"Svar KUN med gyldig JSON: en liste med objekter med feltene 'vert' (enten '{a}' eller '{b}') "
        f"og 'tekst'. Ingen markdown, ingen forklaring. Skriv tall som ord der det er naturlig for tale."
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
    # valider
    out = []
    for m in manus:
        if m.get("vert") and m.get("tekst"):
            out.append({"vert": m["vert"], "tekst": m["tekst"]})
    return out


def eleven_tts(api_key: str, voice_id: str, tekst: str, ut: Path,
               modell: str = "eleven_turbo_v2_5"):
    body = json.dumps({
        "text": tekst,
        "model_id": modell,
        # Energisk levering: lav stability gir mer variasjon/innlevelse,
        # style skrur opp uttrykksfullheten, speaker_boost gir mer nærvær.
        "voice_settings": {
            "stability": 0.3,
            "similarity_boost": 0.8,
            "style": 0.6,
            "use_speaker_boost": True,
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

    print("Skriver manus med Claude ...")
    manus = claude_manus(anthropic_key, data, cfg)
    print(f"  {len(manus)} replikker.")

    # Lagre manuset alltid (gratis)
    (pod_dir / f"manus-{ep_id}.json").write_text(
        json.dumps(manus, ensure_ascii=False, indent=2), encoding="utf-8")

    eleven_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    lyd_fil = None
    if lag_lyd_flag and eleven_key:
        tts_modell = cfg.get("podcast", {}).get("tts_modell", "eleven_turbo_v2_5")
        print(f"Genererer lyd med ElevenLabs ({tts_modell}) ...")
        stemmer = cfg.get("podcast", {}).get("stemmer") or DEFAULT_VOICES
        with tempfile.TemporaryDirectory() as tmp:
            klipp = []
            # Intro-jingle
            jingle_intro = pod_dir / "jingle-intro.mp3"
            if jingle_intro.exists():
                klipp.append(jingle_intro)
                print("  Intro-jingle lagt til")
            for i, m in enumerate(manus):
                vid = stemmer.get(m["vert"]) or list(stemmer.values())[i % len(stemmer)]
                kp = Path(tmp) / f"{i:03d}.mp3"
                eleven_tts(eleven_key, vid, m["tekst"], kp, tts_modell)
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
