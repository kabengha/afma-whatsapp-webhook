# send_campaign.py
import os
import csv
import re
import json
import requests
from datetime import datetime

INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_BASE_URL = os.getenv("INFOBIP_BASE_URL", "https://m3n6y4.api.infobip.com")

# ⚠️ À vérifier sur Infobip :
WHATSAPP_SENDER = os.getenv("INFOBIP_WHATSAPP_SENDER", "212700049292")
TEMPLATE_NAME = os.getenv("INFOBIP_TEMPLATE_NAME", "complement_requis_afma_v3")
TEMPLATE_LANGUAGE = os.getenv("INFOBIP_TEMPLATE_LANGUAGE", "fr")

# Valeurs par défaut pour l’exécution en ligne de commande
DEFAULT_CSV_FILE = "campagne_adherents_infobip-test2.csv"
DEFAULT_REPORT_FILE = "rapport_envoi_detaille.csv"

REQUIRED_COLUMNS = [
    "full.name.adherent",
    "Num tele",
    "D.Consultation",
    "Frais,Engagés",
    "Observation",
]


def clean_placeholder(value: str) -> str:
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
    url = f"{INFOBIP_BASE_URL}/whatsapp/1/message/template"

    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    nom_adherent = clean_placeholder(nom_adherent)
    date_consultation = clean_placeholder(date_consultation)
    frais_engages = clean_placeholder(frais_engages)
    observation = clean_placeholder(observation)

    placeholders = [
        nom_adherent,
        date_consultation,
        frais_engages,
        observation,
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

    try:
        data = resp.json()
    except Exception:
        data = {}

    # 🔍 Debug une fois pour voir la vraie structure
    print("[DEBUG] Réponse Infobip brute:", json.dumps(data, indent=2, ensure_ascii=False))

    try:
        msg_obj = (data.get("messages") or [{}])[0]
        price_obj = msg_obj.get("price") or {}
        # Essaie plusieurs clés possibles
        for key in ("pricePerMessage", "pricePerMessageUsd", "price"):
            if key in price_obj and price_obj[key] is not None:
                cout = float(price_obj[key])
                break
        message_id = msg_obj.get("messageId") or ""
    except Exception:
        pass

    if api_status == "OK":
        print(f"[OK] Message envoyé. messageId={message_id} coût={cout}")
    else:
        error_text = resp.text
        print(f"[ERROR] {status_code} - {error_text}")

    return status_code, api_status, message_id, cout, error_text


def run_campaign(csv_path: str, report_path: str) -> dict:
    """
    Lance une campagne à partir d'un fichier CSV donné.
    Écrit un fichier de rapport.
    Retourne un dict récapitulatif pour l'interface.
    """
    if not INFOBIP_API_KEY:
        raise RuntimeError("INFOBIP_API_KEY manquant dans les variables d'environnement")

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV introuvable : {csv_path}")

    total_with_number = 0
    total_ok = 0
    total_error = 0
    total_cost = 0.0

    with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as fcsv:
        reader = csv.DictReader(fcsv, delimiter=";")

        # ✅ Vérifier les colonnes obligatoires
        cols = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in cols]
        if missing:
            raise ValueError(
                f"Colonnes manquantes dans le CSV : {', '.join(missing)}. "
                f"Colonnes trouvées : {', '.join(cols)}"
            )

        # Créer le rapport
        with open(report_path, mode="w", encoding="utf-8", newline="") as freport:
            writer = csv.writer(freport)
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

            for row in reader:
                nom_adherent = (row.get("full.name.adherent") or "").strip()
                numero = (row.get("Num tele") or "").strip()
                date_consult = (row.get("D.Consultation") or "").strip()
                frais = (row.get("Frais,Engagés") or "").strip()
                observation = (row.get("Observation") or "").strip()

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

    summary = {
        "csv_path": csv_path,
        "report_path": report_path,
        "total_with_number": total_with_number,
        "total_ok": total_ok,
        "total_error": total_error,
        "total_cost": total_cost,
        "timestamp": datetime.now().isoformat(),
    }

    print("\n================= RAPPORT ENVOI =================")
    print(f"Lignes avec numéro      : {total_with_number}")
    print(f"Messages envoyés OK     : {total_ok}")
    print(f"Messages en erreur      : {total_error}")
    print(f"Coût total (approx) USD : {total_cost}")
    print("=================================================\n")
    print(f"[RAPPORT] Fichier généré : {report_path}")

    return summary


if __name__ == "__main__":
    # Mode CLI pour garder ton usage actuel
    summary = run_campaign(DEFAULT_CSV_FILE, DEFAULT_REPORT_FILE)
    print("Résumé:", summary)
