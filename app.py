import os
import json
import csv
from datetime import datetime, timedelta
import threading


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
from send_campaign import run_campaign, PRICE_CACHE_FILE   # 👈 nouvelle import

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


AFMA_LOGO_URL = os.getenv("AFMA_LOGO_URL", "https://afma.ma/wp-content/uploads/2023/02/AFMA.png")

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
    <html lang="fr">
    <head>
      <meta charset="utf-8">
      <title>AFMA | Console WhatsApp</title>
      <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: white;
          min-height: 100vh;
          display: flex;
          align-items: center;
          justify-content: center;
          color: #1b3b5a;
        }
        .card {
          background: #ffffff;
          width: 100%;
          max-width: 420px;
          border-radius: 18px;
          padding: 32px 28px 28px;
          box-shadow: 0 18px 35px rgba(0,0,0,0.18);
        }
        .logo-wrapper {
          display: flex;
          flex-direction: column;
          align-items: center;
          margin-bottom: 24px;
        }
        .logo-img {
          width: 80px;
          height: 80px;
          border-radius: 50%;
          background: #e6f3ff;
          display: flex;
          align-items: center;
          justify-content: center;
          overflow: hidden;
          margin-bottom: 8px;
        }
        .logo-img img {
          max-width: 100%;
          max-height: 100%;
          object-fit: contain;
        }
        .logo-fallback {
          font-size: 28px;
          font-weight: 600;
          color: #0076bf;
        }
        h1 {
          font-size: 22px;
          margin-bottom: 4px;
          text-align: center;
          color: #004a7f;
        }
        .subtitle {
          font-size: 13px;
          text-align: center;
          color: #6b7b8c;
          margin-bottom: 24px;
        }
        .field {
          margin-bottom: 16px;
        }
        label {
          font-size: 13px;
          color: #4b5c70;
          display: block;
          margin-bottom: 6px;
        }
        .input-wrapper {
          display: flex;
          align-items: center;
          border: 1px solid #c7d8ea;
          border-radius: 999px;
          padding: 0 12px;
          background: #f8fbff;
        }
        .input-wrapper span.icon {
          font-size: 16px;
          margin-right: 8px;
          color: #0076bf;
        }
        .input-wrapper input {
          border: none;
          outline: none;
          background: transparent;
          padding: 10px 4px;
          width: 100%;
          font-size: 14px;
          color: #1b3b5a;
        }
        .input-wrapper input::placeholder {
          color: #9aadbf;
        }
        .remember-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 20px;
          font-size: 12px;
          color: #6b7b8c;
        }
        .remember-row label {
          display: flex;
          align-items: center;
          margin-bottom: 0;
          cursor: pointer;
        }
        .remember-row input[type="checkbox"] {
          margin-right: 6px;
        }
        .btn-primary {
          width: 100%;
          border: none;
          border-radius: 999px;
          padding: 11px 16px;
          font-size: 15px;
          font-weight: 600;
          letter-spacing: 0.5px;
          background: #0076bf;
          color: #ffffff;
          cursor: pointer;
          box-shadow: 0 8px 16px rgba(0,118,191,0.35);
          transition: transform 0.05s ease-out, box-shadow 0.05s ease-out, background 0.2s;
        }
        .btn-primary:hover {
          background: #005e9b;
          transform: translateY(-1px);
          box-shadow: 0 10px 20px rgba(0,94,155,0.35);
        }
        .btn-primary:active {
          transform: translateY(0);
          box-shadow: 0 6px 12px rgba(0,94,155,0.35);
        }
        .messages {
          margin-bottom: 12px;
        }
        .messages li {
          list-style: none;
          font-size: 13px;
          color: #d72638;
          background: #ffe6ea;
          border-radius: 10px;
          padding: 8px 10px;
          margin-bottom: 4px;
        }
        .footer-note {
          margin-top: 18px;
          text-align: center;
          font-size: 11px;
          color: #9aadbf;
        }
      </style>
    </head>
    <body>
      <div class="card">
        <div class="logo-wrapper">
          <div class="logo-img">
            {% if afma_logo_url %}
              <img src="{{ afma_logo_url }}" alt="AFMA">
            {% else %}
              <div class="logo-fallback">AF</div>
            {% endif %}
          </div>
          <h1>Console WhatsApp AFMA</h1>
          <p class="subtitle">Accès sécurisé à l’outil de campagne</p>
        </div>

        {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          <ul class="messages">
          {% for category, msg in messages %}
            <li>{{ msg }}</li>
          {% endfor %}
          </ul>
        {% endif %}
        {% endwith %}

        <form method="post">
          <div class="field">
            <label for="username">Utilisateur</label>
            <div class="input-wrapper">
              <span class="icon">👤</span>
              <input id="username" type="text" name="username" placeholder="Votre identifiant" required>
            </div>
          </div>

          <div class="field">
            <label for="password">Mot de passe</label>
            <div class="input-wrapper">
              <span class="icon">🔒</span>
              <input id="password" type="password" name="password" placeholder="Votre mot de passe" required>
            </div>
          </div>

          <div class="remember-row">
             
            <span>Accès interne AFMA</span>
          </div>

          <button type="submit" class="btn-primary">Se connecter</button>
        </form>

        <p class="footer-note">AFMA – Outil interne de campagne WhatsApp</p>
      </div>
    </body>
    </html>
    """
    return render_template_string(html, afma_logo_url=AFMA_LOGO_URL)


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
    <html lang="fr">
    <head>
      <meta charset="utf-8">
      <title>AFMA | Campagnes WhatsApp</title>
      <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #f3f7fb;
          color: #1b3b5a;
          padding: 24px;
        }
        .shell {
          max-width: 1100px;
          margin: 0 auto;
        }
        .topbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 24px;
        }
        .topbar-left {
          display: flex;
          align-items: center;
          gap: 12px;
        }
        .logo-small {
          width: 48px;
          height: 48px;
          border-radius: 50%;
          background: linear-gradient(135deg, #0076bf, #00a0e3);
          display: flex;
          align-items: center;
          justify-content: center;
          overflow: hidden;
        }
        .logo-small img {
          max-width: 90%;
          max-height: 90%;
          object-fit: contain;
          background: transparent;
        }
        .logo-small-fallback {
          font-size: 20px;
          font-weight: 600;
          color: #ffffff;
        }
        .title-main {
          font-size: 22px;
          font-weight: 600;
          color: #004a7f;
        }
        .title-sub {
          font-size: 13px;
          color: #6b7b8c;
        }
        .user-chip {
          font-size: 13px;
          padding: 8px 14px;
          border-radius: 999px;
          background: #ffffff;
          border: 1px solid #c7d8ea;
          display: inline-flex;
          align-items: center;
          gap: 8px;
        }
        .user-chip span.icon {
          font-size: 16px;
        }
        .logout-link {
          margin-left: 12px;
          font-size: 12px;
          color: #d72638;
          text-decoration: none;
        }
        .logout-link:hover {
          text-decoration: underline;
        }
        .messages {
          margin-bottom: 16px;
        }
        .messages li {
          list-style: none;
          font-size: 13px;
          padding: 8px 10px;
          border-radius: 10px;
          margin-bottom: 6px;
        }
        .messages li.error {
          background: #ffe6ea;
          color: #d72638;
        }
        .messages li.success {
          background: #e3f6e8;
          color: #207b3c;
        }
        .grid {
          display: grid;
          grid-template-columns: minmax(0, 1.2fr) minmax(0, 1.4fr);
          gap: 9px;
          align-items: flex-start;
          justify-items: start;
        }
        @media (max-width: 900px) {
          .grid {
            grid-template-columns: 1fr;
          }
        }
        .card {
          background: #ffffff;
          border-radius: 16px;
          padding: 20px 22px 18px;
          box-shadow: 0 10px 25px rgba(0,0,0,0.06);
        }
        .card h2 {
          font-size: 18px;
          margin-bottom: 6px;
          color: #004a7f;
        }
        .card p.desc {
          font-size: 13px;
          color: #6b7b8c;
          margin-bottom: 18px;
        }
        .upload-zone {
          display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            gap: 6px;

            border: 2px dashed #bdd8f3;
            border-radius: 18px;

            padding: 30px 20px;
            cursor: pointer;
            background: #f5faff;

            transition: 0.2s ease;
        }
        .upload-zone:hover {
          border-color: #0076bf;
          background: #f2f7ff;
        }
        
        .upload-icon {
          font-size: 30px;
          margin-bottom: 8px;
          color: #0076bf;
        }
        .upload-title {
          font-size: 14px;
          font-weight: 600;
          color: #004a7f;
          margin-bottom: 4px;
        }
        .upload-sub {
          font-size: 12px;
          color: #6b7b8c;
        }
        .btn-area {
            display: flex;
            justify-content: center;
            margin-top: 20px;
        }
        .btn-run {
          margin-top: 16px;
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: none;
          border-radius: 999px;
          padding: 9px 18px;
          background: #0076bf;
          color: #ffffff;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          box-shadow: 0 6px 14px rgba(0,118,191,0.35);
        }
        .btn-run span.icon {
          font-size: 16px;
        }
        .btn-run:hover {
          background: #005e9b;
        }
        table {
          width: 100%;
          border-collapse: collapse;
          font-size: 13px;
        }
        th, td {
          padding: 8px 10px;
          text-align: left;
          border-bottom: 1px solid #e0e7f1;
        }
        th {
          font-size: 12px;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: #6b7b8c;
          background: #f3f7fb;
        }
        tr:hover td {
          background: #f8fbff;
        }
        .badge-ok {
          display: inline-block;
          padding: 3px 8px;
          border-radius: 999px;
          background: #e3f6e8;
          color: #207b3c;
          font-size: 11px;
        }
        .badge-error {
          display: inline-block;
          padding: 3px 8px;
          border-radius: 999px;
          background: #ffe6ea;
          color: #d72638;
          font-size: 11px;
        }
        .report-link a {
          color: #0076bf;
          text-decoration: none;
          font-weight: 500;
        }
        .report-link a:hover {
          text-decoration: underline;
        }
        .empty-state {
          font-size: 13px;
          color: #8a9ab0;
          padding: 12px 4px 4px;
        }
      </style>
    </head>
    <body>
      <div class="shell">
        <header class="topbar">
          <div class="topbar-left">
            <div class="logo-small">
              {% if afma_logo_url %}
                <img src="{{ afma_logo_url }}" alt="AFMA">
              {% else %}
                <div class="logo-small-fallback">AF</div>
              {% endif %}
            </div>
            <div>
              <div class="title-main">Campagnes WhatsApp AFMA</div>
              <div class="title-sub">Pilotage des envois et suivi des rapports</div>
            </div>
          </div>

          <div>
            <div class="user-chip">
              <span class="icon">👤</span>
              <span>{{ session.username }}</span>
            </div>
            <a class="logout-link" href="{{ url_for('logout') }}">Se déconnecter</a>
          </div>
        </header>

        {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          <ul class="messages">
          {% for category, msg in messages %}
            <li class="{{ category }}">{{ msg }}</li>
          {% endfor %}
          </ul>
        {% endif %}
        {% endwith %}

        <main class="grid">
          <!-- Colonne gauche : nouvelle campagne -->
          <section class="card">
            <h2>Nouvelle campagne</h2>
            <p class="desc">
              Uploadez le fichier CSV fourni par AFMA pour lancer l’envoi de la campagne WhatsApp.
            </p>

            <form method="post" action="{{ url_for('run_campaign_route') }}" enctype="multipart/form-data">
              <label class="upload-zone">
                <div class="upload-icon">☁️</div>
                <div class="upload-title">Cliquer pour sélectionner votre CSV</div>
                <div class="upload-sub">Format attendu : fichier .csv séparé par « ; »</div>
                <input type="file" name="csv_file" accept=".csv" required>
              </label>

            <div class="btn-area">
              <button type="submit" class="btn-run">
                <span class="icon">▶️</span>
                <span>Lancer la campagne</span>
              </button>
              </div>
            </form>
          </section>

          <!-- Colonne droite : historique -->
          <section class="card">
            <h2>Rapport campagne</h2>
        

            {% if history %}
              <div class="table-wrapper">
                <table>
                  <thead>
                    <tr>
                      <th>Date / Heure</th>
                      <th>CSV</th>
                      <th>Rapport</th>
                      <th>Avec numéro</th>
                      <th>OK</th>
                      <th>Erreurs</th>
                      <th>Coût total (USD)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for h in history %}
                    <tr>
                      <td>{{ h.timestamp }}</td>
                      <td>{{ h.csv_name }}</td>
                      <td class="report-link">
                        {% if h.report_name %}
                          <a href="{{ url_for('download_dynamic_report', filename=h.report_name) }}">Télécharger</a>
                        {% else %}
                          -
                        {% endif %}
                      </td>
                      <td>{{ h.total_with_number }}</td>
                      <td>
                        <span class="badge-ok">{{ h.total_ok }}</span>
                      </td>
                      <td>
                        {% if h.total_error > 0 %}
                          <span class="badge-error">{{ h.total_error }}</span>
                        {% else %}
                          <span class="badge-ok">0</span>
                        {% endif %}
                      </td>
                      <td>{{ "%.4f"|format(h.total_cost or 0) }}</td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
            {% else %}
              <p class="empty-state">
                Aucune campagne enregistrée pour le moment. Uploadez un premier CSV pour démarrer.
              </p>
            {% endif %}
          </section>
        </main>
      </div>
    </body>
    </html>
    """
    return render_template_string(html, history=history, afma_logo_url=AFMA_LOGO_URL)

def run_campaign_background(csv_path, report_path, csv_name, report_name):
    """
    Lance la campagne en arrière-plan pour éviter les timeouts HTTP.
    Le résultat est enregistré dans l'historique dès que c'est terminé.
    """
    try:
        print(f"[BG] Démarrage campagne en arrière-plan : {csv_path}")
        summary = run_campaign(csv_path, report_path)

        summary["csv_name"] = csv_name
        summary["report_name"] = report_name
        append_history(summary)

        print(f"[BG] Campagne terminée. OK={summary['total_ok']}, "
              f"Erreur={summary['total_error']}, Coût={summary['total_cost']}")
    except Exception as e:
        print(f"[BG][ERROR] Erreur lors de la campagne : {e}")


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

    # 👉 Lancer en **arrière-plan**
    t = threading.Thread(
        target=run_campaign_background,
        args=(csv_path, report_path, csv_name, report_name),
        daemon=True,
    )
    t.start()

    # On répond tout de suite
    flash(
        "Campagne lancée en arrière-plan. "
        "Rafraîchissez la page dans quelques instants pour voir le rapport.",
        "success",
    )
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

    # Récupérer le JSON brut
    data = request.get_json(silent=True, force=True) or {}

    print("=== RAW WEBHOOK PAYLOAD ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    results = data.get("results", [])
    if not results:
        return jsonify({"status": "no_results"}), 200

    # Session Salesforce (initialisée au premier besoin)
    sf_session = None

    for msg in results:
        # 💰 1) CAS "STATUT" AVEC PRIX (delivery report)
        # Exemple de ce que tu as déjà reçu :
        # {
        #   "price": { "pricePerMessage": 0.006, "currency": "USD" },
        #   "messageId": "...",
        #   "to": "2126...",
        #   "doneAt": "...",
        #   "channel": "WHATSAPP",
        #   ...
        # }
        if "price" in msg and "messageId" in msg and "to" in msg:
            price_obj = msg.get("price") or {}
            price_raw = price_obj.get("pricePerMessage")
            currency = price_obj.get("currency")
            done_at = msg.get("doneAt")
            message_id = msg.get("messageId")
            to_number = msg.get("to")

            # 🔎 On essaie de convertir en float proprement
            price_val = None
          # 🔍 Log brut des events avec "price" pour diagnostic
            try:
                with open("cost_raw.log", "a", encoding="utf-8") as rf:
                    rf.write("=== EVENT PRIX ===\n")
                    rf.write(json.dumps(msg, ensure_ascii=False) + "\n\n")
            except Exception as e:
                print(f"[COST][WARN] Impossible d'écrire dans cost_raw.log: {e}")

            try:
                if price_raw is not None:
                    price_val = float(price_raw)
            except Exception:
                price_val = None

            try:
                # 1) Log détaillé dans un CSV (optionnel mais utile)
                with open("cost_log.csv", "a", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([done_at, to_number, message_id, price_raw, currency])
                print(f"[COST] Log coût: msg={message_id}, to={to_number}, price={price_raw} {currency}")

                # 2) On ne met à jour le fichier prix QUE si price_val > 0
                if price_val is not None and price_val > 0:
                    price_data = {
                        "pricePerMessage": float(price_val),
                        "currency": currency,
                        "updatedAt": done_at,
                    }
                    with open(PRICE_CACHE_FILE, "w", encoding="utf-8") as pf:
                        json.dump(price_data, pf, ensure_ascii=False)
                    print(f"[COST] Fichier prix écrit dans {PRICE_CACHE_FILE}")
                    print(f"[COST] Prix actuel mis à jour: {price_data}")
                else:
                    print(f"[COST] Prix absent ou nul ({price_raw}), fichier prix NON mis à jour.")

            except Exception as e:
                print(f"[COST][ERROR] Impossible de logguer le prix: {e}")

            # ⚠️ On ne traite pas ces évènements côté Salesforce
            continue

        # 💬 2) CAS "MESSAGE WHATSAPP" (avec integrationType + message) → ton flux normal
        if not msg.get("integrationType") or "message" not in msg:
            print("[SKIP] Évènement de statut (delivery/seen), ignoré pour Salesforce.")
            continue

        phone = msg.get("from") or msg.get("sender")
        received_at = msg.get("receivedAt")
        contact = msg.get("contact", {}) or {}
        contact_name = contact.get("name")

        # Données Excel / CSV pour ce numéro
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

                    # 🔁 Réouvrir / remettre le Case en "Nouvelle demande"
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

                    # Accusé de réception après upload OK
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
