import os
import base64
import requests

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


def create_case(session: dict, phone: str, nom: str | None = None, entreprise: str | None = None) -> str:
    """
    Crée un Case dans Salesforce.
    Retourne le CaseId.
    """
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/Case"
    headers = _headers(session)

    payload = {
        "Nom__c": nom or "",
        "Origin": "Whatsapp",
        "Status": "Nouvelle demande",
        "RecordTypeId": SF_CASE_RECORD_TYPE_ID,   # vient de l'ENV
        "Telephone__c": phone,

        # ✅ Champs statiques demandés
        "TypeDeDeclaration__c": "Complément d'information",
        "Type": "Déclaration Maladie",
    }

    # ✅ On ne remplit NomDeLentreprise__c que si on a une valeur
    if entreprise:
        payload["NomDeLentreprise__c"] = entreprise

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



def create_content_version(session: dict, file_bytes: bytes, filename: str, title: str | None = None) -> tuple[str, str]:
    """
    Crée un ContentVersion dans Salesforce.
    Retourne (content_version_id, content_document_id).
    """
    url = f"{session['instance_url']}/services/data/v59.0/sobjects/ContentVersion"
    headers = _headers(session)

    version_data_b64 = base64.b64encode(file_bytes).decode("utf-8")

    payload = {
        "VersionData": version_data_b64,
        "Title": title or filename.rsplit(".", 1)[0],
        "PathOnClient": filename,
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


def upload_document_for_case(session: dict, case_id: str, file_bytes: bytes, filename: str, title: str | None = None) -> str:
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
