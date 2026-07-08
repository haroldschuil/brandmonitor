# Brandmonitor Vendres-Plage

Signaleringstool voor bosbrandrisico rond Camping Homair "La Plage et le Bord de mer" (Vendres, Hérault), 18 juli t/m 1 augustus 2026. Draait elke 2 uur via GitHub Actions en publiceert een mobielvriendelijke statuspagina op GitHub Pages.

**Dit is signalering, geen alarmering.** De bronnen hebben minuten tot uren vertraging. Bij direct gevaar gelden de instructies van de camping en de brandweer, en het Franse cell-broadcastsysteem FR-Alert (werkt ook op Nederlandse telefoons in het gebied, mits noodmeldingen aanstaan: Instellingen → Veiligheid en noodgevallen → Draadloze noodmeldingen).

## De vier lagen

1. **NASA FIRMS** (satelliet): actieve warmtebronnen binnen ~90 km, met afstand en kompasrichting vanaf de camping. Objectiefste bron, maar detecteert ook industriële warmte en landbouwverbranding, en satellieten komen enkele keren per dag over: een kleine, korte brand kan gemist worden.
2. **Vigilancekaart prefectuur Hérault**: dagelijks rond 18u gepubliceerd risiconiveau per sector (groen/geel/oranje/rood), geldig voor de volgende dag. De kaart laadt data via JavaScript; het script probeert die uit te lezen maar dit is niet gegarandeerd. Mislukt het, dan toont de pagina dat expliciet plus de directe link.
3. **Nieuws**: Google News RSS met vier Franstalige zoekopdrachten plus twee directe feeds (feuxdeforet.fr en France 3 Occitanie; deze feed-URL's zijn kandidaten en niet vooraf geverifieerd, de pagina toont per bron of het ophalen lukte). Gewogen trefwoorden: kernplaatsen (Vendres, Valras, Sérignan, Lespignan, Fleury enz.) wegen zwaar, departementsniveau licht.
4. **Weer** (Open-Meteo, geen key nodig): maximale windstoten, temperatuur en minimale luchtvochtigheid voor de komende 24 uur op de campinglocatie.

## Beoordelingsregels

| Niveau | Trigger |
|---|---|
| ROOD | Hotspot ≤ 15 km in de laatste 24 uur, óf nieuwsbericht met kernplaats + evacuatietaal + brand |
| ORANJE | Hotspot ≤ 40 km, óf kernplaats + brand in het nieuws |
| GEEL | Hotspot ≤ 80 km, óf brandberichten op regio-/departementsniveau (laatste 48 uur) |
| GROEN | Geen van bovenstaande |

Windstoten ≥ 60 km/u worden als verzwarende factor vermeld maar verhogen het niveau niet zelfstandig. De stralen staan in `config.json` en zijn pragmatische keuzes, geen wetenschappelijk onderbouwde drempels; pas ze gerust aan.

## Installatie

1. **Coördinaten verifiëren.** De waarden in `config.json` (43.222, 3.250) zijn een schatting. Open Google Maps, druk lang op de camping, en vervang lat/lon. Een afwijking van 1-2 km maakt voor de stralen weinig uit, maar exact is beter.
2. **Sector controleren (eenmalig).** Open https://www.risque-prevention-incendie.fr/herault/ en gebruik de knop "localisez moi" op de camping om te zien in welke van de 9 sectoren Vendres valt. Mijn vermoeden is sector 7 (Plaine viticole / Plaines littorales), maar dat heb ik niet kunnen bevestigen.
3. **Repo aanmaken** (bijv. `brandmonitor`), deze bestanden pushen.
4. **FIRMS-key aanvragen** (gratis, direct): https://firms.modaps.eosdis.nasa.gov/api/map_key/ → als secret `FIRMS_MAP_KEY` toevoegen (Settings → Secrets and variables → Actions). Zonder key draait de rest gewoon; alleen de satellietlaag is dan inactief.
5. **GitHub Pages aanzetten**: Settings → Pages → Deploy from a branch → main, map `/docs`.
6. **Optioneel e-mailalert**: secrets `GMAIL_USER` en `GMAIL_APP_PASSWORD` (app-wachtwoord, zelfde aanpak als Scraper-voor-misdaad) en variable `PAGES_URL` met de Pages-URL. Er wordt alleen gemaild bij escalatie naar ORANJE of hoger, niet bij elke run.
7. **Testen**: Actions → Brandmonitor Vendres → Run workflow. Daarna de Pages-URL op je telefoon bookmarken; de pagina ververst zichzelf elk half uur.

## Bekende beperkingen

- SDIS 34 (brandweer Hérault) en de prefecturen communiceren operationeel vooral via X/Facebook/Instagram; die zijn niet betrouwbaar automatisch uit te lezen. De statuspagina bevat daarom directe links (@Prefet34, @Prefet11).
- De gemeentesites van Vendres en Valras-Plage hebben geen bekende RSS-feed; ook hier alleen een directe link.
- GitHub Actions cron kan tot een kwartier vertragen en draait niet gegarandeerd; check de "Bijgewerkt"-tijd op de pagina.
- De vigilance-uitlezing (laag 2) is best effort. Verwacht dat de eerste run "niet automatisch uitleesbaar" toont; de link naar de officiële kaart staat er altijd bij.
