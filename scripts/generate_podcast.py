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
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech"

# To verter. Stemme-ID-ene kan overstyres i tournament.json -> podcast.stemmer.
# Standard er ElevenLabs innebygde stemmer (tilgjengelig pa alle planer).
DEFAULT_VOICES = {
    "Ada": "EXAVITQu4vr4xnSDxMaL",     # rolig, kvinnelig
    "Jonas": "TxGEqnHWrfWFTfGW9XjX",   # varm, mannlig
}


def claude_manus(api_key: str, data: dict, cfg: dict) -> list[dict]:
    """Be Claude skrive dialogmanus. Returnerer [{vert, tekst}, ...]."""
    vert_navn = list((cfg.get("podcast", {}).get("stemmer") or DEFAULT_VOICES).keys())
    a, b = (vert_navn + ["Ada", "Jonas"])[:2]

    stilling = data.get("stilling", [])[:8]
    topp_sc = data.get("topp_scorere", [])[:5]
    topp_as = data.get("topp_assist", [])[:5]

    sammendrag = {
        "turnering": data.get("turnering", {}).get("navn"),
        "oppdatert": data.get("oppdatert"),
        "stilling": [{"plass": s["plass"], "navn": s["navn"], "poeng": s["poeng"]} for s in stilling],
        "toppscorere": [{"navn": x["navn"], "mal": x["antall"]} for x in topp_sc],
        "assist": [{"navn": x["navn"], "assist": x["antall"]} for x in topp_as],
    }

    system = (
        f"Du skriver manus til en norsk fotballpodcast om en privat tippekonkurranse blant venner. "
        f"To verter, {a} og {b}, prater avslappet og humoristisk sammen. "
        f"De gar gjennom dagens stilling i konkurransen, hvem som leder, hvem som naermer seg, "
        f"og litt om toppscorere og assist. Hold en lett, vennlig tone med glimt i oyet. "
        f"Lengde: rundt 8-12 replikker totalt (ca 3-5 minutter tale). "
        f"Svar KUN med gyldig JSON: en liste av objekter med feltene 'vert' (enten '{a}' eller '{b}') "
        f"og 'tekst'. Ingen markdown, ingen forklaring. Skriv tall som ord der det er naturlig for opplesing."
    )
    user = f"Dagens data:\n{json.dumps(sammendrag, ensure_ascii=False, indent=2)}\n\nSkriv manuset na."

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 2000,
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


def eleven_tts(api_key: str, voice_id: str, tekst: str, ut: Path):
    body = json.dumps({
        "text": tekst,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.75},
    }).encode("utf-8")
    req = urllib.request.Request(f"{ELEVEN_URL}/{voice_id}", data=body, headers={
        "xi-api-key": api_key,
        "content-type": "application/json",
        "accept": "audio/mpeg",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        ut.write_bytes(r.read())


def sett_sammen(klipp: list[Path], ut: Path):
    """Konkatener MP3-klipp med ffmpeg."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for k in klipp:
            f.write(f"file '{k.resolve()}'\n")
        liste = f.name
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", liste, "-c", "copy", str(ut)],
        check=True, capture_output=True,
    )
    os.unlink(liste)


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


def main(tournament_dir: str, lag_lyd_flag: bool):
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

    print("Skriver manus med Claude ...")
    manus = claude_manus(anthropic_key, data, cfg)
    print(f"  {len(manus)} replikker.")

    pod_dir = tdir / "data" / "podcast"
    pod_dir.mkdir(parents=True, exist_ok=True)
    dato = datetime.now(timezone.utc)
    ep_id = dato.strftime("%Y%m%d")

    # Lagre manuset alltid (gratis)
    (pod_dir / f"manus-{ep_id}.json").write_text(
        json.dumps(manus, ensure_ascii=False, indent=2), encoding="utf-8")

    eleven_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    lyd_fil = None
    if lag_lyd_flag and eleven_key:
        print("Genererer lyd med ElevenLabs ...")
        stemmer = cfg.get("podcast", {}).get("stemmer") or DEFAULT_VOICES
        with tempfile.TemporaryDirectory() as tmp:
            klipp = []
            for i, m in enumerate(manus):
                vid = stemmer.get(m["vert"]) or list(stemmer.values())[i % len(stemmer)]
                kp = Path(tmp) / f"{i:03d}.mp3"
                eleven_tts(eleven_key, vid, m["tekst"], kp)
                klipp.append(kp)
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
    dirs = [a for a in args if not a.startswith("--")]
    main(dirs[0] if dirs else "tournaments/vm-2026", lyd)
