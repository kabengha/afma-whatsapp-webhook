import os
import base64
import requests
import re
import unicodedata
from datetime import datetime

SF_AUTH_URL = "https://login.salesforce.com/services/oauth2/token"

SF_CLIENT_ID = os.getenv("SF_CLIENT_ID")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET")
SF_USERNAME = os.getenv("SF_USERNAME")
SF_PASSWORD = os.getenv("SF_PASSWORD")
SF_SECURITY_TOKEN = os.getenv("SF_SECURITY_TOKEN")

SF_CASE_RECORD_TYPE_ID = os.getenv("SF_CASE_RECORD_TYPE_ID", "01268000000kfDeAAI")


class SalesforceError(Exception):
    """Exception personnalisée pour les erreurs Salesforce."""
    pass



def _clean_unicode(s: str) -> str:
    """Supprime les caractères invisibles/bidi et normalise Unicode."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFC", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cf", "Cc"))
    return s

def sanitize_filename(name: str, default: str = "document") -> str:
    """Nettoie un nom de fichier pour éviter les caractères bizarres côté Salesforce."""
    name = _clean_unicode(name).strip() or default
    # Remplace les séparateurs/char interdits par '_'
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    # espaces multiples -> un seul
    name = re.sub(r"\s+", " ", name).strip()
    # évite un nom trop long
    if len(name) > 120:
        name = name[:120]
    return name

def sanitize_title(title: str | None, fallback: str) -> str:
    t = title if title else fallback
    t = _clean_unicode(t).strip() or fallback
    t = t.replace("/", "-")  # évite les slash dans le titre
    if len(t) > 80:
        t = t[:80]
    return t

def get_salesforce_session():
    """
    Demande un nouveau token à Salesforce.
    Retourne un dict {access_token, instance_url}.
    On appelle cette fonction à chaque webhook (stratégie simple & safe).
    """
    if not all([SF_CLIENT_ID, SF_CLIENT_SECRET, SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN]):
        raise SalesforceError(
            "Les variables d'environnement Salesforce ne sont pas toutes définies "
            "(SF_CLIENT_ID, SF_CLIENT_SECRET, SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN)."
        )

    data = {
        "grant_type": "password",
        "client_id": SF_CLIENT_ID,
        "client_secret": SF_CLIENT_SECRET,
        "username": SF_USERNAME,
        # password + security token concaténés
        "password": SF_PASSWORD + SF_SECURITY_TOKEN,
    }

    resp = requests.post(SF_AUTH_URL, data=data, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Erreur d'authentification Salesforce: {e} - {resp.text}")

    payload = resp.json()
    access_token = payload["access_token"]
    instance_url = payload.get("instance_url")

    if not instance_url:
        raise SalesforceError("instance_url manquant dans la réponse Salesforce.")

    return {
        "access_token": access_token,
        "instance_url": instance_url.rstrip("/"),
    }


def _headers(session: dict) -> dict:
    """Construit les headers standard pour l'API Salesforce."""
    return {
        "Authorization": f"Bearer {session['access_token']}",
        "Content-Type": "application/json",
    }


def get_case_status(session: dict, case_id: str) -> str:
    """
    Retourne le champ Status d'un Case Salesforce.
    """
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/Case/{case_id}?fields=Status"
    headers = _headers(session)

    resp = requests.get(url, headers=headers, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Erreur get_case_status: {e} - {resp.text}")

    data = resp.json()
    return data.get("Status", "") or ""


def create_case(session: dict, phone: str, nom: str | None = None,
                entreprise: str | None = None,
                cin: str | None = None,
                police: str | None = None) -> str:
    """
    Crée un Case dans Salesforce.
    Retourne le CaseId.
    """
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/Case"
    headers = _headers(session)

    payload = {
        "Nom__c": nom or "",
        "NomDeLentreprise__c": entreprise or "",
        "Origin": "Whatsapp",
        "TypeDeDeclaration__c": "Complément d'information",
        "Type": "Déclaration Maladie",
        "Status": "Nouvelle demande",
        "RecordTypeId": SF_CASE_RECORD_TYPE_ID,
        "Telephone__c": phone,
        # 🆕 nouveaux champs :
        "CIN__c": cin or "",
        "NDePolice__c": police or "",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Erreur create_case: {e} - {resp.text}")

    data = resp.json()
    case_id = data.get("id")
    if not case_id:
        raise SalesforceError(f"Réponse create_case sans id: {data}")

    return case_id


def update_case_status(session: dict, case_id: str, new_status: str = "Nouvelle demande") -> None:
    """
    Met à jour le statut d'un Case existant.
    Ex : le remettre à 'Nouvelle demande' après réception d'un document.
    """
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/Case/{case_id}"
    headers = _headers(session)

    payload = {
        "Status": new_status
    }

    resp = requests.patch(url, headers=headers, json=payload, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Erreur update_case_status: {e} - {resp.text}")


def create_content_version(session: dict, file_bytes: bytes, filename: str,
                           title: str | None = None) -> tuple[str, str]:
    """
    Crée un ContentVersion dans Salesforce.
    Retourne (content_version_id, content_document_id).
    """
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/ContentVersion"
    headers = _headers(session)

    version_data_b64 = base64.b64encode(file_bytes).decode("utf-8")

    payload = {
        "VersionData": version_data_b64,
        "Title": sanitize_title(title, sanitize_filename(filename).rsplit(".", 1)[0]),
        "PathOnClient": sanitize_filename(filename)
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Erreur create_content_version: {e} - {resp.text}")

    data = resp.json()
    content_version_id = data.get("id")
    content_document_id = data.get("ContentDocumentId")

    # Si ContentDocumentId n'est pas retourné directement, on va le chercher
    if not content_document_id and content_version_id:
        cv_url = f"{session['instance_url']}/services/data/v59.0/sobjects/ContentVersion/{content_version_id}"
        cv_resp = requests.get(cv_url, headers=headers, timeout=10)
        try:
            cv_resp.raise_for_status()
        except requests.HTTPError as e:
            raise SalesforceError(f"Erreur get ContentVersion: {e} - {cv_resp.text}")

        content_document_id = cv_resp.json().get("ContentDocumentId")

    if not content_document_id:
        raise SalesforceError(f"ContentDocumentId introuvable pour ContentVersion {content_version_id}")

    return content_version_id, content_document_id


def link_document_to_case(session: dict, content_document_id: str, case_id: str) -> str:
    """
    Crée un ContentDocumentLink pour lier le document au Case.
    Retourne l'id du ContentDocumentLink.
    """
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/ContentDocumentLink"
    headers = _headers(session)

    payload = {
        "ContentDocumentId": content_document_id,
        "LinkedEntityId": case_id,
        "Visibility": "AllUsers",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Erreur link_document_to_case: {e} - {resp.text}")

    data = resp.json()
    link_id = data.get("id")
    if not link_id:
        raise SalesforceError(f"Réponse ContentDocumentLink sans id: {data}")

    return link_id


def upload_document_for_case(session: dict, case_id: str, file_bytes: bytes,
                             filename: str, title: str | None = None) -> str:
    """
    Helper complet :
    - crée un ContentVersion
    - récupère le ContentDocumentId
    - crée le ContentDocumentLink vers le Case

    Retourne l'id du ContentDocumentLink créé.
    """
    _, content_document_id = create_content_version(session, file_bytes, filename, title=title)
    link_id = link_document_to_case(session, content_document_id, case_id)
    return link_id

# ============================
#  WhatsAppNotifications__c
# ============================

def describe_object(session: dict, sobject_api_name: str) -> dict:
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/{sobject_api_name}/describe"
    resp = requests.get(url, headers=_headers(session), timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Describe échoué pour {sobject_api_name}: {e} - {resp.text}")
    return resp.json()

def link_document_to_entity(session: dict, content_document_id: str, entity_id: str) -> str:
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/ContentDocumentLink"
    headers = _headers(session)
    payload = {
        "ContentDocumentId": content_document_id,
        "LinkedEntityId": entity_id,
        "Visibility": "AllUsers",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Erreur link_document_to_entity: {e} - {resp.text}")
    data = resp.json()
    link_id = data.get("id")
    if not link_id:
        raise SalesforceError(f"Réponse ContentDocumentLink sans id: {data}")
    return link_id

def upload_document_for_entity(session: dict, entity_id: str, file_bytes: bytes,
                               filename: str, title: str | None = None) -> str:
    _, content_document_id = create_content_version(session, file_bytes, filename, title=title)
    return link_document_to_entity(session, content_document_id, entity_id)

def _pick_existing_field(field_names: set[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in field_names:
            return c
    return None

def create_whatsapp_notification_auto(
    session: dict,
    messages_a_envoyer: int,
    messages_envoyes: int,
    messages_echoues: int,
    date_denvoi_jjmmYYYY: str,
    cout_envoi: float,
) -> str:
    sobject = "WhatsAppNotifications__c"
    desc = describe_object(session, sobject)
    fields = desc.get("fields", [])
    field_names = {f.get("name") for f in fields if f.get("name")}
    field_types = {f.get("name"): f.get("type") for f in fields if f.get("name")}

    f_messages_a = _pick_existing_field(field_names, ["MessagesAenvoyer__c", "MessagesAenvoyer"])
    f_messages_ok = _pick_existing_field(field_names, ["MessagesEnvoyes__c", "MessagesEnvoyes"])
    f_messages_nok = _pick_existing_field(field_names, ["MessagesEchoues__c", "MessagesEchoues"])
    f_date = _pick_existing_field(field_names, ["DateDenvoi__c", "DateDenvoi"])
    f_cost = _pick_existing_field(field_names, ["CoutEnvoi__c", "CoutEnvoi"])

    missing = [name for name, val in [
        ("MessagesAenvoyer", f_messages_a),
        ("MessagesEnvoyes", f_messages_ok),
        ("MessagesEchoues", f_messages_nok),
        ("DateDenvoi", f_date),
        ("CoutEnvoi", f_cost),
    ] if not val]
    if missing:
        raise SalesforceError(
            "Champs introuvables via describe pour WhatsAppNotifications__c: "
            + ", ".join(missing)
        )

    payload = {
        f_messages_a: int(messages_a_envoyer),
        f_messages_ok: int(messages_envoyes),
        f_messages_nok: int(messages_echoues),
        f_cost: float(cout_envoi),
    }

    date_type = field_types.get(f_date)
    if date_type in ("date", "datetime"):
        dt = datetime.strptime(date_denvoi_jjmmYYYY, "%d/%m/%Y")
        payload[f_date] = dt.strftime("%Y-%m-%d")
    else:
        payload[f_date] = date_denvoi_jjmmYYYY

    url = f"{session['instance_url']}/services/data/v59.0/sobjects/{sobject}"
    resp = requests.post(url, headers=_headers(session), json=payload, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise SalesforceError(f"Création {sobject} échouée: {e} - {resp.text}")
    data = resp.json()
    rec_id = data.get("id")
    if not rec_id:
        raise SalesforceError(f"Réponse {sobject} sans id: {data}")
    return rec_id