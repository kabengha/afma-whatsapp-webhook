import os
import csv
import requests

INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_BASE_URL = os.getenv("INFOBIP_BASE_URL", "https://m3n6y4.api.infobip.com")

# ⚠️ À vérifier sur Infobip :
WHATSAPP_SENDER = "212700049292"            # ton numéro WhatsApp AFMA
TEMPLATE_NAME = "complement_requis_afma_v3"    # nom EXACT de ta template
TEMPLATE_LANGUAGE = "fr"                    # ou "fr_FR" si besoin

CSV_FILE = "campagne_adherents_infobip-test2.csv"  # ton fichier ; séparateur = ;


def send_template_message(
    to_number: str,
    nom_adherent: str,
    date_consultation: str,
    frais_engages: str,
    observation: str,
):
    """
    Envoie UN message template WhatsApp pour UNE ligne du fichier.
    Les placeholders correspondent à :
      {{1}} = nom_adherent
      {{2}} = date_consultation
      {{3}} = frais_engages
      {{4}} = observation
    """

    url = f"{INFOBIP_BASE_URL}/whatsapp/1/message/template"

    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    placeholders = [
        nom_adherent,       # {{1}}
        date_consultation,  # {{2}}
        frais_engages,      # {{3}}
        observation,        # {{4}}
    ]

    payload = {
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

    print(f"[SEND] Vers {to_number} - {nom_adherent} - {date_consultation} - {frais_engages}")
    resp = requests.post(url, headers=headers, json=payload, timeout=20)

    if 200 <= resp.status_code < 300:
        try:
            message_id = resp.json().get("messages", [{}])[0].get("messageId")
        except Exception:
            message_id = None
        print(f"[OK] Message envoyé. messageId={message_id}")
    else:
        print(f"[ERROR] {resp.status_code} - {resp.text}")


def run_campaign():
    if not INFOBIP_API_KEY:
        raise RuntimeError("INFOBIP_API_KEY manquant dans les variables d'environnement")

    with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")

        for row in reader:
            # ⚠️ noms EXACTS des colonnes
            nom_adherent = row["Nom.Prénom.Adhérent"].strip()
            numero = row["Num tele"].strip()
            date_consult = row["D.Consultation"].strip()
            frais = row["Frais,Engagés"].strip()
            observation = row["Observation"].strip()
            # nom_client = row.get("Nom.Client", "").strip()  # dispo si tu veux un jour {{5}}

            if not numero:
                print("[SKIP] Ligne sans numéro")
                continue

            send_template_message(
                to_number=numero,
                nom_adherent=nom_adherent,
                date_consultation=date_consult,
                frais_engages=frais,
                observation=observation,
            )


if __name__ == "__main__":
    run_campaign()
