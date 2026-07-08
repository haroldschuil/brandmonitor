#!/usr/bin/env python3
"""
Brandmonitor Vendres-Plage
Signaleringstool voor bosbrandrisico rond Camping Homair La Plage et le
Bord de mer (Vendres, Herault). Combineert vier lagen:

  1. NASA FIRMS      - satellietdetectie van actieve branden (objectief)
  2. Vigilancekaart  - dagelijks risiconiveau per sector, prefectuur Herault
  3. Nieuws          - Google News RSS + directe feeds, gewogen trefwoorden
  4. Weer            - windstoten, temperatuur, luchtvochtigheid (Open-Meteo)

Output: docs/index.html (GitHub Pages) + docs/state.json.
Optioneel e-mailalert bij escalatie (Gmail SMTP via secrets).

Alleen standaardbibliotheek, geen dependencies.
"""

import csv
import html
import io
import json
import math
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
UA = {"User-Agent": "Mozilla/5.0 (brandmonitor; persoonlijk gebruik)"}
NIVEAUS = ["GROEN", "GEEL", "ORANJE", "ROOD"]
KLEUREN = {"GROEN": "#1a7a3c", "GEEL": "#b58900", "ORANJE": "#d2691e", "ROOD": "#b3161b"}


def nu():
    return datetime.now(TZ)


def haal(url, timeout=25, binair=False):
    """GET met nette foutafhandeling. Retourneert (data, fout)."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return (data if binair else data.decode("utf-8", errors="replace")), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def kompas(lat1, lon1, lat2, lon2):
    """Richting van punt 1 (camping) naar punt 2 (hotspot), NL-afkortingen."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    graden = (math.degrees(math.atan2(x, y)) + 360) % 360
    richtingen = ["N", "NO", "O", "ZO", "Z", "ZW", "W", "NW"]
    return richtingen[int((graden + 22.5) // 45) % 8]


# ---------------------------------------------------------------- FIRMS ----

def firms_hotspots(cfg, map_key):
    """Actieve hotspots binnen de bbox, met afstand tot de camping."""
    lat = cfg["locatie"]["lat"]
    lon = cfg["locatie"]["lon"]
    half_km = cfg["firms"]["bbox_halfbreedte_km"]
    dlat = half_km / 111.0
    dlon = half_km / (111.0 * math.cos(math.radians(lat)))
    bbox = f"{lon - dlon:.4f},{lat - dlat:.4f},{lon + dlon:.4f},{lat + dlat:.4f}"

    hotspots, fouten = [], []
    for bron in cfg["firms"]["bronnen"]:
        url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
               f"{map_key}/{bron}/{bbox}/{cfg['firms']['dagen']}")
        tekst, fout = haal(url, timeout=40)
        if fout:
            fouten.append(f"{bron}: {fout}")
            continue
        if tekst.strip().lower().startswith("invalid"):
            fouten.append(f"{bron}: {tekst.strip()[:120]}")
            continue
        try:
            for rij in csv.DictReader(io.StringIO(tekst)):
                hlat = float(rij["latitude"])
                hlon = float(rij["longitude"])
                dist = haversine(lat, lon, hlat, hlon)
                acq = f"{rij.get('acq_date', '')} {rij.get('acq_time', '').zfill(4)}"
                try:
                    t_utc = datetime.strptime(acq, "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
                    tijd = t_utc.astimezone(TZ).strftime("%d-%m %H:%M")
                except ValueError:
                    tijd = acq
                hotspots.append({
                    "lat": hlat, "lon": hlon,
                    "afstand_km": round(dist, 1),
                    "richting": kompas(lat, lon, hlat, hlon),
                    "tijd_lokaal": tijd,
                    "bron": bron.replace("_NRT", ""),
                    "frp": rij.get("frp", ""),
                    "confidence": rij.get("confidence", ""),
                })
        except Exception as e:
            fouten.append(f"{bron}: parsefout {e}")
    hotspots.sort(key=lambda h: h["afstand_km"])
    return hotspots, fouten


# ----------------------------------------------------------------- feeds ----

def parse_feed(xml_tekst, bronnaam):
    """Minimale RSS 2.0 / Atom-parser. Retourneert lijst items."""
    items = []
    try:
        root = ET.fromstring(xml_tekst)
    except ET.ParseError:
        return items
    ns_atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):  # RSS 2.0
        titel = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        beschr = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        items.append({"titel": titel, "link": link, "pub": pub,
                      "tekst": f"{titel} {beschr}", "bron": bronnaam})
    for it in root.iter(f"{ns_atom}entry"):  # Atom
        titel = (it.findtext(f"{ns_atom}title") or "").strip()
        le = it.find(f"{ns_atom}link")
        link = le.get("href", "") if le is not None else ""
        pub = (it.findtext(f"{ns_atom}updated") or "").strip()
        items.append({"titel": titel, "link": link, "pub": pub,
                      "tekst": titel, "bron": bronnaam})
    return items


def parse_pubdate(pub):
    """Probeert RFC 822 en ISO 8601. Retourneert datetime of None."""
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            d = datetime.strptime(pub, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(pub.replace("Z", "+00:00"))
    except ValueError:
        return None


def score_item(tekst, kw):
    """Gewogen score plus vlaggen, zelfde principe als Misdaad Monitor.
    Uitsluitingen (bijv. Port-Vendres, Fleury-Mérogis) worden eerst
    verwijderd zodat ze geen valse kernplaats-match geven."""
    t = tekst.lower()
    for u in kw.get("uitsluitingen", []):
        t = t.replace(u, " ")
    score = 0
    vlag = {"kern": False, "regio": False, "signaal": False, "brand": False}
    for w in kw["kern_plaatsen"]:
        if w in t:
            score += 10
            vlag["kern"] = True
    for w in kw["regio_plaatsen"]:
        if w in t:
            score += 4
            vlag["regio"] = True
    for w in kw["signaal"]:
        if w in t:
            score += 4
            vlag["signaal"] = True
    for w in kw["brand"]:
        if w in t:
            score += 2
            vlag["brand"] = True
    return score, vlag


def verzamel_nieuws(cfg):
    items, feedstatus = [], []
    for q in cfg["google_news_queries"]:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + "&hl=fr&gl=FR&ceid=FR:fr")
        tekst, fout = haal(url)
        naam = f"Google News: {q[:60]}"
        if fout:
            feedstatus.append({"naam": naam, "ok": False, "detail": fout})
            continue
        gevonden = parse_feed(tekst, "Google News")
        feedstatus.append({"naam": naam, "ok": True, "detail": f"{len(gevonden)} items"})
        items.extend(gevonden)
    for feed in cfg["directe_feeds"]:
        tekst, fout = haal(feed["url"])
        if fout:
            feedstatus.append({"naam": feed["naam"], "ok": False, "detail": fout})
            continue
        gevonden = parse_feed(tekst, feed["naam"])
        feedstatus.append({"naam": feed["naam"], "ok": True, "detail": f"{len(gevonden)} items"})
        items.extend(gevonden)

    # dedupliceren op titel, scoren, alleen brandgerelateerd houden
    gezien, resultaat = set(), []
    kw = cfg["trefwoorden"]
    for it in items:
        sleutel = it["titel"].lower().strip()
        if not sleutel or sleutel in gezien:
            continue
        gezien.add(sleutel)
        score, vlag = score_item(it["tekst"], kw)
        if not vlag["brand"] and not vlag["signaal"]:
            continue
        d = parse_pubdate(it["pub"])
        uren_oud = (datetime.now(timezone.utc) - d).total_seconds() / 3600 if d else None
        it.update({"score": score, "vlag": vlag, "uren_oud": uren_oud,
                   "pub_lokaal": d.astimezone(TZ).strftime("%d-%m %H:%M") if d else it["pub"]})
        resultaat.append(it)
    resultaat.sort(key=lambda x: (-x["score"], x["uren_oud"] if x["uren_oud"] is not None else 999))
    return resultaat, feedstatus


# ------------------------------------------------------------- vigilance ----

def vigilance_niveaus(cfg):
    """Best effort: probeert de risicokaart van de prefectuur uit te lezen.
    De niveaus worden via JavaScript geladen; dit zoekt naar JSON-endpoints
    in de pagina en de bijbehorende scripts. Slaagt dit niet, dan meldt de
    pagina dat eerlijk en blijft de directe link over."""
    basis = cfg["vigilance"]["pagina"]
    resultaat = {"ok": False, "niveaus": None, "detail": ""}
    pagina, fout = haal(basis)
    if fout:
        resultaat["detail"] = f"pagina niet bereikbaar: {fout}"
        return resultaat

    kandidaten = set(re.findall(r"""["'](/[^"'\s]*(?:data|json|niveau|massif|risque)[^"'\s]*)["']""",
                                pagina, flags=re.I))
    for src in re.findall(r"""<script[^>]+src=["']([^"']+)["']""", pagina, flags=re.I)[:6]:
        js_url = urllib.parse.urljoin(basis, src)
        if urllib.parse.urlparse(js_url).netloc != urllib.parse.urlparse(basis).netloc:
            continue
        js, jfout = haal(js_url)
        if js:
            kandidaten |= set(re.findall(
                r"""["'](/[^"'\s]*(?:data|json|niveau|massif|risque)[^"'\s]*)["']""", js, flags=re.I))

    kleurwoorden = {"vert": "GROEN", "jaune": "GEEL", "orange": "ORANJE", "rouge": "ROOD"}
    for pad in list(kandidaten)[:10]:
        url = urllib.parse.urljoin(basis, pad)
        tekst, fout = haal(url, timeout=15)
        if not tekst:
            continue
        try:
            data = json.loads(tekst)
        except json.JSONDecodeError:
            continue
        platte = json.dumps(data, ensure_ascii=False).lower()
        if any(k in platte for k in kleurwoorden):
            resultaat.update(ok=True, niveaus=data,
                             detail=f"ruwe data gevonden via {url} (interpretatie handmatig controleren)")
            return resultaat
    resultaat["detail"] = ("niveaus niet automatisch uitleesbaar; "
                           "raadpleeg de kaart via de link hieronder")
    return resultaat


# ------------------------------------------------------------------ weer ----

def weer(cfg):
    lat, lon = cfg["locatie"]["lat"], cfg["locatie"]["lon"]
    url = ("https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}"
           "&hourly=wind_speed_10m,wind_gusts_10m,wind_direction_10m,"
           "temperature_2m,relative_humidity_2m"
           "&forecast_days=2&timezone=Europe%2FParis")
    tekst, fout = haal(url)
    if fout:
        return {"ok": False, "detail": fout}
    try:
        d = json.loads(tekst)["hourly"]
        # komende 24 uur vanaf nu
        start = nu().strftime("%Y-%m-%dT%H:00")
        try:
            i0 = d["time"].index(start)
        except ValueError:
            i0 = 0
        venster = slice(i0, i0 + 24)
        stoten = d["wind_gusts_10m"][venster]
        idx_max = stoten.index(max(stoten))
        return {
            "ok": True,
            "max_stoot_kmh": round(max(stoten)),
            "moment_max": d["time"][venster][idx_max][11:16],
            "richting_graden": d["wind_direction_10m"][venster][idx_max],
            "temp_max": round(max(d["temperature_2m"][venster])),
            "rv_min": round(min(d["relative_humidity_2m"][venster])),
        }
    except Exception as e:
        return {"ok": False, "detail": f"parsefout: {e}"}


# ------------------------------------------------------------ beoordeling ----

def beoordeel(cfg, hotspots, nieuws, w):
    """Regelgebaseerde beoordeling. Regels staan in de README.
    Dit is signalering, geen vervanging van officiele alarmering."""
    stralen = cfg["stralen_km"]
    redenen = []
    niveau = 0  # index in NIVEAUS

    vers_nieuws = [n for n in nieuws if n["uren_oud"] is not None and n["uren_oud"] <= 48]

    for h in hotspots:
        if h["afstand_km"] <= stralen["rood"]:
            niveau = max(niveau, 3)
            redenen.append(f"Satelliet: hotspot op {h['afstand_km']} km ({h['richting']}, {h['tijd_lokaal']})")
            break
        if h["afstand_km"] <= stralen["oranje"]:
            niveau = max(niveau, 2)
            redenen.append(f"Satelliet: hotspot op {h['afstand_km']} km ({h['richting']}, {h['tijd_lokaal']})")
            break
        if h["afstand_km"] <= stralen["geel"]:
            niveau = max(niveau, 1)
            redenen.append(f"Satelliet: hotspot op {h['afstand_km']} km ({h['richting']})")
            break

    for n in vers_nieuws:
        v = n["vlag"]
        if v["kern"] and v["signaal"] and v["brand"]:
            niveau = max(niveau, 3)
            redenen.append(f"Nieuws: evacuatietaal bij kernplaats: {n['titel'][:90]}")
        elif v["kern"] and v["brand"]:
            niveau = max(niveau, 2)
            redenen.append(f"Nieuws: brand genoemd bij kernplaats: {n['titel'][:90]}")
        elif v["brand"] and (v["regio"] or v["signaal"]):
            niveau = max(niveau, 1)

    if niveau == 1 and not any("Nieuws" in r or "Satelliet" in r for r in redenen):
        redenen.append("Nieuws: brandmeldingen in de regio (departementsniveau)")

    if w.get("ok") and w["max_stoot_kmh"] >= 60 and niveau >= 1:
        redenen.append(f"Weer verzwaart: windstoten tot {w['max_stoot_kmh']} km/u verwacht ({w['moment_max']}u)")

    if niveau == 0:
        redenen.append("Geen hotspots binnen 80 km en geen lokale brandberichten in de laatste 48 uur")
    return NIVEAUS[niveau], redenen


# ------------------------------------------------------------------ HTML ----

def render(cfg, niveau, redenen, hotspots, nieuws, vig, w, feedstatus, firms_fouten, firms_actief, degradatie=False):
    e = html.escape
    kleur = KLEUREN[niveau]
    tijd = nu().strftime("%A %d %B %Y, %H:%M")
    degradatie_html = ("<div style='background:#b3161b;color:#fff;padding:10px 14px;"
                       "text-align:center;font-weight:700'>Bronnen onbereikbaar: "
                       "niveau onbetrouwbaar, gebruik de directe links onderaan</div>"
                       if degradatie else "")

    def sectie(titel, body):
        return f'<section><h2>{e(titel)}</h2>{body}</section>'

    # hotspots
    if not firms_actief:
        hs_html = '<p class="waarschuwing">FIRMS_MAP_KEY ontbreekt: satellietlaag inactief. Zie README.</p>'
    elif hotspots:
        rijen = "".join(
            f"<tr><td>{h['afstand_km']} km</td><td>{h['richting']}</td>"
            f"<td>{e(h['tijd_lokaal'])}</td><td>{e(h['bron'])}</td>"
            f"<td><a href='https://www.google.com/maps?q={h['lat']},{h['lon']}'>kaart</a></td></tr>"
            for h in hotspots[:15])
        hs_html = ("<table><tr><th>Afstand</th><th>Richting</th><th>Waargenomen</th><th>Satelliet</th><th></th></tr>"
                   + rijen + "</table>"
                   "<p class='klein'>Satellieten detecteren warmtebronnen: ook industriële installaties en "
                   "landbouwverbranding geven hotspots. Afstand en clustering zeggen meer dan een enkele stip.</p>")
    else:
        hs_html = f"<p>Geen hotspots binnen ~{cfg['firms']['bbox_halfbreedte_km']} km in de afgelopen {cfg['firms']['dagen'] * 24} uur.</p>"
    if firms_fouten:
        hs_html += "<p class='klein'>Deels mislukt: " + e("; ".join(firms_fouten)) + "</p>"

    # nieuws
    if nieuws:
        li = "".join(
            f"<li class='{'kern' if n['vlag']['kern'] else ''}'>"
            f"<a href='{e(n['link'])}'>{e(n['titel'])}</a>"
            f"<span class='klein'> — {e(n['pub_lokaal'])}, score {n['score']}</span></li>"
            for n in nieuws[:25])
        nieuws_html = f"<ul>{li}</ul>"
    else:
        nieuws_html = "<p>Geen brandgerelateerde berichten gevonden.</p>"

    # vigilance
    vig_html = f"<p>{e(vig['detail'])}</p>"
    if vig["ok"]:
        vig_html += f"<pre class='klein'>{e(json.dumps(vig['niveaus'], ensure_ascii=False, indent=1)[:1500])}</pre>"
    vig_html += (f"<p><a class='knop' href='{e(cfg['vigilance']['pagina'])}'>Open de officiële kaart</a> "
                 "<span class='klein'>(dagelijks bijgewerkt rond 18u, geldig voor de volgende dag)</span></p>")

    # weer
    if w.get("ok"):
        weer_html = (f"<p>Komende 24 uur: windstoten tot <strong>{w['max_stoot_kmh']} km/u</strong> "
                     f"(rond {w['moment_max']}u, richting {w['richting_graden']}°), "
                     f"max {w['temp_max']}°C, minimale luchtvochtigheid {w['rv_min']}%.</p>"
                     "<p class='klein'>Sterke wind bij lage luchtvochtigheid is de combinatie die branden snel laat uitbreiden.</p>")
    else:
        weer_html = f"<p class='waarschuwing'>Weerdata niet beschikbaar: {e(w.get('detail', ''))}</p>"

    links_html = "<ul>" + "".join(
        f"<li><a href='{e(l['url'])}'>{e(l['naam'])}</a></li>" for l in cfg["vaste_links"]) + "</ul>"

    status_html = "<ul class='klein'>" + "".join(
        f"<li>{'✓' if f['ok'] else '✗'} {e(f['naam'])} — {e(f['detail'])}</li>" for f in feedstatus) + "</ul>"

    redenen_html = "".join(f"<li>{e(r)}</li>" for r in redenen)

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>Brandmonitor Vendres — {niveau}</title>
<style>
  :root {{ --accent: {kleur}; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #f5f4f0; color: #1c1c1a; line-height: 1.45; }}
  main {{ max-width: 720px; margin: 0 auto; padding: 0 14px 40px; }}
  .banner {{ background: var(--accent); color: #fff; padding: 22px 14px; }}
  .banner h1 {{ margin: 0; font-size: 1.05rem; font-weight: 600; opacity: .9; }}
  .banner .niveau {{ font-size: 2.6rem; font-weight: 800; letter-spacing: .04em; }}
  .banner .tijd {{ opacity: .85; font-size: .85rem; }}
  section {{ background: #fff; border-radius: 10px; padding: 14px 16px; margin-top: 14px; box-shadow: 0 1px 2px rgba(0,0,0,.06); }}
  h2 {{ font-size: 1rem; margin: 0 0 8px; border-bottom: 2px solid var(--accent); padding-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th, td {{ text-align: left; padding: 4px 6px; border-bottom: 1px solid #eee; }}
  ul {{ margin: 6px 0; padding-left: 18px; }}
  li {{ margin-bottom: 6px; }}
  li.kern {{ font-weight: 600; }}
  a {{ color: #0a5c8a; }}
  .klein {{ font-size: .8rem; color: #666; }}
  .waarschuwing {{ color: #b3161b; font-weight: 600; }}
  .knop {{ display: inline-block; background: var(--accent); color: #fff; padding: 8px 14px; border-radius: 6px; text-decoration: none; font-weight: 600; }}
  .nood {{ background: #1c1c1a; color: #fff; }}
  .nood a {{ color: #ffd; }}
  pre {{ overflow-x: auto; background: #f5f4f0; padding: 8px; border-radius: 6px; }}
</style>
</head>
<body>
<div class="banner">
  <main>
    <h1>Brandmonitor — {e(cfg['locatie']['naam'])}</h1>
    <div class="niveau">{niveau}</div>
    <div class="tijd">Bijgewerkt: {e(tijd)} (lokale tijd Frankrijk) · pagina ververst elk half uur</div>
  </main>
</div>
{degradatie_html}
<main>
<section>
  <h2>Waarom dit niveau</h2>
  <ul>{redenen_html}</ul>
  <p class="klein">Dit is een geautomatiseerde signalering op basis van openbare bronnen, met vertraging van
  minuten tot uren. Bij direct gevaar: volg de instructies van de camping en de brandweer, niet deze pagina.</p>
</section>
{sectie("Satellietdetectie (NASA FIRMS)", hs_html)}
{sectie("Officieel risiconiveau (prefectuur Hérault)", vig_html)}
{sectie("Nieuwsberichten (laatste 48 uur)", nieuws_html)}
{sectie("Weer op de camping", weer_html)}
{sectie("Directe bronnen", links_html)}
<section class="nood">
  <h2 style="border-color:#fff">Bij nood</h2>
  <p><strong>112</strong> (algemeen) of <strong>18</strong> (pompiers). Meld: camping Homair, Vendres-Plage,
  chemin des Montilles.</p>
  <p>FR-Alert (het Franse cell-broadcastsysteem) stuurt bij evacuaties een alarm naar alle telefoons in het
  gebied, ook Nederlandse. Controleer vóór vertrek of noodmeldingen op je Android-toestel aanstaan:
  Instellingen → Veiligheid en noodgevallen → Draadloze noodmeldingen.</p>
</section>
{sectie("Status van de bronnen", status_html)}
</main>
</body>
</html>
"""


# ------------------------------------------------------------------ mail ----

def stuur_alert(cfg, niveau, vorig, redenen, pages_url):
    if not cfg["email_alert"]["actief"]:
        return "alerts uitgeschakeld in config"
    gebruiker = os.environ.get("GMAIL_USER")
    wachtwoord = os.environ.get("GMAIL_APP_PASSWORD")
    if not gebruiker or not wachtwoord:
        return "GMAIL_USER/GMAIL_APP_PASSWORD niet gezet: geen mail verstuurd"
    drempel = NIVEAUS.index(cfg["email_alert"]["vanaf_niveau"])
    if NIVEAUS.index(niveau) < drempel or NIVEAUS.index(niveau) <= NIVEAUS.index(vorig):
        return f"geen escalatie ({vorig} → {niveau}), geen mail"
    body = (f"Niveau: {vorig} → {niveau}\n\n" + "\n".join(f"- {r}" for r in redenen)
            + f"\n\nStatuspagina: {pages_url}\nBij twijfel: receptie camping of 112.")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"{cfg['email_alert']['onderwerp_prefix']} {niveau}"
    msg["From"] = gebruiker
    msg["To"] = gebruiker
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(gebruiker, wachtwoord)
            s.send_message(msg)
        return f"alert gemaild ({vorig} → {niveau})"
    except Exception as e:
        return f"mail mislukt: {e}"


# ------------------------------------------------------------------ main ----

def main():
    hier = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(hier, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(os.path.join(hier, "docs"), exist_ok=True)
    state_pad = os.path.join(hier, "docs", "state.json")
    vorig = "GROEN"
    if os.path.exists(state_pad):
        try:
            with open(state_pad, encoding="utf-8") as f:
                vorig = json.load(f).get("niveau", "GROEN")
        except Exception:
            pass

    map_key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    firms_actief = bool(map_key)
    hotspots, firms_fouten = firms_hotspots(cfg, map_key) if firms_actief else ([], [])
    nieuws, feedstatus = verzamel_nieuws(cfg)
    vig = vigilance_niveaus(cfg)
    w = weer(cfg)

    niveau, redenen = beoordeel(cfg, hotspots, nieuws, w)

    # Datakwaliteit: een GROEN op basis van onbereikbare bronnen is niets waard
    feeds_ok = any(f["ok"] for f in feedstatus)
    firms_ok = firms_actief and len(firms_fouten) < len(cfg["firms"]["bronnen"])
    degradatie = not feeds_ok and not firms_ok
    if degradatie:
        redenen.insert(0, "LET OP: nieuws- én satellietbronnen waren niet bereikbaar; "
                          "dit niveau is onbetrouwbaar. Raadpleeg de directe bronnen.")

    pages_url = os.environ.get("PAGES_URL", "")
    mail_status = stuur_alert(cfg, niveau, vorig, redenen, pages_url)

    pagina = render(cfg, niveau, redenen, hotspots, nieuws, vig, w,
                    feedstatus, firms_fouten, firms_actief, degradatie)
    with open(os.path.join(hier, "docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(pagina)
    with open(state_pad, "w", encoding="utf-8") as f:
        json.dump({"niveau": niveau, "tijd": nu().isoformat(),
                   "redenen": redenen, "mail": mail_status}, f, ensure_ascii=False, indent=1)

    print(f"Niveau: {niveau} (was {vorig}) | mail: {mail_status}")
    for r in redenen:
        print(" -", r)


if __name__ == "__main__":
    sys.exit(main())
