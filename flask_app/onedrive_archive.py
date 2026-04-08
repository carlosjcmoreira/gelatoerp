"""
OneDrive archiving via Microsoft Graph API — delegated OAuth2 approach.

The user authorises the app once via browser (OAuth2 Authorization Code flow).
The refresh token is stored in system_config ('onedrive_refresh_token').
On every upload, the refresh token is exchanged for a short-lived access token.

Env vars required: AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
Tenant: 'common' (supports personal Microsoft accounts such as @outlook.com).

system_config keys used:
  onedrive_refresh_token  — OAuth2 refresh token (set on callback)
  onedrive_user_email     — email of the authorised Microsoft account
  onedrive_folder         — base folder path in OneDrive
                            (default: 'Scoopy/2. Contabilidade/Registo de Faturas')

Upload path structure:
  {base}/{year}/{MonAA}/Faturas/{subfolder}/{filename}
  e.g. Scoopy/2. Contabilidade/Registo de Faturas/2026/Mar26/Faturas/Matosinhos (M)/file.pdf
"""
import logging
import os
import re
from datetime import datetime
from urllib.parse import quote

logger = logging.getLogger(__name__)

_MONTH_ABBR_PT = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                  'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

_OLD_DEFAULT_FOLDER = 'Scoopy/Faturas'
_DEFAULT_FOLDER = 'Scoopy/2. Contabilidade/Registo de Faturas'

GRAPH_BASE = 'https://graph.microsoft.com/v1.0'
TOKEN_URL = 'https://login.microsoftonline.com/common/oauth2/v2.0/token'

AZURE_CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
AZURE_CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET')

SCOPES = 'Files.ReadWrite offline_access User.Read'


def _db():
    import database as _database
    return _database


def _get_refresh_token() -> str | None:
    try:
        return _db().get_system_config('onedrive_refresh_token') or None
    except Exception as e:
        logger.warning("Could not read onedrive_refresh_token: %s", e)
        return None


def _get_access_token() -> str:
    """Exchange stored refresh token for a new access token."""
    import requests

    refresh_token = _get_refresh_token()
    if not refresh_token:
        raise RuntimeError('OneDrive não autorizado. Autorize em Gestor > Configurações.')

    resp = requests.post(TOKEN_URL, data={
        'grant_type': 'refresh_token',
        'client_id': AZURE_CLIENT_ID,
        'client_secret': AZURE_CLIENT_SECRET,
        'refresh_token': refresh_token,
        'scope': SCOPES,
    }, timeout=20)

    if not resp.ok:
        raise RuntimeError(f'Falha ao renovar token OneDrive: {resp.status_code} {resp.text[:200]}')

    data = resp.json()

    new_refresh = data.get('refresh_token')
    if new_refresh and new_refresh != refresh_token:
        try:
            _db().set_system_config('onedrive_refresh_token', new_refresh)
        except Exception as e:
            logger.warning("Could not persist rotated refresh token: %s", e)

    return data['access_token']


def _get_base_folder() -> str:
    try:
        folder = (_db().get_system_config('onedrive_folder') or '').strip('/')
    except Exception:
        folder = ''
    if not folder or folder == _OLD_DEFAULT_FOLDER:
        try:
            _db().set_system_config('onedrive_folder', _DEFAULT_FOLDER)
        except Exception as e:
            logger.warning("Could not migrate onedrive_folder to new default: %s", e)
        return _DEFAULT_FOLDER
    return folder


def _month_folder(year: int, month: int) -> str:
    """Return month folder name, e.g. 'Mar26' for March 2026."""
    abbr = _MONTH_ABBR_PT[month - 1]
    year_2 = str(year)[-2:]
    return f'{abbr}{year_2}'


def _list_children(token: str, parent_id: str, drive_id: str = None) -> list:
    """
    Return all children of a drive item, handling pagination.
    When drive_id is set, uses the drive-specific endpoint (required for
    remote drives / SharePoint libraries). Always requests remoteItem so
    callers can detect shortcuts.
    """
    import requests
    auth = {'Authorization': f'Bearer {token}'}
    items = []
    if drive_id:
        url = (f'{GRAPH_BASE}/drives/{drive_id}/items/{parent_id}'
               f'/children?$select=id,name,remoteItem&$top=200')
    else:
        url = (f'{GRAPH_BASE}/me/drive/items/{parent_id}'
               f'/children?$select=id,name,remoteItem&$top=200')
    while url:
        resp = requests.get(url, headers=auth, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get('value', []))
        url = data.get('@odata.nextLink')
    return items


def _navigate_to_folder(token: str, folder_path: str) -> tuple:
    """
    Navigate to a folder by traversing each path component level by level.
    Missing folders are created automatically.
    Follows remoteItem shortcuts (e.g. OneDrive shortcuts / shared folders)
    to the real drive when encountered.

    Returns (folder_id, drive_id) where drive_id is None when the destination
    is on the user's own drive, or a string drive ID when on a remote drive.
    """
    import requests
    auth = {'Authorization': f'Bearer {token}'}
    parts = [p for p in folder_path.split('/') if p]
    current_id = 'root'
    current_drive_id = None  # None → use /me/drive endpoints
    for folder_name in parts:
        children = _list_children(token, current_id, current_drive_id)
        match = next((c for c in children if c['name'].lower() == folder_name.lower()), None)
        if match:
            ri = match.get('remoteItem')
            if ri:
                # Item is a shortcut/remote — follow it to the real drive
                new_drive_id = ri.get('parentReference', {}).get('driveId')
                current_drive_id = new_drive_id or current_drive_id
                current_id = ri['id']
                logger.debug("Followed OneDrive shortcut '%s' → drive=%s id=%s",
                             folder_name, current_drive_id, current_id)
            else:
                current_id = match['id']
        else:
            if current_drive_id:
                create_url = f'{GRAPH_BASE}/drives/{current_drive_id}/items/{current_id}/children'
            else:
                create_url = f'{GRAPH_BASE}/me/drive/items/{current_id}/children'
            resp = requests.post(
                create_url,
                headers={**auth, 'Content-Type': 'application/json'},
                json={
                    'name': folder_name,
                    'folder': {},
                    '@microsoft.graph.conflictBehavior': 'replace',
                },
                timeout=20,
            )
            resp.raise_for_status()
            current_id = resp.json()['id']
            logger.info("Created OneDrive folder: %s", folder_name)
    return current_id, current_drive_id


def _upload_via_delegated(pdf_bytes: bytes, pdf_filename: str,
                          subfolder: str, year: int, month: int) -> dict:
    """Upload PDF using the stored delegated OAuth2 token."""
    import requests

    token = _get_access_token()
    base = _get_base_folder()
    month_str = _month_folder(year, month)
    folder_path = f'{base}/{year}/{month_str}/Faturas/{subfolder}'

    folder_id, drive_id = _navigate_to_folder(token, folder_path)

    prefix_match = re.search(r'\(([^)]+)\)', subfolder)
    upload_filename = f'{prefix_match.group(1)}_{pdf_filename}' if prefix_match else pdf_filename

    filename_encoded = quote(upload_filename, safe='')
    if drive_id:
        upload_url = f'{GRAPH_BASE}/drives/{drive_id}/items/{folder_id}:/{filename_encoded}:/content'
    else:
        upload_url = f'{GRAPH_BASE}/me/drive/items/{folder_id}:/{filename_encoded}:/content'

    resp = requests.put(
        upload_url,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/pdf'},
        data=pdf_bytes,
        timeout=60,
    )
    resp.raise_for_status()
    item = resp.json()
    return {
        'onedrive_path': f'{folder_path}/{upload_filename}',
        'web_url': item.get('webUrl', ''),
        'warning': None,
    }


def is_configured() -> bool:
    """Return True if a valid refresh token is stored."""
    return bool(_get_refresh_token() and AZURE_CLIENT_ID and AZURE_CLIENT_SECRET)


def upload_invoice_pdf(pdf_bytes: bytes, pdf_filename: str,
                       subfolder: str, issue_date) -> dict:
    """
    Upload a PDF to OneDrive using the stored delegated token.

    Returns dict with 'onedrive_path', 'web_url', and 'warning' keys.
    If not configured, returns gracefully with a warning.
    """
    if issue_date:
        if hasattr(issue_date, 'year'):
            year, month = issue_date.year, issue_date.month
        else:
            dt = datetime.strptime(str(issue_date), '%Y-%m-%d')
            year, month = dt.year, dt.month
    else:
        now = datetime.now()
        year, month = now.year, now.month

    if not is_configured():
        return {
            'onedrive_path': None,
            'web_url': None,
            'warning': 'OneDrive não autorizado. Autorize em Gestor > Configurações.',
        }

    try:
        return _upload_via_delegated(pdf_bytes, pdf_filename, subfolder, year, month)
    except Exception as e:
        logger.error("OneDrive delegated upload failed: %s", e)
        return {
            'onedrive_path': None,
            'web_url': None,
            'warning': f'Erro ao arquivar no OneDrive: {e}',
        }


def test_connection() -> dict:
    """
    Test the delegated connection by uploading a tiny placeholder file.
    Returns {'ok': True/False, 'web_url': ..., 'error': ...}.
    """
    import requests

    if not is_configured():
        return {'ok': False, 'error': 'OneDrive não autorizado.'}

    try:
        token = _get_access_token()
    except RuntimeError as e:
        return {'ok': False, 'error': str(e)}

    test_filename = '_scoopyteste_.pdf'
    test_bytes = b'%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n%%EOF'
    base = _get_base_folder()
    try:
        folder_id, drive_id = _navigate_to_folder(token, base)
    except Exception as e:
        return {'ok': False, 'error': f'Erro ao aceder à pasta: {e}'}
    if drive_id:
        upload_url = f'{GRAPH_BASE}/drives/{drive_id}/items/{folder_id}:/_scoopyteste_.pdf:/content'
    else:
        upload_url = f'{GRAPH_BASE}/me/drive/items/{folder_id}:/_scoopyteste_.pdf:/content'

    try:
        resp = requests.put(
            upload_url,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/pdf'},
            data=test_bytes,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            item = resp.json()
            return {'ok': True, 'web_url': item.get('webUrl', '')}
        else:
            return {'ok': False, 'error': f'HTTP {resp.status_code}: {resp.text[:300]}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
