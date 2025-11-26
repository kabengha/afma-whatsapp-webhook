import os
import json
import csv
from datetime import datetime, timedelta

import requests
from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    render_template_string,
    redirect,
    url_for,
    session,
    flash,
)
from functools import wraps

from salesforce_client import (
    get_salesforce_session,
    create_case,
    upload_document_for_case,
    update_case_status,
    SalesforceError,
)
from send_campaign import run_campaign  # 👈 nouvelle import

app = Flask(__name__)

# ============================
#  Config login / interface
# ============================
app.secret_key = os.getenv("APP_SECRET_KEY", "change-me-please")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "AFMA25@@")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
REPORT_DIR = os.getenv("REPORT_DIR", "reports")
HISTORY_FILE = os.getenv("HISTORY_FILE", "campaign_history.jsonl")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

# ============================
#  Config Infobip
# ============================
INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_BASE_URL = os.getenv("INFOBIP_BASE_URL", "https://m3n6y4.api.infobip.com")
INFOBIP_WHATSAPP_SENDER = os.getenv("INFOBIP_WHATSAPP_SENDER", "212700049292")

# ============================
#  Base de données campagne (CSV)
# ============================

# phone -> [rows...]
CLIENT_ROWS_BY_PHONE: dict[str, list[dict]] = {}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def load_client_db(csv_path: str | None = None):
    """
    Charge le fichier CSV de campagne en mémoire.
    - Une ligne = un dossier (même si numéro dupliqué)
    - On range par téléphone : phone -> [rows...]
    """
    global CLIENT_ROWS_BY_PHONE

    if csv_path is None:
        # ⚠️ par défaut on pointe sur ton fichier de campagne
        csv_path = os.getenv("CLIENT_CSV_PATH", "campagne_adherents_infobip-test2.csv")

    CLIENT_ROWS_BY_PHONE = {}

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                phone = (
                    row.get("Num tele")
                    or row.get("Telephone")
                    or row.get("Téléphone")
                    or ""
                )
                phone = str(phone).strip()
                if not phone:
                    continue

                CLIENT_ROWS_BY_PHONE.setdefault(phone, []).append(row)

        total_rows = sum(len(v) for v in CLIENT_ROWS_BY_PHONE.values())
        print(f"[CLIENT_DB] Chargé {total_rows} lignes depuis {csv_path}")
    except FileNotFoundError:
        print(
            f"[CLIENT_DB][WARN] Fichier {csv_path} introuvable. "
            f"Pas de données campagne en mémoire."
        )
    except Exception as e:
        print(f"[CLIENT_DB][ERROR] Erreur chargement {csv_path}: {e}")


def extract_name_from_row(row: dict) -> str | None:
    """
    Récupère le nom complet de l'adhérent depuis une ligne CSV.
    Pour toi : colonne 'full.name.adherent'.
    """
    if not row:
        return None

    for col in [
        "full.name.adherent",  # 👈 ton cas principal
        "Nom",
        "Nom complet",
        "FullName",
    ]:
        val = row.get(col)
        if val:
            return str(val).strip()

    return None


def extract_company_from_row(row: dict) -> str | None:
    """
    Récupère le nom de l'entreprise depuis une ligne CSV.
    Pour toi : colonne 'Nom.Client'.
    """
    if not row:
        return None

    for col in [
        "Nom.Client",  # 👈 ton vrai nom de colonne
        "Entreprise",
        "Nom entreprise",
        "Raison sociale",
        "Company",
    ]:
        val = row.get(col)
        if val:
            return str(val).strip()

    return None


# ============================
#  Stockage en mémoire
# ============================

MESSAGE_STORE: dict = {}  # { phone_number: [ { message_data }, ... ] }
CASE_STORE: dict = {}     # { phone_number: { "case_id": "...", "last_ts": "..." } }

CASE_WINDOW = timedelta(hours=2)  # fenêtre active 2h


# ============================
#  Helpers généraux
# ============================

def parse_infobip_timestamp(ts: str) -> datetime | None:
    """Parse un timestamp Infobip du type '2025-11-16T10:26:07.000+0000'."""
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


def get_case_for_phone(session, phone: str, nom: str | None, entreprise: str | None,
                       received_at: str) -> str:
    """
    Retourne l'ID du Case à utiliser pour ce numéro.
    - Si fenêtre < 2h et un Case existe déjà en mémoire → réutiliser
    - Sinon → créer un nouveau Case dans Salesforce
    """
    active = has_active_window(phone, received_at)
    cached = CASE_STORE.get(phone)

    if active and cached and cached.get("case_id"):
        print(f"[CASE] Réutilisation du Case existant pour {phone}: {cached['case_id']}")
        cached["last_ts"] = received_at
        return cached["case_id"]

    print(
        f"[CASE] Création d'un nouveau Case pour {phone} "
        f"(active_window={active}, cached={bool(cached)})"
    )
    case_id = create_case(session, phone=phone, nom=nom, entreprise=entreprise)

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

        content_type = resp.headers.get("Content-Type", "").lower()
        ext = ""

        if "jpeg" in content_type or "jpg" in content_type:
            ext = ".jpg"
        elif "png" in content_type:
            ext = ".png"
        elif "pdf" in content_type:
            ext = ".pdf"
        elif "gif" in content_type:
            ext = ".gif"

        if suggested_filename:
            filename = suggested_filename
        else:
            filename = final_url.split("/")[-1] or "whatsapp-file"

        if ext and not filename.lower().endswith(ext):
            filename += ext

        return resp.content, filename

    except Exception as e:
        print(f"[DOWNLOAD] Erreur téléchargement fichier {final_url}: {e}")
        return None, ""


def send_ack_message(phone: str):
    """Envoie un message WhatsApp simple d'accusé de réception."""
    if not (INFOBIP_API_KEY and INFOBIP_BASE_URL and INFOBIP_WHATSAPP_SENDER):
        print("[ACK] Variables Infobip manquantes, ack non envoyé.")
        return

    url = f"{INFOBIP_BASE_URL}/whatsapp/1/message/text"
    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "from": INFOBIP_WHATSAPP_SENDER,
        "to": phone,
        "content": {
            "text": (
                "Bonjour,\n\n"
                "Nous vous remercions pour l’envoi de votre complément de dossier.\n"
                "Votre document a bien été reçu et sera traité dans les plus brefs délais.\n\n"
                "Vous pouvez suivre le traitement de votre dossier via l’application mobile ou le portail Web.\n\n"
                "Cordialement."
            )
        },
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[ACK] Ack envoyé à {phone}")
    except Exception as e:
        print(
            f"[ACK][ERROR] Impossible d'envoyer l'ack à {phone}: {e} - "
            f"{getattr(resp, 'text', '')}"
        )


# ============================
#  Auth / Interface HTML
# ============================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("dashboard"))
        else:
            flash("Identifiants incorrects", "error")

    html = """
    <!doctype html>
    <html>
    <head><title>Login campagne WhatsApp</title></head>
    <body>
      <h1>Login</h1>
      {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        <ul>
        {% for category, msg in messages %}
          <li style="color:red;">{{ msg }}</li>
        {% endfor %}
        </ul>
      {% endif %}
      {% endwith %}
      <form method="post">
        <label>Utilisateur:</label>
        <input type="text" name="username"><br>
        <label>Mot de passe:</label>
        <input type="password" name="password"><br><br>
        <button type="submit">Se connecter</button>
      </form>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def append_history(entry: dict):
    entry = dict(entry)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history(limit: int = 20):
    if not os.path.exists(HISTORY_FILE):
        return []
    lines = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except Exception:
                continue
    lines.reverse()
    return lines[:limit]


@app.route("/")
@login_required
def dashboard():
    history = load_history()

    html = """
    <!doctype html>
    <html>
    <head>
      <title>Console campagne WhatsApp AFMA</title>
    </head>
    <body>
      <h1>Campagnes WhatsApp AFMA</h1>
      <p>Connecté en tant que {{ session.username }}</p>
      <p><a href="{{ url_for('logout') }}">Se déconnecter</a></p>

      {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        <ul>
        {% for category, msg in messages %}
          <li style="color:{% if category == 'error' %}red{% else %}green{% endif %};">
            {{ msg }}
          </li>
        {% endfor %}
        </ul>
      {% endif %}
      {% endwith %}

      <h2>Lancer une nouvelle campagne</h2>
      <form method="post" action="{{ url_for('run_campaign_route') }}" enctype="multipart/form-data">
        <label>Fichier CSV de campagne :</label>
        <input type="file" name="csv_file" accept=".csv" required>
        <br><br>
        <button type="submit">Lancer la campagne</button>
      </form>

      <hr>
      <h2>Historique des campagnes</h2>
      {% if history %}
        <table border="1" cellpadding="5">
          <tr>
            <th>Date/heure</th>
            <th>CSV</th>
            <th>Rapport</th>
            <th>Lignes avec numéro</th>
            <th>OK</th>
            <th>Erreurs</th>
            <th>Coût total</th>
          </tr>
          {% for h in history %}
          <tr>
            <td>{{ h.timestamp }}</td>
            <td>{{ h.csv_name }}</td>
            <td>
              {% if h.report_name %}
                <a href="{{ url_for('download_dynamic_report', filename=h.report_name) }}">Télécharger</a>
              {% else %}
                -
              {% endif %}
            </td>
            <td>{{ h.total_with_number }}</td>
            <td>{{ h.total_ok }}</td>
            <td>{{ h.total_error }}</td>
            <td>{{ h.total_cost }}</td>
          </tr>
          {% endfor %}
        </table>
      {% else %}
        <p>Aucune campagne enregistrée pour le moment.</p>
      {% endif %}
    </body>
    </html>
    """
    return render_template_string(html, history=history)


@app.route("/run-campaign", methods=["POST"])
@login_required
def run_campaign_route():
    file = request.files.get("csv_file")
    if not file:
        flash("Aucun fichier CSV reçu.", "error")
        return redirect(url_for("dashboard"))

    if not file.filename.lower().endswith(".csv"):
        flash("Veuillez uploader un fichier .csv", "error")
        return redirect(url_for("dashboard"))

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = file.filename.replace(" ", "_")
    csv_name = f"{timestamp_str}_{safe_name}"
    csv_path = os.path.join(UPLOAD_DIR, csv_name)

    file.save(csv_path)

    # 🔄 Met à jour la base en mémoire pour le webhook
    load_client_db(csv_path)

    report_name = f"rapport_{timestamp_str}.csv"
    report_path = os.path.join(REPORT_DIR, report_name)

    try:
        summary = run_campaign(csv_path, report_path)

        summary["csv_name"] = csv_name
        summary["report_name"] = report_name
        append_history(summary)

        flash(
            f"Campagne lancée. OK: {summary['total_ok']}, erreurs: {summary['total_error']}, coût total: {summary['total_cost']}",
            "success",
        )
    except Exception as e:
        flash(f"Erreur lors de la campagne : {e}", "error")
        return redirect(url_for("dashboard"))

    return redirect(url_for("dashboard"))


@app.route("/download-report/<path:filename>")
@login_required
def download_dynamic_report(filename):
    filename = os.path.basename(filename)
    file_path = os.path.join(REPORT_DIR, filename)
    if not os.path.exists(file_path):
        return "Fichier non trouvé", 404
    return send_file(file_path, as_attachment=True)


# ============================
#  Webhook Infobip
# ============================

@app.route("/webhook/infobip", methods=["GET", "POST"])
def infobip_webhook():
    if request.method == "GET":
        return "OK", 200

    data = request.get_json(silent=True, force=True) or {}

    print("=== RAW WEBHOOK PAYLOAD ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    results = data.get("results", [])
    if not results:
        return jsonify({"status": "no_results"}), 200

    sf_session = None

    for msg in results:
        if not msg.get("integrationType") or "message" not in msg:
            print("[SKIP] Évènement de statut (delivery/seen), ignoré pour Salesforce.")
            continue

        phone = msg.get("from") or msg.get("sender")
        received_at = msg.get("receivedAt")
        contact = msg.get("contact", {}) or {}
        contact_name = contact.get("name")

        rows_for_phone = CLIENT_ROWS_BY_PHONE.get(phone, [])
        row_for_case = rows_for_phone[0] if rows_for_phone else {}
        excel_full_name = extract_name_from_row(row_for_case)
        excel_company = extract_company_from_row(row_for_case)

        print(
            f"[CLIENT_DB] Phone={phone} -> nom_excel={excel_full_name}, "
            f"entreprise_excel={excel_company}, nom_whatsapp={contact_name}"
        )

        message_obj = msg.get("message", {}) or {}
        msg_type = message_obj.get("type")

        text = None
        doc_url = None
        caption = None

        if msg_type in ("TEXT", "text"):
            text = (
                message_obj.get("text")
                or message_obj.get("content", {}).get("text")
            )

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

        active_window = has_active_window(phone, received_at)
        print(f"[WINDOW] Conversation active (<2h) pour {phone} ? {active_window}")
        print("------------------------")

        store_in_memory(
            phone=phone,
            msg_type=msg_type,
            text=text,
            doc_url=doc_url,
            timestamp=received_at,
        )

        try:
            if sf_session is None:
                sf_session = get_salesforce_session()
                print("[SF] Session Salesforce initialisée")

            case_id = get_case_for_phone(
                session=sf_session,
                phone=phone,
                nom=excel_full_name or contact_name,
                entreprise=excel_company,
                received_at=received_at,
            )

            # Si on a un document ou une image → upload vers Salesforce
            if doc_url:
                file_bytes, filename = download_file(doc_url, suggested_filename=caption)
                if file_bytes:
                    print(
                        f"[SF] Upload du document pour le Case {case_id}, "
                        f"filename={filename}"
                    )
                    link_id = upload_document_for_case(
                        session=sf_session,
                        case_id=case_id,
                        file_bytes=file_bytes,
                        filename=filename,
                        title=f"Whatsapp - {phone}",
                    )
                    print(
                        f"[SF] Document lié au Case {case_id} via "
                        f"ContentDocumentLink {link_id}"
                    )

                    try:
                        update_case_status(sf_session, case_id, "Nouvelle demande")
                        print(
                            f"[SF] Statut du Case {case_id} remis à 'Nouvelle demande'"
                        )
                    except SalesforceError as e:
                        print(
                            f"[SF][ERROR] Impossible de mettre à jour le statut "
                            f"du Case {case_id}: {e}"
                        )

                    send_ack_message(phone)
                else:
                    print(
                        f"[SF] Aucun fichier téléchargé pour {doc_url}, "
                        f"upload ignoré."
                    )

        except SalesforceError as e:
            print(f"[SF][ERROR] Erreur Salesforce: {e}")
        except Exception as e:
            print(f"[SF][ERROR] Exception inattendue: {e}")

    return jsonify({"status": "ok"}), 200


# ============================
#  Démarrage / init
# ============================

load_client_db()

if __name__ == "__main__":
    load_client_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
