import os
import csv
import re
import requests
from datetime import datetime

INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_BASE_URL = os.getenv("INFOBIP_BASE_URL", "https://m3n6y4.api.infobip.com")

# ⚠️ À vérifier sur Infobip :
WHATSAPP_SENDER = "212700049292"              # ton numéro WhatsApp AFMA
TEMPLATE_NAME = "complement_requis_afma_v3"   # nom EXACT de ta template
TEMPLATE_LANGUAGE = "fr"                      # ou "fr_FR" si besoin

CSV_FILE = "campagne_adherents_infobip-test2.csv"  # ton fichier ; séparateur = ;
REPORT_FILE = "rapport_envoi_detaille.csv"         # rapport détaillé


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
    Retourne :
      - status_code (int)
      - api_status ("OK" ou "ERROR")
      - message_id (str ou "")
      - cout (float ou 0.0)
      - error_text (str ou "")
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

    status_code = resp.status_code
    api_status = "OK" if 200 <= status_code < 300 else "ERROR"
    message_id = ""
    cout = 0.0
    error_text = ""

    # On essaie de lire la réponse JSON proprement
    try:
        data = resp.json()
    except Exception:
        data = {}

    try:
        msg_obj = (data.get("messages") or [{}])[0]
        # Selon Infobip, price peut être là ou pas
        price_obj = msg_obj.get("price") or {}
        cout_val = price_obj.get("pricePerMessage")
        if cout_val is not None:
            cout = float(cout_val)
        message_id = msg_obj.get("messageId") or ""
    except Exception:
        pass

    if api_status == "OK":
        print(f"[OK] Message envoyé. messageId={message_id}")
    else:
        error_text = resp.text
        print(f"[ERROR] {status_code} - {error_text}")

    return status_code, api_status, message_id, cout, error_text


def run_campaign():
    if not INFOBIP_API_KEY:
        raise RuntimeError("INFOBIP_API_KEY manquant dans les variables d'environnement")

    total_with_number = 0
    total_ok = 0
    total_error = 0
    total_cost = 0.0

    # On crée le fichier rapport avec un header
    with open(REPORT_FILE, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "numero",
            "nom_adherent",
            "date_consultation",
            "frais",
            "observation",
            "status_code_http",
            "api_status",
            "message_id",
            "cout_usd",
            "timestamp_envoi",
            "error_text",
        ])

        # Lecture du fichier campagne
        with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as fcsv:
            reader = csv.DictReader(fcsv, delimiter=";")

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

                status_code, api_status, message_id, cout, error_text = send_template_message(
                    to_number=numero,
                    nom_adherent=nom_adherent,
                    date_consultation=date_consult,
                    frais_engages=frais,
                    observation=observation,
                )

                if api_status == "OK":
                    total_ok += 1
                    total_cost += cout
                else:
                    total_error += 1

                writer.writerow([
                    numero,
                    nom_adherent,
                    date_consult,
                    frais,
                    observation,
                    status_code,
                    api_status,
                    message_id,
                    cout,
                    datetime.now().isoformat(),
                    error_text,
                ])

    # --- Résumé console ---
    print("\n================= RAPPORT ENVOI =================")
    print(f"Lignes avec numéro      : {total_with_number}")
    print(f"Messages envoyés OK     : {total_ok}")
    print(f"Messages en erreur      : {total_error}")
    print(f"Coût total (approx) USD : {total_cost}")
    if total_with_number > 0:
        delivery_rate = (total_ok / total_with_number) * 100
        print(f"Taux de succès (HTTP OK): {delivery_rate:.2f}%")
    print("=================================================\n")

    print(f"[RAPPORT] Fichier généré : {REPORT_FILE}")


if __name__ == "__main__":
    run_campaign()
