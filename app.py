import os
import json
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify

from salesforce_client import (
    get_salesforce_session,
    create_case,
    upload_document_for_case,
    SalesforceError,
)

app = Flask(__name__)

# ============================
#  Config Infobip (via env vars)
# ============================
INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_BASE_URL = os.getenv("INFOBIP_BASE_URL", "https://m3n6y4.api.infobip.com")

# ============================
#  Stockage en mémoire
# ============================

# Historique des messages par numéro
# { phone_number: [ { message_data }, ... ] }
MESSAGE_STORE: dict = {}

# Cache des Cases Salesforce créés par numéro
# { phone_number: { "case_id": "...", "last_ts": "2025-11-16T10:26:07.000+0000" } }
CASE_STORE: dict = {}

# Fenêtre de 2h pour considérer une "conversation active"
CASE_WINDOW = timedelta(hours=2)


# ============================
#  Helpers généraux
# ============================

def parse_infobip_timestamp(ts: str) -> datetime | None:
    """
    Parse un timestamp Infobip du type :
    '2025-11-16T10:26:07.000+0000'
    """
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        print(f"[WARN] Impossible de parser le timestamp : {ts}")
        return None


def has_active_window(phone: str, current_ts_str: str) -> bool:
    """
    Retourne True si ce numéro a déjà un message précédent
    dans les 2 dernières heures (avant le message courant).
    """
    messages = MESSAGE_STORE.get(phone, [])
    if not messages:
        return False

    current_ts = parse_infobip_timestamp(current_ts_str)
    if current_ts is None:
        return False

    # On regarde le DERNIER message déjà enregistré pour ce numéro
    last_msg = messages[-1]
    last_ts_str = last_msg.get("timestamp")
    last_ts = parse_infobip_timestamp(last_ts_str)

    if last_ts is None:
        return False

    return (current_ts - last_ts) <= CASE_WINDOW


def store_in_memory(phone, msg_type, text=None, doc_url=None, timestamp=None):
    """Stocke les messages reçus en mémoire (temporaire)."""

    entry = {
        "type": msg_type,
        "text": text,
        "doc_url": doc_url,
        "timestamp": timestamp,
    }

    if phone not in MESSAGE_STORE:
        MESSAGE_STORE[phone] = []

    MESSAGE_STORE[phone].append(entry)

    print(f"[STORE] Message ajouté pour {phone}: {entry}")
    print(f"[STORE] Total messages pour {phone}: {len(MESSAGE_STORE[phone])}")


def get_case_for_phone(session, phone: str, nom: str | None, received_at: str) -> str:
    """
    Retourne l'ID du Case à utiliser pour ce numéro.

    - Si fenêtre < 2h et un Case existe déjà en mémoire → réutiliser ce Case
    - Sinon → créer un nouveau Case dans Salesforce, l'enregistrer dans CASE_STORE,
      puis le retourner
    """
    active = has_active_window(phone, received_at)
    cached = CASE_STORE.get(phone)

    # Si fenêtre active et on a déjà un Case pour ce numéro → on réutilise
    if active and cached and cached.get("case_id"):
        print(f"[CASE] Réutilisation du Case existant pour {phone}: {cached['case_id']}")
        # On met à jour la dernière activité
        cached["last_ts"] = received_at
        return cached["case_id"]

    # Sinon, on crée un nouveau Case dans Salesforce
    print(f"[CASE] Création d'un nouveau Case pour {phone} (active_window={active}, cached={bool(cached)})")
    case_id = create_case(session, phone=phone, nom=nom)

    # On met à jour le cache
    CASE_STORE[phone] = {
        "case_id": case_id,
        "last_ts": received_at,
    }

    print(f"[CASE] Nouveau Case créé pour {phone}: {case_id}")
    return case_id


def normalize_infobip_media_url(raw_url: str) -> str:
    """
    Infobip envoie souvent des URLs https://api.infobip.com/...
    mais ton compte utilise un host dédié (INFOBIP_BASE_URL).

    On garde le chemin /whatsapp/... et on remplace juste le domaine.
    """
    if not raw_url:
        return raw_url

    marker = "/whatsapp"
    if marker in raw_url:
        _, path = raw_url.split(marker, 1)
        return f"{INFOBIP_BASE_URL}{marker}{path}"

    return raw_url


def download_file(url: str, suggested_filename: str | None = None) -> tuple[bytes | None, str]:
    """
    Télécharge un fichier depuis une URL (doc/image Infobip) avec Auth API Key.
    Retourne (file_bytes, filename) ou (None, "") en cas d'erreur.
    """
    if not url:
        return None, ""

    final_url = normalize_infobip_media_url(url)
    print(f"[DOWNLOAD] URL finale utilisée pour Infobip : {final_url}")

    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Accept": "*/*",
    }

    try:
        resp = requests.get(final_url, headers=headers, timeout=20)
        resp.raise_for_status()

        # --- Déduire l'extension depuis Content-Type ---
        content_type = resp.headers.get("Content-Type", "").lower()
        ext = ""

        if "jpeg" in content_type:
            ext = ".jpg"
        elif "jpg" in content_type:
            ext = ".jpg"
        elif "png" in content_type:
            ext = ".png"
        elif "pdf" in content_type:
            ext = ".pdf"
        elif "gif" in content_type:
            ext = ".gif"

        # Nom du fichier : priorité au caption/nom donné
        if suggested_filename:
            filename = suggested_filename
        else:
            filename = final_url.split("/")[-1] or "whatsapp-file"

        # Ajouter extension si manquante
        if ext and not filename.lower().endswith(ext):
            filename += ext

        return resp.content, filename

    except Exception as e:
        print(f"[DOWNLOAD] Erreur téléchargement fichier {final_url}: {e}")
        return None, ""


# ============================
#  Webhook Infobip
# ============================

@app.route("/webhook/infobip", methods=["GET", "POST"])
def infobip_webhook():
    if request.method == "GET":
        return "OK", 200

    # Récupérer le JSON brut
    data = request.get_json(silent=True, force=True) or {}

    print("=== RAW WEBHOOK PAYLOAD ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    results = data.get("results", [])
    if not results:
        return jsonify({"status": "no_results"}), 200

    # Session Salesforce (initialisée au premier besoin)
    sf_session = None

    # Traiter chaque message
    for msg in results:
        phone = msg.get("from") or msg.get("sender")
        received_at = msg.get("receivedAt")
        contact = msg.get("contact", {}) or {}
        contact_name = contact.get("name")

        message_obj = msg.get("message", {}) or {}
        msg_type = message_obj.get("type")

        text = None
        doc_url = None
        caption = None  # caption / nom du fichier

        # ---- TEXT ----
        if msg_type in ("TEXT", "text"):
            text = (
                message_obj.get("text")
                or message_obj.get("content", {}).get("text")
            )

        # ---- DOCUMENT ou IMAGE ----
        if msg_type in ("DOCUMENT", "document", "IMAGE", "image"):
            doc_url = (
                message_obj.get("url")
                or message_obj.get("document", {}).get("url")
                or message_obj.get("image", {}).get("url")
            )
            caption = message_obj.get("caption")

        print("----- MESSAGE REÇU -----")
        print(f"Numéro : {phone}")
        print(f"Type   : {msg_type}")
        if text:
            print(f"Texte  : {text}")
        if doc_url:
            print(f"URL doc/image : {doc_url}")
            if caption:
                print(f"Nom du fichier : {caption}")
        print(f"Timestamp : {received_at}")

        # Fenêtre 2h
        active_window = has_active_window(phone, received_at)
        print(f"[WINDOW] Conversation active (<2h) pour {phone} ? {active_window}")
        print("------------------------")

        #  Stockage mémoire
        store_in_memory(
            phone=phone,
            msg_type=msg_type,
            text=text,
            doc_url=doc_url,
            timestamp=received_at,
        )

        # Intégration Salesforce
        try:
            if sf_session is None:
                sf_session = get_salesforce_session()
                print("[SF] Session Salesforce initialisée")

            # Récupérer ou créer le Case pour ce numéro
            case_id = get_case_for_phone(
                session=sf_session,
                phone=phone,
                nom=contact_name,
                received_at=received_at,
            )

            # Si on a un document ou une image → upload vers Salesforce
            if doc_url:
                file_bytes, filename = download_file(doc_url, suggested_filename=caption)
                if file_bytes:
                    print(f"[SF] Upload du document pour le Case {case_id}, filename={filename}")
                    link_id = upload_document_for_case(
                        session=sf_session,
                        case_id=case_id,
                        file_bytes=file_bytes,
                        filename=filename,
                        title=f"Whatsapp - {phone}",
                    )
                    print(f"[SF] Document lié au Case {case_id} via ContentDocumentLink {link_id}")
                else:
                    print(f"[SF] Aucun fichier téléchargé pour {doc_url}, upload ignoré.")


        except SalesforceError as e:
            print(f"[SF][ERROR] Erreur Salesforce: {e}")
        except Exception as e:
            print(f"[SF][ERROR] Exception inattendue: {e}")

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Dev local
    app.run(host="0.0.0.0", port=5000, debug=True)