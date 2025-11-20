import os
import csv
import re
import requests
from datetime import datetime

# ============================
#  Config Infobip
# ============================
INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_BASE_URL = os.getenv("INFOBIP_BASE_URL", "https://m3n6y4.api.infobip.com")

WHATSAPP_SENDER = "212700049292"              # ton numéro WhatsApp AFMA
TEMPLATE_NAME = "complement_requis_afma_v3"   # nom EXACT de ta template
TEMPLATE_LANGUAGE = "fr"                      # ou "fr_FR" si besoin

CSV_FILE = "campagne_adherents_infobip-test2.csv"  # ton fichier ; séparateur = ;
REPORT_FILE = "rapport_envoi_detaille.csv"

# On initialise le fichier de rapport avec l'en-tête
with open(REPORT_FILE, mode="w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([
        "numero",
        "nom_adherent",
        "date_consultation",
        "frais",
        "observation",
        "status_code",
        "status_message",
        "message_id",
        "cout_usd",
        "timestamp_envoi",
    ])


def clean_placeholder(value: str) -> str:
    """
    Nettoie une valeur avant de l'envoyer dans un placeholder Infobip :
    - supprime les retours à la ligne / tabulations
    - réduit les espaces multiples
    """
    if not value:
        return ""
    value = value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip()


def send_template_message(
    to_number: str,
    nom_adherent: str,
    date_consultation: str,
    frais_engages: str,
    observation: str,
):
    """
    Envoie UN message template WhatsApp pour UNE ligne du fichier.
    Retourne (success: bool, message_id: str | None, error_text: str | None)
    """

    url = f"{INFOBIP_BASE_URL}/whatsapp/1/message/template"

    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Nettoyage pour respecter les règles d’Infobip
    nom_adherent = clean_placeholder(nom_adherent)
    date_consultation = clean_placeholder(date_consultation)
    frais_engages = clean_placeholder(frais_engages)
    observation = clean_placeholder(observation)

    placeholders = [
        nom_adherent,       # {{1}}
        date_consultation,  # {{2}}
        frais_engages,      # {{3}}
        observation,        # {{4}}
    ]

    payload = {
        "messages": [
            {
                "from": WHATSAPP_SENDER,
                "to": to_number,
                "content": {
                    "templateName": TEMPLATE_NAME,
                    "language": TEMPLATE_LANGUAGE,
                    "templateData": {
                        "body": {
                            "placeholders": placeholders
                        }
                    }
                }
            }
        ]
    }

    print(f"[SEND] Vers {to_number} - {nom_adherent} - {date_consultation} - {frais_engages}")
    resp = requests.post(url, headers=headers, json=payload, timeout=20)

    # --- Construction de la ligne de rapport ---
    status_code = resp.status_code
    message_id = None
    cout = None
    message_status = ""

    try:
        rj = resp.json()
        message_item = (rj.get("messages") or [{}])[0]
        message_id = message_item.get("messageId")
        message_status = message_item.get("status", "")
        price = message_item.get("price") or {}
        cout = price.get("pricePerMessage")
    except Exception:
        pass

    # On ajoute une ligne dans le CSV de rapport
    with open(REPORT_FILE, mode="a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            to_number,
            nom_adherent,
            date_consultation,
            frais_engages,
            observation,
            status_code,
            message_status,
            message_id,
            cout,
            datetime.now().isoformat()
        ])

    # --- Gestion succès / erreur côté console + retour ---
    if 200 <= resp.status_code < 300:
        print(f"[OK] Message envoyé. messageId={message_id}")
        return True, message_id, None
    else:
        error_text = resp.text
        print(f"[ERROR] {resp.status_code} - {error_text}")
        return False, None, error_text


def run_campaign():
    if not INFOBIP_API_KEY:
        raise RuntimeError("INFOBIP_API_KEY manquant dans les variables d'environnement")

    total_with_number = 0
    total_ok = 0
    total_error = 0

    with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")

        for row in reader:
            nom_adherent = row["full.name.adherent"].strip()
            numero = row["Num tele"].strip()
            date_consult = row["D.Consultation"].strip()
            frais = row["Frais,Engagés"].strip()
            observation = row["Observation"].strip()

            if not numero:
                print("[SKIP] Ligne sans numéro")
                continue

            total_with_number += 1

            success, msg_id, error_text = send_template_message(
                to_number=numero,
                nom_adherent=nom_adherent,
                date_consultation=date_consult,
                frais_engages=frais,
                observation=observation,
            )

            if success:
                total_ok += 1
            else:
                total_error += 1

    # --- Résumé console ---
    print("\n================= RAPPORT ENVOI =================")
    print(f"Lignes avec numéro      : {total_with_number}")
    print(f"Messages envoyés OK     : {total_ok}")
    print(f"Messages en erreur      : {total_error}")
    print(f"Fichier de rapport      : {REPORT_FILE}")
    print("=================================================\n")


if __name__ == "__main__":
    run_campaign()
import os
import csv
import re
import requests
from datetime import datetime

# ============================
#  Config Infobip
# ============================
INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_BASE_URL = os.getenv("INFOBIP_BASE_URL", "https://m3n6y4.api.infobip.com")

WHATSAPP_SENDER = "212700049292"              # ton numéro WhatsApp AFMA
TEMPLATE_NAME = "complement_requis_afma_v3"   # nom EXACT de ta template
TEMPLATE_LANGUAGE = "fr"                      # ou "fr_FR" si besoin

CSV_FILE = "campagne_adherents_infobip-test2.csv"  # ton fichier ; séparateur = ;
REPORT_FILE = "rapport_envoi_detaille.csv"

# On initialise le fichier de rapport avec l'en-tête
with open(REPORT_FILE, mode="w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([
        "numero",
        "nom_adherent",
        "date_consultation",
        "frais",
        "observation",
        "status_code",
        "status_message",
        "message_id",
        "cout_usd",
        "timestamp_envoi",
    ])


def clean_placeholder(value: str) -> str:
    """
    Nettoie une valeur avant de l'envoyer dans un placeholder Infobip :
    - supprime les retours à la ligne / tabulations
    - réduit les espaces multiples
    """
    if not value:
        return ""
    value = value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip()


def send_template_message(
    to_number: str,
    nom_adherent: str,
    date_consultation: str,
    frais_engages: str,
    observation: str,
):
    """
    Envoie UN message template WhatsApp pour UNE ligne du fichier.
    Retourne (success: bool, message_id: str | None, error_text: str | None)
    """

    url = f"{INFOBIP_BASE_URL}/whatsapp/1/message/template"

    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Nettoyage pour respecter les règles d’Infobip
    nom_adherent = clean_placeholder(nom_adherent)
    date_consultation = clean_placeholder(date_consultation)
    frais_engages = clean_placeholder(frais_engages)
    observation = clean_placeholder(observation)

    placeholders = [
        nom_adherent,       # {{1}}
        date_consultation,  # {{2}}
        frais_engages,      # {{3}}
        observation,        # {{4}}
    ]

    payload = {
        "messages": [
            {
                "from": WHATSAPP_SENDER,
                "to": to_number,
                "content": {
                    "templateName": TEMPLATE_NAME,
                    "language": TEMPLATE_LANGUAGE,
                    "templateData": {
                        "body": {
                            "placeholders": placeholders
                        }
                    }
                }
            }
        ]
    }

    print(f"[SEND] Vers {to_number} - {nom_adherent} - {date_consultation} - {frais_engages}")
    resp = requests.post(url, headers=headers, json=payload, timeout=20)

    # --- Construction de la ligne de rapport ---
    status_code = resp.status_code
    message_id = None
    cout = None
    message_status = ""

    try:
        rj = resp.json()
        message_item = (rj.get("messages") or [{}])[0]
        message_id = message_item.get("messageId")
        message_status = message_item.get("status", "")
        price = message_item.get("price") or {}
        cout = price.get("pricePerMessage")
    except Exception:
        pass

    # On ajoute une ligne dans le CSV de rapport
    with open(REPORT_FILE, mode="a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            to_number,
            nom_adherent,
            date_consultation,
            frais_engages,
            observation,
            status_code,
            message_status,
            message_id,
            cout,
            datetime.now().isoformat()
        ])

    # --- Gestion succès / erreur côté console + retour ---
    if 200 <= resp.status_code < 300:
        print(f"[OK] Message envoyé. messageId={message_id}")
        return True, message_id, None
    else:
        error_text = resp.text
        print(f"[ERROR] {resp.status_code} - {error_text}")
        return False, None, error_text


def run_campaign():
    if not INFOBIP_API_KEY:
        raise RuntimeError("INFOBIP_API_KEY manquant dans les variables d'environnement")

    total_with_number = 0
    total_ok = 0
    total_error = 0

    with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")

        for row in reader:
            nom_adherent = row["full.name.adherent"].strip()
            numero = row["Num tele"].strip()
            date_consult = row["D.Consultation"].strip()
            frais = row["Frais,Engagés"].strip()
            observation = row["Observation"].strip()

            if not numero:
                print("[SKIP] Ligne sans numéro")
                continue

            total_with_number += 1

            success, msg_id, error_text = send_template_message(
                to_number=numero,
                nom_adherent=nom_adherent,
                date_consultation=date_consult,
                frais_engages=frais,
                observation=observation,
            )

            if success:
                total_ok += 1
            else:
                total_error += 1

    # --- Résumé console ---
    print("\n================= RAPPORT ENVOI =================")
    print(f"Lignes avec numéro      : {total_with_number}")
    print(f"Messages envoyés OK     : {total_ok}")
    print(f"Messages en erreur      : {total_error}")
    print(f"Fichier de rapport      : {REPORT_FILE}")
    print("=================================================\n")


if __name__ == "__main__":
    run_campaign()
