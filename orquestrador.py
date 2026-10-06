import os
import sys
import glob
import json
import time
import re
import logging
from logging.handlers import RotatingFileHandler
import datetime
import subprocess
import tempfile
import shutil
import base64
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from markitdown import MarkItDown
import concurrent.futures
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, BarColumn, TaskProgressColumn
from threading import Lock

# Trava para evitar que os logs de múltiplas threads se embaralhem no console
print_lock = Lock()

# Novos imports da SDK do Google GenAI
from google import genai
from google.genai import types
from google.genai.errors import APIError

# Imports para Google Drive
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import requests

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# Rotação automática de logs: mantém até 3 backups de ~500KB cada (~1.5MB total)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(
    'espelho_magico.log',
    maxBytes=500_000,
    backupCount=3,
    encoding='utf-8'
)
_log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(_log_handler)

# Silencia warnings repetitivos do pdfminer (FontBBox)
logging.getLogger("pdfminer").setLevel(logging.ERROR)

console = Console()
load_dotenv()

# Validação de ambiente
api_keys_str = os.environ.get("GEMINI_API_KEY", "")
api_keys = [k.strip() for k in api_keys_str.split(",") if k.strip()]
perplexity_api_key = os.environ.get("PERPLEXITY_API_KEY")

if not api_keys:
    console.print("[bold red]Erro:[/bold red] Nenhuma GEMINI_API_KEY encontrada no .env.")
    exit(1)

clientes_gemini = [genai.Client(api_key=k) for k in api_keys]
indice_cliente_atual = 0

def converter_pdf_markitdown_limpo(caminho_pdf):
    """Lê o PDF com MarkItDown e aplica todas as limpezas de Regex na força bruta."""
    md = MarkItDown()
    result = md.convert(caminho_pdf)
    texto = result.text_content
    
    # 1. Limpeza de caracteres nulos e quebras de página do PDF
    texto = texto.replace('\x0c', '') 
    
    # 2. Remoção de blocos grandes de patrocínio (Mata do "APRESENTADO POR" até a próxima seção que comece com emoji ou linha vazia forte)
    texto = re.sub(r'(?im)^APRESENTADO POR.*?(?=^🔵|^#|^\*|\n{3,})', '', texto, flags=re.DOTALL)
    
    # 3. Lista de Regex para aniquilar linhas específicas (Headers, Footers, CTAs e lixo de email)
    patterns_linha_unica = [
        r'(?i)^leia mais.*$',
        r'(?i)^clique aqui.*$',
        r'(?i)^Responder a:.*$',
        r'(?i)^Para:.*$',
        r'<[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}>', # Extrai e-mails soltos
        r'(?i)^Boas-vindas às.*$',
        r'(?i)^👋\s*BOA NOITE.*$',
        r'(?i)^.*Leia Online.*$',
        r'(?i)^Assine a .* PRO para continuar lendo.*$',
        r'(?i)^Assinar e continuar lendo.*$',
        r'(?i)^Atualize suas preferências de e-mail.*$',
        r'(?i)^© \d{4}.*$',
        r'(?i)^Reservar minha vaga.*$',
        r'^\d{1,2} de [a-z]+ de \d{4}.*$' # Datas de cabeçalho
    ]
    
    for p in patterns_linha_unica:
        texto = re.sub(p, '', texto, flags=re.MULTILINE)
        
    # 4. Limpeza final de formatação (Tira excesso de espaços e quebras de linha triplas criadas pelos buracos do Regex)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    texto = re.sub(r' {2,}', ' ', texto)
    return texto

def converter_pdf_marker(caminho_pdf):
    """Converte PDF usando o Marker (datalab-to/marker) via CLI subprocess.
    
    O Marker usa modelos de deep learning para extrair texto com alta fidelidade,
    preservando tabelas, listas e estrutura do documento. Roda como subprocesso
    para não carregar os modelos ML na memória do processo principal.
    
    Returns:
        str: Texto em Markdown extraído pelo Marker.
        
    Raises:
        Exception: Se o Marker não estiver instalado ou falhar na conversão.
    """
    # Verifica se o marker_single está disponível no PATH do venv
    marker_cmd = shutil.which("marker_single")
    if not marker_cmd:
        # Tenta o caminho direto do venv no Windows
        venv_marker = os.path.join(os.path.dirname(sys.executable), "marker_single.exe")
        if os.path.exists(venv_marker):
            marker_cmd = venv_marker
        else:
            raise Exception("marker_single não encontrado. Execute: pip install marker-pdf")
    
    # Cria diretório temporário para a saída do Marker
    dir_temp = tempfile.mkdtemp(prefix="marker_")
    
    try:
        cmd = [marker_cmd, caminho_pdf, "--output_dir", dir_temp, "--output_format", "markdown"]
        
        # Se MARKER_USE_LLM estiver ativo e houver chave Gemini, usa LLM para reparar tabelas
        if os.environ.get("MARKER_USE_LLM", "").lower() == "true":
            cmd.append("--use_llm")
        
        logging.info(f"Marker CLI: {' '.join(cmd)}")
        
        # Desativa hf_transfer para evitar erros 403 (Forbidden) no download do HuggingFace
        env_vars = os.environ.copy()
        env_vars["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        
        resultado = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutos de timeout generoso para PDFs pesados e/ou CPU lenta
            encoding='utf-8',
            errors='replace',
            env=env_vars
        )
        
        if resultado.returncode != 0:
            erro = resultado.stderr.strip() if resultado.stderr else "Código de saída não-zero"
            raise Exception(f"Marker falhou (código {resultado.returncode}): {erro}")
        
        # O Marker gera a saída dentro de um subdiretório com o nome do PDF
        nome_base = os.path.splitext(os.path.basename(caminho_pdf))[0]
        
        # Busca o arquivo .md gerado (pode estar em subdiretório)
        arquivos_md = glob.glob(os.path.join(dir_temp, "**", "*.md"), recursive=True)
        
        if not arquivos_md:
            raise Exception(f"Marker não gerou arquivo .md em {dir_temp}")
        
        # Lê o primeiro .md encontrado (geralmente só tem um)
        with open(arquivos_md[0], 'r', encoding='utf-8') as f:
            texto = f.read()
        
        if not texto.strip():
            raise Exception("Marker gerou arquivo .md vazio")
            
        logging.info(f"Marker extraiu {len(texto)} caracteres de {caminho_pdf}")
        return texto
        
    finally:
        # Limpa o diretório temporário
        try:
            shutil.rmtree(dir_temp, ignore_errors=True)
        except Exception:
            pass

def converter_pdf_com_fallback(caminho_pdf):
    """Tenta converter o PDF usando Marker primeiro; se falhar, usa MarkItDown.
    
    Controlado pela variável de ambiente MARKER_ENABLED (default: true se instalado).
    """
    usar_marker = os.environ.get("MARKER_ENABLED", "true").lower() == "true"
    
    if usar_marker:
        try:
            texto = converter_pdf_marker(caminho_pdf)
            console.print("[bold green]  ✔ Marker[/bold green] [dim](extração de alta fidelidade)[/dim]")
            return texto
        except Exception as e:
            console.print(f"[yellow]  ⚠ Marker indisponível ({e}). Usando MarkItDown como fallback...[/yellow]")
            logging.warning(f"Marker fallback para MarkItDown: {e}")
    
    texto = converter_pdf_markitdown_limpo(caminho_pdf)
    console.print("[dim]  ✔ MarkItDown (extração com regex)[/dim]")
    return texto

def extrair_com_perplexity(conteudo_html, prompt):
    if not perplexity_api_key:
        raise Exception("PERPLEXITY_API_KEY ausente.")
        
    console.print(f"[cyan]➜ Extraindo texto via Perplexity...[/cyan]")
    modelo_perplexity = os.environ.get("PERPLEXITY_MODEL", "sonar-pro")
    
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {perplexity_api_key}",
        "Content-Type": "application/json"
    }
    
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Filtre e extraia os dados deste conteúdo:\n\n{conteudo_html}"}
    ]
    
    payload = {
        "model": modelo_perplexity,
        "messages": messages,
        "temperature": 0.2,
    }
    
    response = None
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"], None
    except Exception as e:
        erro_detalhado = response.text if response is not None and hasattr(response, "text") else ""
        raise Exception(f"Erro na API do Perplexity: {e} - {erro_detalhado}")

def extrair_com_gemini(conteudo_html, prompt, modelo_atual="gemini-3.8-flash"):
    global indice_cliente_atual
    ultimo_erro = "Erro desconhecido"
    
    if len(clientes_gemini) > 1:
        with print_lock:
            indice_cliente_atual = (indice_cliente_atual + 1) % len(clientes_gemini)
        
    max_retries = 5
    
    for retry in range(1, max_retries + 1):
        tentativas_maximas = len(clientes_gemini)
        
        for tentativa in range(1, tentativas_maximas + 1):
            cliente_atual = clientes_gemini[indice_cliente_atual]
            try:
                with print_lock:
                    console.print(f"[cyan]➜ Processando com {modelo_atual} (Retentativa {retry}/{max_retries} | Chave {indice_cliente_atual + 1}/{len(clientes_gemini)})...[/cyan]")
                
                config_kwargs = {
                    "system_instruction": prompt,
                    "temperature": 0.2,
                }
                if "RESULTADOS RECENTES" in prompt:
                    config_kwargs["tools"] = [{"google_search": {}}]

                response = cliente_atual.models.generate_content(
                    model=modelo_atual,
                    contents=[conteudo_html],
                    config=types.GenerateContentConfig(**config_kwargs)
                )
                return response.text, None, cliente_atual
                
            except APIError as e:
                erro_str = str(e)
                if "404" in erro_str or "NOT_FOUND" in erro_str:
                    raise Exception(f"Modelo {modelo_atual} não encontrado ou descontinuado (Erro 404).")
                    
                if "429" in erro_str or "RESOURCE_EXHAUSTED" in erro_str or "503" in erro_str or "UNAVAILABLE" in erro_str:
                    is_quota = "429" in erro_str or "RESOURCE_EXHAUSTED" in erro_str
                    ultimo_erro = "Cota Esgotada (Rate Limit / 429)" if is_quota else "Serviço Indisponível (503)"
                    
                    if not is_quota:
                        # Se for erro 503, o problema é no servidor do modelo, não na chave. 
                        with print_lock:
                            console.print(f"[yellow]  ⚠ 503 Service Unavailable no {modelo_atual}. Problema no servidor.[/yellow]")
                        break # Sai do loop de chaves, vai para o sleep do loop de retries
                        
                    if tentativa < tentativas_maximas:
                        with print_lock:
                            console.print(f"[yellow]  ⚠ {ultimo_erro} na Chave {indice_cliente_atual + 1} no {modelo_atual}. Alternando para a próxima chave...[/yellow]")
                        indice_cliente_atual = (indice_cliente_atual + 1) % len(clientes_gemini)
                        continue
                    else:
                        with print_lock:
                            console.print(f"[yellow]  ⚠ Todas as chaves esgotaram limite para {modelo_atual} na retentativa {retry}.[/yellow]")
                        break # Sai do loop de chaves, vai para o sleep do loop de retries
                else:
                    raise Exception(f"Erro na API ({modelo_atual}): {erro_str}")
            except Exception as e:
                 raise Exception(f"Erro inesperado ({modelo_atual}): {str(e)}")
                 
        if retry < max_retries:
            import random
            espera_base = 15 * (2 ** (retry - 1)) # Exponential backoff: 15, 30, 60, 120...
            espera = espera_base + random.uniform(1.0, 5.0) # Jitter
            with print_lock:
                console.print(f"[yellow]  ⏳ Aguardando {espera:.1f}s antes de tentar o {modelo_atual} novamente...[/yellow]")
            time.sleep(espera)
            
    raise Exception(f"Todas as {max_retries} tentativas falharam para o {modelo_atual} após backoff. Motivo final: {ultimo_erro}")

def processar_conteudo_html(conteudo_html, prompt, chave_newsletter):
    texto_limpo = ""
    
    modelos_para_testar = []
    modelo_env = os.environ.get("GEMINI_MODEL")
    modelo_fallback = os.environ.get("GEMINI_MODEL_FALLBACK")
    
    if modelo_env:
        modelos_para_testar.append(modelo_env)
    if modelo_fallback:
        modelos_para_testar.append(modelo_fallback)
        
    modelos_para_testar.extend([
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-flash-latest",
        "gemini-pro-latest"
    ])
    modelos_para_testar = list(dict.fromkeys(modelos_para_testar))
    
    ia_padrao = os.environ.get("IA_PADRAO", "gemini").lower()
    
    # Roteamento cravado pro Gol Contra (Removido, usará Gemini por padrão)        
    if ia_padrao == "perplexity":
        try:
            texto_limpo, _ = extrair_com_perplexity(conteudo_html, prompt)
            return texto_limpo
        except Exception as e:
            with print_lock: console.print(f"[yellow]⚠ Erro no Perplexity: {e}. Tentando Gemini como fallback...[/yellow]")

    for modelo in modelos_para_testar:
        try:
            texto_limpo, _, _ = extrair_com_gemini(conteudo_html, prompt, modelo_atual=modelo)
            return texto_limpo
        except Exception as e:
            with print_lock: console.print(f"[yellow]  ⚠ Erro com modelo {modelo}: {e}. Tentando próxima versão...[/yellow]")
            continue
            
    msg_erro = "⚠ Falha total em todos os modelos testados."
    logging.warning(msg_erro)
    with print_lock: console.print(f"[bold red]{msg_erro}[/bold red]")
    return "ERRO: Falha na extração."

# Lógica de Integração com Google Drive e Gmail
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/gmail.modify'
]

def autenticar_google():
    """
    Retorna os clients instanciados do Drive e do Gmail.
    """
    creds = None
    if os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        except Exception as e:
            logging.warning(f"Erro ao ler token.json: {e}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logging.warning(f"Erro ao renovar token: {e}. Forçando novo login.")
                creds = None
                if os.path.exists('token.json'):
                    os.remove('token.json')
                    
        if not creds or not creds.valid:
            if not os.path.exists('credentials.json'):
                logging.error("credentials.json não encontrado. Autenticação abortada.")
                raise FileNotFoundError("O arquivo credentials.json é obrigatório para rodar a automação do Google.")
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    try:
        drive_service = build('drive', 'v3', credentials=creds)
        gmail_service = build('gmail', 'v1', credentials=creds)
        return drive_service, gmail_service
    except Exception as e:
        logging.error(f"Erro crítico ao instanciar APIs do Google: {e}")
        raise

def buscar_e_processar_emails(gmail_service):
    """
    Pesquisa e-mails não lidos na label especificada e extrai o payload em HTML.
    """
    query = "label:Espelho_Magico is:unread"
    try:
        resultados = gmail_service.users().messages().list(userId='me', q=query).execute()
        mensagens = resultados.get('messages', [])
        
        if not mensagens:
            console.print("[yellow]Nenhum e-mail novo encontrado no marcador Espelho_Magico.[/yellow]")
            return []
            
        emails_processados = []
        for msg in mensagens:
            detalhes = gmail_service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            payload = detalhes.get('payload', {})
            
            def get_html_body(payload_part):
                if payload_part.get('mimeType') == 'text/html':
                    return payload_part.get('body', {}).get('data')
                if 'parts' in payload_part:
                    for part in payload_part['parts']:
                        res = get_html_body(part)
                        if res:
                            return res
                return payload_part.get('body', {}).get('data') if not payload_part.get('parts') else None

            corpo_codificado = get_html_body(payload)
                
                
            if corpo_codificado:
                html_limpo = base64.urlsafe_b64decode(corpo_codificado).decode('utf-8')
                headers = payload.get('headers', [])
                assunto = next((h['value'] for h in headers if h['name'] == 'Subject'), "Newsletter_Sem_Assunto")
                remetente = next((h['value'] for h in headers if h['name'] == 'From'), "Desconhecido")
                
                emails_processados.append({
                    "id": msg['id'],
                    "assunto": assunto,
                    "remetente": remetente,
                    "conteudo_html": html_limpo
                })
        return emails_processados
    except Exception as e:
        logging.error(f"Falha na comunicação com o Gmail: {e}")
        console.print(f"[bold red]✖ Erro no Gmail: {e}[/bold red]")
        return []

def marcar_email_como_lido(gmail_service, msg_id):
    """
    Remove a flag UNREAD de um e-mail específico.
    """
    try:
        gmail_service.users().messages().modify(
            userId='me', 
            id=msg_id, 
            body={'removeLabelIds': ['UNREAD']}
        ).execute()
        logging.info(f"E-mail {msg_id} marcado como lido.")
    except Exception as e:
        logging.error(f"Erro ao marcar mensagem {msg_id} como lida: {e}")

def upload_para_drive(service, caminho_arquivo, nome_arquivo):
    if not service:
        return
        
    # Garante que qualquer extensão seja removida para o nome no Drive
    import os
    nome_drive = os.path.splitext(nome_arquivo)[0]
    file_metadata = {'name': nome_drive}
    
    max_tentativas = 3
    for tentativa in range(1, max_tentativas + 1):
        try:
            # É recomendado instanciar o MediaFileUpload a cada tentativa para evitar problemas de cursor de arquivo
            media = MediaFileUpload(caminho_arquivo, mimetype='text/markdown', resumable=True)
            
            # Verifica se o arquivo já existe no Drive (com ou sem a extensão .md)
            query = f"name='{nome_drive}' or name='{nome_arquivo}'"
            results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
            items = results.get('files', [])
            
            if items:
                # Atualiza o arquivo existente (mantém o ID e atualiza nome e conteúdo)
                file_id = items[0]['id']
                console.print(f"[cyan]➜ [Drive] Arquivo existente encontrado. Atualizando (ID: {file_id})...[/cyan]")
                file = service.files().update(fileId=file_id, body=file_metadata, media_body=media, fields='id').execute(num_retries=3)
                console.print(f"[bold green]✔ Atualização concluída! ID no Drive: {file.get('id')}[/bold green]")
                
                # Se houver duplicatas antigas, avisa o usuário
                if len(items) > 1:
                    console.print(f"[yellow]⚠ Aviso: Foram encontradas {len(items)-1} duplicatas antigas no Drive com esse nome. Apenas a primeira foi atualizada.[/yellow]")
            else:
                # Cria um novo arquivo caso não exista
                console.print(f"[cyan]➜ [Drive] Fazendo upload de {nome_drive} (Tentativa {tentativa}/{max_tentativas})...[/cyan]")
                file = service.files().create(body=file_metadata, media_body=media, fields='id').execute(num_retries=3)
                console.print(f"[bold green]✔ Upload concluído! ID no Drive: {file.get('id')}[/bold green]")
            return
        except Exception as e:
            logging.error(f"Erro no upload/atualização para o Drive ({nome_arquivo}) - Tentativa {tentativa}: {e}")
            if tentativa < max_tentativas:
                console.print(f"[yellow]⚠ Falha no upload/atualização: {e}. Tentando novamente em 5 segundos...[/yellow]")
                time.sleep(5)
            else:
                console.print(f"[bold red]✖ Erro final no upload/atualização para o Drive após {max_tentativas} tentativas: {e}[/bold red]")

MAPEAMENTO_PREFIXOS = {
    "the news": "Boletim Fora de Ordem",
    "tns money": "Fechamento de Caixa",
    "tns health": "Diagnóstico Sincero",
    "tns sports": "Gol Contra",
    "tns business": "Ata de Reunião",
    "tns better work": "Ata de Reunião",
    "aidrops": "Alucinação Sintética",
    "notícias de segurança": "Hackeando o Problema",
    "notícias de entretenimento": "Pipoca Vencida",
    "notícias de games": "Controle Quebrado",
    "peak": "The Out of Order Dispatch",
}

def buscar_resultados_futebol(data_ontem_str, dia_semana_ontem):
    console.print(f"[cyan]➜ Buscando placares do dia {data_ontem_str} usando Gemini (Google Search)...[/cyan]")
    
    prompt = f"""
INSTRUÇÕES OBRIGATÓRIAS:
Você deve pesquisar na web os resultados dos principais jogos de futebol que ocorreram no dia {data_ontem_str} ({dia_semana_ontem}).
Retorne a resposta EXCLUSIVAMENTE no seguinte formato Markdown:

## RESULTADOS RECENTES ({data_ontem_str})

### [Nome do Campeonato]
[Time Mandante] [Placar] x [Placar] [Time Visitante]

Foque apenas nos principais campeonatos (Brasileirão, Copa do Brasil, Libertadores, Sul-Americana, Champions League, principais ligas europeias). Ignore amistosos irrelevantes.
NÃO adicione texto extra, saudações ou conversas, apenas a lista de resultados formatada em Markdown.
    """
    
    # A presença de "RESULTADOS RECENTES" no prompt ativará o Google Search no extrair_com_gemini
    try:
        conteudo_base = f"Encontre os resultados dos jogos de futebol do dia {data_ontem_str}."
        texto_resultados, _, _ = extrair_com_gemini(conteudo_base, prompt)
        return texto_resultados
    except Exception as e:
        return f"⚠️ Erro ao buscar resultados com Gemini: {e}"


def processar_lote_emails():
    tempo_inicio_total = time.time()
    console.print(Panel.fit("[bold cyan]Iniciando Extração com Espelho Mágico ✨ (Modo E-mail)[/bold cyan]"))
    
    try:
        drive_service, gmail_service = autenticar_google()
    except Exception as e:
        console.print(f"[bold red]Falha na autenticação do Google: {e}[/bold red]")
        return
        
    emails = buscar_e_processar_emails(gmail_service)
    if not emails:
        return

    prompt_espelho_magico = """
Você é o framework Espelho Mágico, um EXTRATOR DE DADOS rigoroso.
Sua única missão é analisar este documento (newsletter) e extrair o conteúdo informativo (títulos, notícias, agendas, quick takes e enquetes), removendo todo o resto.
O resultado alimentará um podcast gerado por IA (NotebookLM).

CRÍTICO: EXTRAIA ABSOLUTAMENTE TODAS AS NOTÍCIAS. NÃO PARE NO MEIO DO DOCUMENTO. PERCORRA O HTML ATÉ O FINAL E GARANTA QUE NENHUMA NOTÍCIA FIQUE DE FORA!

CRITÉRIO ZERO (PROIBIDO RESUMIR):
- Você NÃO É um redator. Você é um EXTRATOR. 
- NUNCA resuma, reescreva, condense, parafraseie ou altere as palavras originais das notícias.
- A extração do texto da notícia DEVE SER UMA CÓPIA LITERAL (ipsis litteris) e na íntegra. Copie e cole o texto original. Se alterar uma palavra, você falhou.
- EXCEÇÃO DE FORMATAÇÃO: O PDF frequentemente quebra as linhas no meio das frases. Você DEVE juntar essas quebras de linha artificiais para formar parágrafos contínuos, fluidos e normais. Não deixe frases cortadas ao meio por quebras de linha (`\n`).

REGRAS DE FILTRAGEM (O QUE IGNORAR COMPLETAMENTE):
- EXCLUA a seção/notícia INTEIRA APENAS SE for uma manchete muito curta ("isca de clique") que depende de um link para fazer sentido ("LEIA MAIS AQUI", "No que estamos clicando"). Se a notícia for completa e detalhada, mas apenas tiver um link "(Aprofunde)" ou "Leia mais" no final, MANTENHA a notícia toda e APAGUE APENAS o texto do link.
- EXCLUA qualquer texto/imagem que seja publicidade, patrocínio, venda de produtos, propagandas ("oferecimento", "Apresentado por").
- EXCLUA cabeçalhos, rodapés, números de página, textos legais, e links de redes sociais.
- EXCLUA saudações diárias (ex: "Bom dia", "Boa noite"), introduções pessoais/filosóficas dos editores, e a seção "OPINIÃO DO LEITOR" (ou qualquer enquete sobre a própria newsletter).
- EXCLUA cabeçalhos de e-mail completos (remetente, destinatário, data, assunto, "Leia Online", "Responder a:").
- EXCLUA seções de entretenimento vazio sem substância noticiosa: "AROUND THE WEB", "DESCONTRAIR", "DÊNIUS GAMES", "RECADO DO TIME" e equivalentes.
- EXCLUA "PROGRAMA DE INDICAÇÃO", links de indicação pessoal ("Seu link pessoal:"), e qualquer seção "QUEM SOMOS" ou descrição da própria newsletter.
- EXCLUA boas-vindas a novos assinantes (ex: "Boas-vindas às X novas pessoas...") e CTAs de venda de assinatura (ex: "Assine a PRO para continuar lendo").
- EXCLUA "NA EDIÇÃO DE HOJE" (lista de manchetes resumidas no início) — o conteúdo completo já virá nas seções seguintes.
- MANTENHA as seções de "QUICK TAKES", "AGENDA DO DIA", "ENQUETE DO DIA" e os "RESULTADOS" de enquetes (desde que sejam sobre esportes ou notícias factuais). Copie-as exatamente como estão.

TRATAMENTO DE IMAGENS:
- Para cada imagem de uma notícia mantida, crie uma descrição rica, vívida e detalhada.
- Formato obrigatório: `[Descrição da Imagem: detalhamento da cena e textos dentro da imagem...]`
- Ignore imagens que sejam apenas logotipos soltos, patrocinadores ou grafismos decorativos.

REGRAS DE FORMATAÇÃO MARKDOWN:
- Entregue o resultado EXCLUSIVAMENTE em Markdown limpo.
- NÃO crie saudações ou conversas na resposta (ex: "Aqui está o texto..."). Vá direto ao ponto.
- Use o nível 1 (`#`) APENAS para o título principal (Data do documento). Use nível 2 (`##`) ou inferior para as notícias.
- Gere titles únicos. Adicione contexto no título se houver repetição (ex: `## NBA - Lakers`).
- NUNCA repita o mesmo título de notícia. Se um texto foi quebrado e a notícia continua, prossiga organicamente sem inventar um novo cabeçalho.
- NUNCA coloque pontuação no final dos títulos (ex: `!`, `?`, `.`).
- Deixe sempre UMA linha em branco antes e depois de listas.
- Deixe espaços ao redor dos pipes de tabelas (ex: `| --- | --- |`).
- Use apenas UM espaço após o marcador de lista (ex: `- item`).
- NUNCA repita trechos de texto acidentalmente. Revise sua formatação.
    """

    resultados = []
    pasta_resultados = "resultados_markdown"
    os.makedirs(pasta_resultados, exist_ok=True)
    
    # Agrupar emails pelos prefixos no assunto
    grupos = {}
    for email in emails:
        assunto = email['assunto'].lower()
        remetente = email.get('remetente', '').lower()
        chave_encontrada = None
        for prefixo in MAPEAMENTO_PREFIXOS:
            if prefixo in remetente or assunto.startswith(prefixo):
                chave_encontrada = prefixo
                break
        
        if chave_encontrada:
            if chave_encontrada not in grupos:
                grupos[chave_encontrada] = []
            grupos[chave_encontrada].append(email)
        else:
            grupos[assunto] = [email] # Fallback: email sem prefixo mapeado
            
    # Processar cada grupo
    for chave, emails_grupo in grupos.items():
        if chave in MAPEAMENTO_PREFIXOS:
            titulo_base = MAPEAMENTO_PREFIXOS[chave]
            console.print(f"\n[bold magenta]Encontrados e-mails para: {titulo_base}[/bold magenta]")
            
            padrao_busca = os.path.join(pasta_resultados, f"{titulo_base} #*.md")
            arquivos_existentes = glob.glob(padrao_busca)
            
            maior_numero = 0
            for arq in arquivos_existentes:
                nome_arq = os.path.basename(arq)
                match = re.search(r'#(\d+)\.md$', nome_arq)
                if match:
                    num = int(match.group(1))
                    if num > maior_numero:
                        maior_numero = num
            
            if maior_numero > 0:
                numero = str(maior_numero + 1)
                console.print(f"[bold green]▶ Número da edição automatizado: {numero} (Última encontrada: {maior_numero})[/bold green]")
            elif sys.stdin.isatty():
                numero = input(f"▶ Qual o número da edição para '{titulo_base}'? ")
            else:
                numero = time.strftime("%Y%m%d")
                console.print(f"[yellow]▶ Sem terminal interativo. Usando data como número: {numero}[/yellow]")
                
            nome_final = f"{titulo_base} #{numero.strip()}.md"
        else:
            titulo_base = re.sub(r'[\\/*?:"<>|]', "", chave)[:50]
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            nome_final = f"{titulo_base}_{timestamp}.md"
            
        console.print(f"\n[bold blue]Processando para gerar: {nome_final}[/bold blue]")
        
        unificar_inteligente = False
        if len(emails_grupo) >= 2 and sys.stdin.isatty():
            console.print(f"\n[bold yellow]Foram encontrados {len(emails_grupo)} e-mails para '{titulo_base}'.[/bold yellow]")
            resposta = input("▶ Deseja unificar a extração de forma inteligente em um único texto (mesclando assuntos e evitando repetições)? (S/N): ").strip().lower()
            if resposta == 's':
                unificar_inteligente = True

def processar_grupo_isolado(chave, emails_grupo, titulo_base, nome_final, unificar_inteligente, pasta_resultados, prompt_espelho_magico, drive_service, gmail_service):
    textos_grupo = []
    emails_sucesso = []
    
    # Lógica do Gol Contra (tns sports) rodada apenas uma vez por grupo
    texto_resultados_grupo = ""
    if chave == "tns sports":
        hoje = datetime.datetime.now()
        ontem = hoje - datetime.timedelta(days=1)
        arquivo_controle = "controle_gol.json"
        ultima_data = None
        
        if os.path.exists(arquivo_controle):
            try:
                with open(arquivo_controle, 'r', encoding='utf-8') as f:
                    dados_controle = json.load(f)
                    ultima_data_str = dados_controle.get("ultima_data_extraida")
                    if ultima_data_str:
                        ultima_data = datetime.datetime.strptime(ultima_data_str, "%Y-%m-%d").date()
            except Exception as e:
                with print_lock: console.print(f"[yellow]⚠ Erro ao ler {arquivo_controle}: {e}[/yellow]")

        if not ultima_data:
            ultima_data = ontem.date() - datetime.timedelta(days=1)

        datas_para_buscar = []
        data_atual = ultima_data + datetime.timedelta(days=1)
        
        while data_atual <= ontem.date():
            datas_para_buscar.append(data_atual)
            data_atual += datetime.timedelta(days=1)

        resultados_lista = []
        dias_semana = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        
        for data_busca in datas_para_buscar:
            data_str_br = data_busca.strftime("%d/%m/%Y")
            dia_semana = dias_semana[data_busca.weekday()]
            resultado_dia = buscar_resultados_futebol(data_str_br, dia_semana)
            if resultado_dia:
                resultados_lista.append(resultado_dia)
                
        texto_resultados_grupo = "\n\n".join(resultados_lista)
        
        try:
            with open(arquivo_controle, 'w', encoding='utf-8') as f:
                json.dump({"ultima_data_extraida": ontem.strftime("%Y-%m-%d")}, f)
        except Exception as e:
            with print_lock: console.print(f"[yellow]⚠ Erro ao salvar {arquivo_controle}: {e}[/yellow]")

    if unificar_inteligente:
        prompt_atual = prompt_espelho_magico + """

ATENÇÃO - MODO UNIFICAÇÃO INTELIGENTE:
Foram fornecidas MÚLTIPLAS edições desta newsletter abaixo (separadas por "--- INÍCIO DA EDIÇÃO ---").
Sua missão adicional é:
- Unificar as notícias comuns e organizá-las de forma coesa.
- NÃO repetir a mesma notícia ou assuntos idênticos. Se duas edições falarem do mesmo tema, faça a fusão completa das informações.
- Manter o rigor de não resumir ou alterar os fatos originais, mas agrupar informações que se complementam.
- Garantir que o resultado final pareça uma única edição compilada e completa, sem redundâncias.
"""
        conteudo_combinado = ""
        for i, email in enumerate(emails_grupo):
            conteudo_combinado += f"\n\n--- INÍCIO DA EDIÇÃO {i+1} ---\n\n{email['conteudo_html']}"
        
        with print_lock: console.print(f"[cyan]➜ Enviando {len(emails_grupo)} e-mails agrupados para extração unificada ({titulo_base})...[/cyan]")
        texto = processar_conteudo_html(conteudo_combinado, prompt_atual, chave)
        if texto and not str(texto).startswith("ERRO:"):
            texto_final = (texto_resultados_grupo + "\n\n---\n\n" + texto.strip()) if texto_resultados_grupo else texto.strip()
            textos_grupo.append(texto_final)
            emails_sucesso.extend(emails_grupo)
    else:
        for i, email in enumerate(emails_grupo):
            prompt_atual = prompt_espelho_magico
            texto = processar_conteudo_html(email['conteudo_html'], prompt_atual, chave)
            if texto and not str(texto).startswith("ERRO:"):
                # Adiciona texto_resultados_grupo apenas no primeiro e-mail (para não repetir)
                if i == 0 and texto_resultados_grupo:
                    texto_final = texto_resultados_grupo + "\n\n---\n\n" + texto.strip()
                else:
                    texto_final = texto.strip()
                textos_grupo.append(texto_final)
                emails_sucesso.append(email)
            time.sleep(2)
        
    if textos_grupo:
        texto_final_mesclado = "\n\n---\n\n".join(textos_grupo)
        
        linhas = texto_final_mesclado.split("\n")
        linhas_processadas = []
        titulos_vistos = set()
        for linha in linhas:
            match_heading = re.match(r'^(#{2,})\s+(.*)$', linha)
            if match_heading:
                titulo = match_heading.group(2).strip().lower()
                if titulo in titulos_vistos:
                    continue
                titulos_vistos.add(titulo)
            linhas_processadas.append(linha)
        
        texto_final_mesclado = "\n".join(linhas_processadas)
        
        caminho_final = os.path.join(pasta_resultados, nome_final)
        
        with open(caminho_final, "w", encoding="utf-8") as f:
            f.write(texto_final_mesclado + "\n")
            
        with print_lock: console.print(f"[bold green]✔[/bold green] Salvo: [bold]{caminho_final}[/bold]")
        
        if drive_service:
            upload_para_drive(drive_service, caminho_final, nome_final)
            
        for email in emails_sucesso:
            marcar_email_como_lido(gmail_service, email['id'])
            
        return f"✅ {nome_final} gerado com sucesso!"
    else:
        with print_lock: console.print(f"[bold red]✖ Falha ao gerar texto para {nome_final}[/bold red]")
        return f"❌ Erro ao gerar {nome_final}"

def processar_lote_emails():
    tempo_inicio_total = time.time()
    console.print(Panel.fit("[bold cyan]Iniciando Extração com Espelho Mágico ✨ (Modo E-mail e Multithread)[/bold cyan]"))
    
    try:
        drive_service, gmail_service = autenticar_google()
    except Exception as e:
        console.print(f"[bold red]Falha na autenticação do Google: {e}[/bold red]")
        return
        
    emails = buscar_e_processar_emails(gmail_service)
    if not emails:
        return

    prompt_espelho_magico = """
Você é o framework Espelho Mágico, um EXTRATOR DE DADOS rigoroso.
Sua única missão é analisar este documento (newsletter) e extrair o conteúdo informativo (títulos, notícias, agendas, quick takes e enquetes), removendo todo o resto.
O resultado alimentará um podcast gerado por IA (NotebookLM).

CRÍTICO: EXTRAIA ABSOLUTAMENTE TODAS AS NOTÍCIAS. NÃO PARE NO MEIO DO DOCUMENTO. PERCORRA O HTML ATÉ O FINAL E GARANTA QUE NENHUMA NOTÍCIA FIQUE DE FORA!

CRITÉRIO ZERO (PROIBIDO RESUMIR):
- Você NÃO É um redator. Você é um EXTRATOR. 
- NUNCA resuma, reescreva, condense, parafraseie ou altere as palavras originais das notícias.
- A extração do texto da notícia DEVE SER UMA CÓPIA LITERAL (ipsis litteris) e na íntegra. Copie e cole o texto original. Se alterar uma palavra, você falhou.
- EXCEÇÃO DE FORMATAÇÃO: O PDF frequentemente quebra as linhas no meio das frases. Você DEVE juntar essas quebras de linha artificiais para formar parágrafos contínuos, fluidos e normais. Não deixe frases cortadas ao meio por quebras de linha (`\n`).

REGRAS DE FILTRAGEM (O QUE IGNORAR COMPLETAMENTE):
- EXCLUA a seção/notícia INTEIRA APENAS SE for uma manchete muito curta ("isca de clique") que depende de um link para fazer sentido ("LEIA MAIS AQUI", "No que estamos clicando"). Se a notícia for completa e detalhada, mas apenas tiver um link "(Aprofunde)" ou "Leia mais" no final, MANTENHA a notícia toda e APAGUE APENAS o texto do link.
- EXCLUA qualquer texto/imagem que seja publicidade, patrocínio, venda de produtos, propagandas ("oferecimento", "Apresentado por").
- EXCLUA cabeçalhos, rodapés, números de página, textos legais, e links de redes sociais.
- EXCLUA saudações diárias (ex: "Bom dia", "Boa noite"), introduções pessoais/filosóficas dos editores, e a seção "OPINIÃO DO LEITOR" (ou qualquer enquete sobre a própria newsletter).
- EXCLUA cabeçalhos de e-mail completos (remetente, destinatário, data, assunto, "Leia Online", "Responder a:").
- EXCLUA seções de entretenimento vazio sem substância noticiosa: "AROUND THE WEB", "DESCONTRAIR", "DÊNIUS GAMES", "RECADO DO TIME" e equivalentes.
- EXCLUA "PROGRAMA DE INDICAÇÃO", links de indicação pessoal ("Seu link pessoal:"), e qualquer seção "QUEM SOMOS" ou descrição da própria newsletter.
- EXCLUA boas-vindas a novos assinantes (ex: "Boas-vindas às X novas pessoas...") e CTAs de venda de assinatura (ex: "Assine a PRO para continuar lendo").
- EXCLUA "NA EDIÇÃO DE HOJE" (lista de manchetes resumidas no início) — o conteúdo completo já virá nas seções seguintes.
- MANTENHA as seções de "QUICK TAKES", "AGENDA DO DIA", "ENQUETE DO DIA" e os "RESULTADOS" de enquetes (desde que sejam sobre esportes ou notícias factuais). Copie-as exatamente como estão.

TRATAMENTO DE IMAGENS:
- Para cada imagem de uma notícia mantida, crie uma descrição rica, vívida e detalhada.
- Formato obrigatório: `[Descrição da Imagem: detalhamento da cena e textos dentro da imagem...]`
- Ignore imagens que sejam apenas logotipos soltos, patrocinadores ou grafismos decorativos.

REGRAS DE FORMATAÇÃO MARKDOWN:
- Entregue o resultado EXCLUSIVAMENTE em Markdown limpo.
- NÃO crie saudações ou conversas na resposta (ex: "Aqui está o texto..."). Vá direto ao ponto.
- Use o nível 1 (`#`) APENAS para o título principal (Data do documento). Use nível 2 (`##`) ou inferior para as notícias.
- Gere titles únicos. Adicione contexto no título se houver repetição (ex: `## NBA - Lakers`).
- NUNCA repita o mesmo título de notícia. Se um texto foi quebrado e a notícia continua, prossiga organicamente sem inventar um novo cabeçalho.
- NUNCA coloque pontuação no final dos títulos (ex: `!`, `?`, `.`).
- Deixe sempre UMA linha em branco antes e depois de listas.
- Deixe espaços ao redor dos pipes de tabelas (ex: `| --- | --- |`).
- Use apenas UM espaço após o marcador de lista (ex: `- item`).
- NUNCA repita trechos de texto acidentalmente. Revise sua formatação.
    """

    resultados = []
    pasta_resultados = "resultados_markdown"
    os.makedirs(pasta_resultados, exist_ok=True)
    
    # Agrupar emails pelos prefixos no assunto
    grupos = {}
    for email in emails:
        assunto = email['assunto'].lower()
        remetente = email.get('remetente', '').lower()
        chave_encontrada = None
        for prefixo in MAPEAMENTO_PREFIXOS:
            if prefixo in remetente or assunto.startswith(prefixo):
                chave_encontrada = prefixo
                break
        
        if chave_encontrada:
            if chave_encontrada not in grupos:
                grupos[chave_encontrada] = []
            grupos[chave_encontrada].append(email)
        else:
            grupos[assunto] = [email] # Fallback: email sem prefixo mapeado
            
    # Etapa 1: Interação sequencial com o usuário
    configs_grupos = []
    for chave, emails_grupo in grupos.items():
        if chave in MAPEAMENTO_PREFIXOS:
            titulo_base = MAPEAMENTO_PREFIXOS[chave]
            console.print(f"\n[bold magenta]Encontrados e-mails para: {titulo_base}[/bold magenta]")
            
            padrao_busca = os.path.join(pasta_resultados, f"{titulo_base} #*.md")
            arquivos_existentes = glob.glob(padrao_busca)
            
            maior_numero = 0
            for arq in arquivos_existentes:
                nome_arq = os.path.basename(arq)
                match = re.search(r'#(\d+)\.md$', nome_arq)
                if match:
                    num = int(match.group(1))
                    if num > maior_numero:
                        maior_numero = num
            
            if maior_numero > 0:
                numero = str(maior_numero + 1)
                console.print(f"[bold green]▶ Número da edição automatizado: {numero} (Última encontrada: {maior_numero})[/bold green]")
            elif sys.stdin.isatty():
                numero = input(f"▶ Qual o número da edição para '{titulo_base}'? ")
            else:
                numero = time.strftime("%Y%m%d")
                console.print(f"[yellow]▶ Sem terminal interativo. Usando data como número: {numero}[/yellow]")
                
            nome_final = f"{titulo_base} #{numero.strip()}.md"
        else:
            titulo_base = re.sub(r'[\\/*?:"<>|]', "", chave)[:50]
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            nome_final = f"{titulo_base}_{timestamp}.md"
            
        unificar_inteligente = False
        if len(emails_grupo) >= 2 and sys.stdin.isatty():
            console.print(f"[bold yellow]Foram encontrados {len(emails_grupo)} e-mails para '{titulo_base}'.[/bold yellow]")
            resposta = input("▶ Deseja unificar a extração de forma inteligente em um único texto (mesclando assuntos e evitando repetições)? (S/N): ").strip().lower()
            if resposta == 's':
                unificar_inteligente = True
                
        configs_grupos.append({
            "chave": chave,
            "emails_grupo": emails_grupo,
            "titulo_base": titulo_base,
            "nome_final": nome_final,
            "unificar_inteligente": unificar_inteligente
        })
        
    console.print(f"\n[bold green]Iniciando processamento paralelo de {len(configs_grupos)} grupos de e-mails...[/bold green]\n")
        
    # Etapa 2: Processamento Paralelo de todos os grupos
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        
        task_id = progress.add_task("[cyan]Extraindo com IA...", total=len(configs_grupos))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(configs_grupos), 5)) as executor:
            futuros = {
                executor.submit(
                    processar_grupo_isolado,
                    cfg["chave"],
                    cfg["emails_grupo"],
                    cfg["titulo_base"],
                    cfg["nome_final"],
                    cfg["unificar_inteligente"],
                    pasta_resultados,
                    prompt_espelho_magico,
                    drive_service,
                    gmail_service
                ): cfg for cfg in configs_grupos
            }
            
            for futuro in concurrent.futures.as_completed(futuros):
                cfg = futuros[futuro]
                try:
                    resultado = futuro.result()
                    resultados.append(resultado)
                except Exception as e:
                    resultados.append(f"❌ Erro crítico no grupo {cfg['titulo_base']}: {e}")
                
                progress.advance(task_id)

    tempo_total = time.time() - tempo_inicio_total
    minutos = int(tempo_total // 60)
    segundos = int(tempo_total % 60)
    
    console.print("\n[bold cyan]Resumo do Processamento:[/bold cyan]")
    for r in resultados:
        console.print(r)
    console.print(f"\n[bold cyan]⏱ Tempo total: {minutos}min {segundos}s[/bold cyan]")
    logging.info(f"Processamento concluído em {minutos}min {segundos}s. {len(resultados)} grupo(s) processado(s).")

if __name__ == "__main__":
    processar_lote_emails()
