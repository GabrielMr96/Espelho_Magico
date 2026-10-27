import base64
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config
from .log import escape, logger

# Os clientes do googleapiclient (httplib2) não são thread-safe; serializamos as chamadas.
google_lock = threading.Lock()

# Timeout (conexão, leitura) e política de retry para os endpoints OAuth do Google.
# Redes corporativas/VPN às vezes derrubam a primeira conexão; sem retry, a troca do
# código de autorização falhava mesmo depois de o usuário já ter autorizado no navegador.
_OAUTH_TIMEOUT = (15, 60)


def _retry_policy():
    return Retry(
        total=5,
        connect=5,
        read=3,
        backoff_factor=2,  # 0s, 2s, 4s, 8s, 16s
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=None,  # inclui POST (endpoint /token)
        raise_on_status=False,
    )


def _montar_retry(session):
    adapter = HTTPAdapter(max_retries=_retry_policy())
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Aplica um timeout padrão (o fetch_token do oauthlib não define nenhum -> espera do SO).
    request_original = session.request

    def request_com_timeout(method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _OAUTH_TIMEOUT
        return request_original(method, url, **kwargs)

    session.request = request_com_timeout
    return session


def autenticar_google():
    """Retorna (drive_service, gmail_service)."""
    creds = None
    if config.TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), config.GOOGLE_SCOPES)
        except Exception as e:
            logger.warning(f"Erro ao ler token.json: {escape(str(e))}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request(session=_montar_retry(requests.Session())))
            except Exception as e:
                logger.warning(f"Erro ao renovar token: {escape(str(e))}. Forçando novo login.")
                creds = None
                config.TOKEN_PATH.unlink(missing_ok=True)

        if not creds or not creds.valid:
            if not config.CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"O arquivo credentials.json é obrigatório (procurado em {config.CREDENTIALS_PATH})."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(config.CREDENTIALS_PATH), config.GOOGLE_SCOPES)
            _montar_retry(flow.oauth2session)
            creds = flow.run_local_server(port=0, timeout_seconds=300)

        config.TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return drive_service, gmail_service


# --- Parsing MIME --------------------------------------------------------------

def _charset_da_parte(parte):
    for h in parte.get("headers", []) or []:
        if h.get("name", "").lower() == "content-type":
            m = re.search(r'charset="?([\w\-]+)"?', h.get("value", ""), re.I)
            if m:
                return m.group(1)
    return "utf-8"


def decodificar_base64(data, charset="utf-8"):
    bruto = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return bruto.decode(charset, errors="replace")
    except LookupError:  # charset desconhecido
        return bruto.decode("utf-8", errors="replace")


def extrair_corpo(payload):
    """Percorre a árvore MIME e devolve (html, texto_puro), priorizando text/html.

    Ignora anexos (partes com filename). Corrige o bug antigo em que a primeira parte
    folha (geralmente text/plain) era retornada antes do text/html.
    """
    html = None
    texto = None

    def visitar(parte):
        nonlocal html, texto
        mime = (parte.get("mimeType") or "").lower()
        filhos = parte.get("parts") or []
        if filhos:
            for filho in filhos:
                visitar(filho)
            return
        if parte.get("filename"):
            return
        data = (parte.get("body") or {}).get("data")
        if not data:
            return
        if mime == "text/html" and html is None:
            html = decodificar_base64(data, _charset_da_parte(parte))
        elif mime == "text/plain" and texto is None:
            texto = decodificar_base64(data, _charset_da_parte(parte))

    visitar(payload or {})
    return html, texto


def _header(headers, nome, padrao):
    return next((h["value"] for h in headers if h.get("name", "").lower() == nome.lower()), padrao)


def executar_requisicao_google(req, max_tentativas=5):
    """Executa uma requisição do Google API Client de forma thread-safe e com backoff para rate limits."""
    for tentativa in range(max_tentativas):
        try:
            with google_lock:
                return req.execute()
        except HttpError as e:
            codigo = getattr(e.resp, "status", None)
            if codigo in (403, 429) and tentativa < max_tentativas - 1:
                espera = 15 * (tentativa + 1)
                logger.warning(
                    f"[yellow]⚠ Limite de requisições do Gmail (status {codigo}). "
                    f"Aguardando {espera}s antes de tentar novamente...[/yellow]"
                )
                time.sleep(espera)
                continue
            if codigo in (500, 502, 503, 504) and tentativa < max_tentativas - 1:
                time.sleep(5)
                continue
            raise


def buscar_emails(gmail_service):
    """Lista os e-mails não lidos do marcador e extrai o conteúdo. Uma falha isolada não derruba o lote."""
    query = f"label:{config.GMAIL_LABEL} is:unread"
    ids = []
    try:
        token = None
        while True:
            resp = executar_requisicao_google(
                gmail_service.users().messages().list(userId="me", q=query, pageToken=token)
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token:
                break
    except Exception as e:
        logger.error(f"[bold red]✖ Erro ao listar e-mails do Gmail: {escape(str(e))}[/bold red]")
        return []

    if not ids:
        logger.info(f"[yellow]Nenhum e-mail novo encontrado no marcador {config.GMAIL_LABEL}.[/yellow]")
        return []

    emails = []
    for msg_id in ids:
        try:
            detalhes = executar_requisicao_google(
                gmail_service.users().messages().get(userId="me", id=msg_id, format="full")
            )
            payload = detalhes.get("payload", {})
            html, texto = extrair_corpo(payload)
            conteudo = html or texto
            if not conteudo:
                logger.warning(f"[yellow]⚠ E-mail {msg_id} sem corpo legível. Ignorado.[/yellow]")
                continue
            headers = payload.get("headers", [])
            emails.append({
                "id": msg_id,
                "assunto": _header(headers, "Subject", "Newsletter_Sem_Assunto"),
                "remetente": _header(headers, "From", "Desconhecido"),
                "data": _header(headers, "Date", ""),
                "conteudo_html": conteudo,
                "eh_html": html is not None,
            })
            # Pausa curta cooperativa para respeitar limites por segundo do Gmail
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"[bold red]✖ Falha ao ler e-mail {msg_id}: {escape(str(e))}[/bold red]")
    return emails


def marcar_como_lido(gmail_service, msg_id):
    try:
        executar_requisicao_google(
            gmail_service.users().messages().modify(
                userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
            )
        )
        logger.debug(f"E-mail {msg_id} marcado como lido.")
    except Exception as e:
        logger.error(f"Erro ao marcar mensagem {msg_id} como lida: {escape(str(e))}")
