#!/usr/bin/env python3
"""Mensa-Coach: holt den Wochenspeiseplan zweier Esslinger Mensen und laesst ihn von Groq bewerten."""
import datetime as dt
import json
import os
import re

import requests
from bs4 import BeautifulSoup

BASIS = "https://sws2.maxmanager.xyz/"
AJAX = BASIS + "inc/ajax-php_konnektor.inc.php"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELLE = "https://api.groq.com/openai/v1/models"
MODELL = "openai/gpt-oss-120b"
MENSEN = {"flandernstrasse": "FLANDERN", "stadtmitte": "STADTMITTE"}
KATEGORIEN = ["VEGANER RENNER", "HAUPTGERICHT", "MENSA SPECIAL", "ALL TIME CLASSIC"]
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr"]

sitzung = requests.Session()
sitzung.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Referer": BASIS,
    "X-Requested-With": "XMLHttpRequest",
})


def mensa_nummern():
    """Sucht die Nummern der Mensen per Textsuche im rohen HTML der Auswahlliste."""
    html = sitzung.get(BASIS, timeout=30).text
    optionen = re.findall(r'<option[^>]*value=["\']?(\d+)["\']?[^>]*>([^<]+)<', html, re.I)
    nummern = {}
    for datei, suchwort in MENSEN.items():
        for wert, text in optionen:
            if suchwort in text.upper():
                nummern[datei] = wert
                break
        print(f"Mensa {suchwort}: Nummer {nummern.get(datei)}")
    if len(nummern) < len(MENSEN):
        print("Gefundene Auswahlliste:", optionen)
        raise SystemExit("Mensa-Nummern nicht gefunden")
    return nummern


def zahl(treffer):
    return float(treffer.group(1).replace(",", ".")) if treffer else None


def tag_lesen(loc_id, datum, montag):
    """Liest alle gewuenschten Gerichte eines Tages."""
    formular = {
        "func": "make_spl",
        "locId": loc_id,
        "date": datum.isoformat(),
        "lang": "de",
        "startThisWeek": montag.isoformat(),
        "startNextWeek": (montag + dt.timedelta(days=7)).isoformat(),
    }
    antwort = sitzung.post(AJAX, data=formular, timeout=30)
    suppe = BeautifulSoup(antwort.text, "html.parser")
    gerichte, kategorie = [], ""
    elemente = suppe.find_all(lambda t: t.name == "div" and t.get("class")
                              and ("gruppenname" in t["class"] or "splMeal" in t["class"]))
    for el in elemente:
        if "gruppenname" in el["class"]:
            kategorie = el.get_text(" ", strip=True).replace("*", "").strip()
            continue
        if not any(k in kategorie.upper() for k in KATEGORIEN):
            continue
        name_el = el.select_one(".visible-xs-block span") or el.select_one(".visible-xs-block")
        if not name_el:
            continue
        name = re.sub(r"\s*\((?:[A-Za-z0-9]{1,4}\s*,?\s*)+\)", "", name_el.get_text(" ", strip=True))
        name = re.sub(r"\s+", " ", name).strip()
        if not name or "geschlossen" in name.lower():
            continue
        text = el.get_text(" ", strip=True)
        preis_el = el.select_one(".preise-xs")
        preis = re.search(r"(\d+,\d\d)", preis_el.get_text(" ")) if preis_el else None
        preis = preis or re.search(r"(\d+,\d\d)\s*€", text)
        symbole = " ".join(f"{i.get('src', '')} {i.get('alt', '')} {i.get('title', '')}"
                           for i in el.find_all("img")).lower()
        vegan = "vegan" in symbole
        gerichte.append({
            "kategorie": kategorie,
            "name": name,
            "preis_studierende": zahl(preis),
            "vegan": vegan,
            "vegetarisch": vegan or "vegetar" in symbole,
            "kcal_100g": zahl(re.search(r"(\d+(?:[.,]\d+)?)\s*kcal", text, re.I)),
            "eiweiss_100g": zahl(re.search(r"Eiwei(?:ß|ss)[^0-9]{0,15}(\d+(?:[.,]\d+)?)", text, re.I)),
        })
    return gerichte, antwort.text


def groq_bewerten(namen):
    """Ein einziger Groq-Aufruf fuer alle Gerichte. Liefert {name: bewertung}."""
    schluessel = os.environ.get("GROQ_API_KEY", "")
    if not schluessel or not namen:
        print("Groq: kein Schluessel oder keine Gerichte, Bewertung uebersprungen")
        return {}
    kopf = {"Authorization": f"Bearer {schluessel}"}
    liste = [{"id": i, "name": n} for i, n in enumerate(namen)]
    auftrag = (
        "Du bist Ernaehrungsberater. Bewerte jedes Mensa-Gericht. Antworte nur mit JSON der Form "
        '{"bewertungen":[{"id":0,"gesundheit":7,"kcal_portion":650,"begruendung":"..."}]}. '
        "gesundheit: ganze Zahl 1-10 (10 = sehr gesund). kcal_portion: geschaetzte kcal einer "
        "ueblichen Mensaportion. begruendung: Deutsch, hoechstens 12 Woerter. "
        "Gerichte: " + json.dumps(liste, ensure_ascii=False)
    )
    nutzlast = {
        "model": MODELL,
        "messages": [{"role": "user", "content": auftrag}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "reasoning_effort": "low",
    }
    try:
        r = requests.post(GROQ_URL, headers=kopf, json=nutzlast, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:800]}")
        daten = r.json()
        nutzung = daten.get("usage", {})
        print(f"Groq-Tokens: {nutzung.get('total_tokens')} (Eingabe {nutzung.get('prompt_tokens')}, "
              f"Ausgabe {nutzung.get('completion_tokens')})")
        inhalt = json.loads(daten["choices"][0]["message"]["content"])
        ergebnis = {}
        for b in inhalt.get("bewertungen", []):
            if isinstance(b.get("id"), int) and 0 <= b["id"] < len(namen):
                ergebnis[namen[b["id"]]] = b
        print(f"Groq: {len(ergebnis)} von {len(namen)} Gerichten bewertet")
        return ergebnis
    except Exception as fehler:
        print("Groq-Fehler:", fehler)
        try:
            modelle = requests.get(GROQ_MODELLE, headers=kopf, timeout=30).json()
            print("Verfuegbare Modelle:", [m["id"] for m in modelle.get("data", [])])
        except Exception as fehler2:
            print("Modellliste nicht abrufbar:", fehler2)
        return {}


def main():
    heute = dt.date.today()
    montag = heute - dt.timedelta(days=heute.weekday())
    nummern = mensa_nummern()
    wochen = {}
    for datei, loc_id in nummern.items():
        tage = []
        for i, kurz in enumerate(WOCHENTAGE):
            datum = montag + dt.timedelta(days=i)
            gerichte, roh = tag_lesen(loc_id, datum, montag)
            print(f"{datei} {kurz} {datum}: {len(gerichte)} Gerichte")
            if not gerichte:
                print("  Antwortanfang:", roh[:500].replace("\n", " "))
            tage.append({"datum": datum.isoformat(), "wochentag": kurz, "gerichte": gerichte})
        wochen[datei] = tage

    namen = sorted({g["name"] for tage in wochen.values() for t in tage for g in t["gerichte"]})
    bewertungen = groq_bewerten(namen)
    stand = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    os.makedirs("data", exist_ok=True)
    for datei, tage in wochen.items():
        for t in tage:
            for g in t["gerichte"]:
                b = bewertungen.get(g["name"], {})
                g["gesundheit"] = b.get("gesundheit")
                g["kcal_portion"] = b.get("kcal_portion")
                g["begruendung"] = " ".join(str(b.get("begruendung", "")).split()[:12]) or None
        with open(f"data/{datei}.json", "w", encoding="utf-8") as f:
            json.dump({"stand": stand, "tage": tage}, f, ensure_ascii=False, indent=1)
        print(f"data/{datei}.json geschrieben")


if __name__ == "__main__":
    main()
