#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Surveillance de nouveaux produits — version sans interface graphique,
conçue pour tourner automatiquement via GitHub Actions (ou tout autre
serveur/cron). Fait UN SEUL passage sur tous les liens de config.json,
puis s'arrête (c'est le planificateur — GitHub Actions — qui la relance
périodiquement).

Identifiants email lus depuis des variables d'environnement (jamais dans
config.json, pour ne rien exposer si le dépôt est public) :
    GMAIL_EXPEDITEUR
    GMAIL_MDP_APPLICATION
    GMAIL_DESTINATAIRE
"""

import json
import os
import re
import sys
import time
import smtplib
import hashlib
import traceback
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

ICI = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ICI, "config.json")
STATE_DIR = os.path.join(ICI, "etat_liens")

os.makedirs(STATE_DIR, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def charger_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def config_email():
    return {
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "expediteur": os.environ.get("GMAIL_EXPEDITEUR", ""),
        "mot_de_passe_application": os.environ.get("GMAIL_MDP_APPLICATION", ""),
        "destinataire": os.environ.get("GMAIL_DESTINATAIRE", ""),
    }

# ---------------------------------------------------------------------------
# Etat (produits déjà vus) par lien
# ---------------------------------------------------------------------------

def _state_path(lien_id):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", lien_id)
    return os.path.join(STATE_DIR, f"{safe}.json")

def charger_etat(lien_id):
    path = _state_path(lien_id)
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(json.load(f))

def sauvegarder_etat(lien_id, produits_ids):
    with open(_state_path(lien_id), "w", encoding="utf-8") as f:
        json.dump(sorted(produits_ids), f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Récupération de page (rapide ou navigateur headless)
# ---------------------------------------------------------------------------

def recuperer_page(url, moteur="rapide"):
    if moteur == "navigateur":
        return recuperer_page_navigateur(url)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text

def recuperer_page_navigateur(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        navigateur = p.chromium.launch(headless=True)
        try:
            page = navigateur.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            html = page.content()
        finally:
            navigateur.close()
    return html

# ---------------------------------------------------------------------------
# Extraction des produits
# ---------------------------------------------------------------------------

def _identifiant_produit(href, titre):
    base = (href or titre or "").strip()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

def extraire_produits(html, base_url, selecteur_css=""):
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    produits = []

    if selecteur_css:
        elements = soup.select(selecteur_css)
        for el in elements:
            candidats = [el] if el.name == "a" else el.find_all("a", href=True)
            if not candidats:
                continue
            a = max(candidats, key=lambda x: len(x.get_text(strip=True)))
            if not a.get("href"):
                continue
            href = requests.compat.urljoin(base_url, a["href"])
            titre = a.get_text(strip=True) or el.get_text(strip=True)[:120]
            if not titre:
                continue
            produits.append({
                "id": _identifiant_produit(href, titre),
                "titre": titre,
                "url": href,
            })
    else:
        vus_href = set()
        for a in soup.find_all("a", href=True):
            titre = a.get_text(strip=True)
            href = a["href"]
            if not titre or len(titre) < 8:
                continue
            if href in vus_href:
                continue
            if href.startswith("#") or href.startswith("javascript:"):
                continue
            vus_href.add(href)
            full_url = requests.compat.urljoin(base_url, href)
            produits.append({
                "id": _identifiant_produit(full_url, titre),
                "titre": titre,
                "url": full_url,
            })

    return produits

def urls_pages(lien):
    nb_pages = max(1, int(lien.get("nb_pages", 1)))
    modele = (lien.get("url_pagine") or "").strip()
    if nb_pages <= 1 or not modele or "{page}" not in modele:
        return [lien["url"]]
    return [modele.replace("{page}", str(n)) for n in range(1, nb_pages + 1)]

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def envoyer_email(cfg_email, sujet, corps):
    if not cfg_email["expediteur"] or not cfg_email["mot_de_passe_application"] or not cfg_email["destinataire"]:
        print("!! Email non configuré (variables d'environnement manquantes) — envoi ignoré.")
        return
    msg = MIMEText(corps, "plain", "utf-8")
    msg["Subject"] = Header(sujet, "utf-8")
    msg["From"] = cfg_email["expediteur"]
    msg["To"] = cfg_email["destinataire"]

    with smtplib.SMTP(
        cfg_email["smtp_server"], cfg_email["smtp_port"], timeout=20, local_hostname="localhost"
    ) as server:
        server.starttls()
        server.login(cfg_email["expediteur"], cfg_email["mot_de_passe_application"])
        server.sendmail(cfg_email["expediteur"], [cfg_email["destinataire"]], msg.as_bytes())

def envoyer_alerte(nom_lien, url_lien, nouveaux, cfg_email):
    sujet = f"Nouveau(x) produit(s) : {nom_lien}"
    lignes = [f"Nouveaux produits detectes sur : {nom_lien}", f"Page surveillee : {url_lien}", ""]
    for p in nouveaux:
        lignes.append(f"- {p['titre']}\n  {p['url']}")
    corps = "\n".join(lignes)
    envoyer_email(cfg_email, sujet, corps)
    print(f"  -> Email envoye a {cfg_email['destinataire']}.")

# ---------------------------------------------------------------------------
# Vérification d'un lien
# ---------------------------------------------------------------------------

def verifier_lien(lien, cfg_email):
    nom = lien.get("nom") or lien["url"]
    print(f"[{nom}]")
    try:
        urls = urls_pages(lien)
        moteur = lien.get("moteur_rendu", "rapide")
        produits = []
        for i, url_page in enumerate(urls):
            html = recuperer_page(url_page, moteur=moteur)
            produits.extend(extraire_produits(html, url_page, lien.get("selecteur_css", "")))
            if i < len(urls) - 1:
                time.sleep(1)

        vus = {}
        for p in produits:
            vus[p["id"]] = p
        produits = list(vus.values())

        ids_actuels = {p["id"] for p in produits}
        ids_connus = charger_etat(lien["id"])
        premiere_fois = len(ids_connus) == 0
        nouveaux = [p for p in produits if p["id"] not in ids_connus]

        sauvegarder_etat(lien["id"], ids_actuels | ids_connus)

        if premiere_fois:
            print(f"  {len(produits)} produit(s) reference(s) (etat initial, pas d'email).")
            return

        if nouveaux:
            print(f"  {len(nouveaux)} nouveau(x) produit(s) !")
            envoyer_alerte(nom, lien["url"], nouveaux, cfg_email)
        else:
            print(f"  Rien de nouveau ({len(produits)} produits vus).")

    except requests.exceptions.RequestException as e:
        print(f"  Erreur reseau : {e}")
    except Exception as e:
        print(f"  Erreur inattendue : {e}")
        traceback.print_exc()

# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main():
    cfg = charger_config()
    cfg_email = config_email()
    print(f"=== Passage du {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    for lien in cfg["liens"]:
        verifier_lien(lien, cfg_email)
    print("=== Termine ===")

if __name__ == "__main__":
    main()
