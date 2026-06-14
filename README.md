# Piggy's tippekonkurranse

Selvbygd, gratis system for fotball-tippekonkurranser. Du laster opp deltakernes
utfylte Excel-ark til GitHub, så henter systemet resultater automatisk, regner
poeng, lager statistikk og publiserer alt som en nettside på GitHub Pages — pluss
en daglig podcast.

Bygget for å gjenbrukes hvert mesterskap: **VM 2026**, **EM 2028**, og videre.

## Mappestruktur

\`\`\`
tournaments/
  vm-2026/
    tournament.json      ← konfig: navn, datoer, poengregler, API-konkurranse
    ark/                 ← legg deltakernes .xlsx-ark her (ett per person)
    data/
      manuell.json       ← felt API-et ikke gir gratis (assist, antall kort)
      fasit.json         ← auto-generert: faktiske resultater
      stilling.json      ← auto-generert: tabell + statistikk
  em-2028/               ← klar mal for neste mesterskap
scripts/                 ← parser, poengmotor, henting, podcast (delt kode)
site/                    ← nettsiden (GitHub Pages leser denne)
.github/workflows/       ← automatikken
\`\`\`

## Engangsoppsett

1. **Lag et nytt public GitHub-repo** og last opp alt innholdet her.
2. **Slå på GitHub Pages:** Settings → Pages → Source: "GitHub Actions".
3. **Legg inn API-nøkkel:** registrer gratis på https://www.api-football.com
   → Settings → Secrets and variables → Actions → New secret:
   navn `API_FOOTBALL_KEY`, verdi = nøkkelen din.
4. **(Valgfritt) Podcast:** legg til `ANTHROPIC_API_KEY` og `ELEVENLABS_API_KEY`
   som secrets for å skru på auto-generert manus + lyd.

## Slik bruker du det

- **Legg til en deltaker:** last opp arket deres til `tournaments/vm-2026/ark/`.
  Filnavnet blir navnet i tabellen (f.eks. `Simon.xlsx` → "Simon").
- **Alt annet skjer av seg selv:** hver gang du laster opp, og hver natt under
  mesterskapet, kjører automatikken og oppdaterer siden.
- **Fyll inn manuelle felt** (antall kort) i `data/manuell.json` når de
  er kjent — typisk mot slutten.
- **Nytt mesterskap?** Kopier `em-2028/`-mappen, juster `tournament.json`
  (særlig `api_football_league`: 1 = VM, 4 = EM), og legg inn ark.

## Poengregler

Definert per turnering i `tournament.json`. Standard (fra Excel-malen):
gruppespill 1p, gruppevinner 3p, 16-dels 1p, 8-dels 2p, kvart 3p, semi 4p,
bronselag 1p, bronsevinner 3p, finalelag 5p, vinner 10p, toppscorer 10p,
assist 7p, antall mål 20p, antall kort 20p. Maks 294p.

## Hva som auto-hentes vs. fylles manuelt

Auto fra API-Football: kampresultater, gruppevinnere, hele sluttspillstreet,
finale-/bronsevinner, topp 10 toppscorere, topp 10 assist og antall mål.

Manuelt (ikke gratis/pålitelig): antall kort totalt. Assist hentes automatisk,
men kan overstyres i manuell.json hvis kildene er uenige. Bekreft også
toppscorer mot offisielle tiebreak-regler helt på slutten.
