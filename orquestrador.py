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
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from markitdown import MarkItDown

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

import groq
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
groq_api_key = os.environ.get("GROQ_API_KEY")
perplexity_api_key = os.environ.get("PERPLEXITY_API_KEY")

if not api_keys:
    console.print("[bold red]Erro:[/bold red] Nenhuma GEMINI_API_KEY encontrada no .env.")
    exit(1)

clientes_gemini = [genai.Client(api_key=k) for k in api_keys]
indice_cliente_atual = 0
client_groq = groq.Groq(api_key=groq_api_key) if groq_api_key else None

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

def extrair_com_perplexity(caminho_pdf, prompt):
    if not perplexity_api_key:
        raise Exception("PERPLEXITY_API_KEY ausente.")
        
    console.print(f"[cyan]➜ [1/2] Lendo e convertendo {caminho_pdf} para Perplexity...[/cyan]")
    texto = converter_pdf_com_fallback(caminho_pdf)
    
    console.print(f"[cyan]➜ [2/2] Extraindo texto via Perplexity...[/cyan]")
    modelo_perplexity = os.environ.get("PERPLEXITY_MODEL", "sonar-pro")
    
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {perplexity_api_key}",
        "Content-Type": "application/json"
    }
    
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Filtre e extraia os dados deste texto:\n\n{texto}"}
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
        erro_detalhado = ""
        if response is not None and hasattr(response, "text"):
            erro_detalhado = response.text
        raise Exception(f"Erro na API do Perplexity: {e} - {erro_detalhado}")

def extrair_com_groq(caminho_pdf, prompt):
    if not client_groq:
        raise Exception("GROQ_API_KEY ausente.")
        
    console.print(f"[cyan]➜ [1/2] Lendo e convertendo {caminho_pdf} para Groq...[/cyan]")
    texto = converter_pdf_com_fallback(caminho_pdf)
    
    # Dividir texto em chunks para evitar rate limit de TPM (Tokens per minute)
    # Reduzindo para 3500 caracteres (aprox. 800 tokens) e dividindo por parágrafos
    TAMANHO_MAX = 3500 
    chunks = []
    chunk_atual = ""
    for paragrafo in texto.split("\n\n"):
        if len(chunk_atual) + len(paragrafo) > TAMANHO_MAX:
            chunks.append(chunk_atual)
            chunk_atual = paragrafo + "\n\n"
        else:
            chunk_atual += paragrafo + "\n\n"
    if chunk_atual:
        chunks.append(chunk_atual)
    
    if len(chunks) > 1:
        console.print(f"[yellow]  ⚠ O PDF convertido é muito grande. Dividido em {len(chunks)} partes. Haverá pausas de 1 min entre as partes (Rate Limit).[/yellow]")
    
    console.print(f"[cyan]➜ [2/2] Extraindo texto via Groq...[/cyan]")
    texto_completo = ""
    modelo_groq = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    
    for i, chunk in enumerate(chunks):
        if i > 0:
            console.print(f"[yellow]  ⏳ Aguardando 61s para o reset do limite de TPM do Groq (Iniciando Parte {i+1}/{len(chunks)})...[/yellow]")
            time.sleep(61)
            
        prompt_para_chunk = prompt
        # O Groq não possui a ferramenta de busca do Google (google_search).
        # Removemos a instrução em todas as partes para evitar erro 400 (tool_use_failed).
        if "INSTRUÇÃO ADICIONAL EXCLUSIVA PARA ESTA EDIÇÃO:" in prompt_para_chunk:
            prompt_para_chunk = prompt_para_chunk.split("INSTRUÇÃO ADICIONAL EXCLUSIVA PARA ESTA EDIÇÃO:")[0]
            
        messages = [
            {"role": "system", "content": prompt_para_chunk},
            {"role": "user", "content": f"Filtre e extraia os dados desta parte do texto:\n\n{chunk}"}
        ]
        
        for continuacao in range(4): # Limite de continuações
            chat_completion = client_groq.chat.completions.create(
                messages=messages,
                model=modelo_groq,
                temperature=0.2,
                max_tokens=2048,
            )
            msg_content = chat_completion.choices[0].message.content
            texto_completo += msg_content + "\n\n"
            
            if chat_completion.choices[0].finish_reason != "length":
                break
                
            console.print(f"[yellow]  ⚠ Cortado pelo limite. Aguardando 61s para o reset do TPM antes de continuar...[/yellow]")
            messages.append({"role": "assistant", "content": msg_content})
            messages.append({"role": "user", "content": "Continue exatamente de onde parou."})
            time.sleep(61)
            
    return texto_completo, None

def extrair_com_gemini(caminho_pdf, prompt, modelo_atual="gemini-2.5-flash"):
    global indice_cliente_atual
    
    ultimo_erro = "Erro desconhecido"
    
    console.print(f"[cyan]➜ [1/2] Extraindo texto do PDF localmente (poupando cota de upload)...[/cyan]")
    texto_pdf = converter_pdf_com_fallback(caminho_pdf)
    
    # Balanceamento Proativo (Round-Robin)
    # A cada nova chamada de extração, alternamos para a próxima chave disponível
    if len(clientes_gemini) > 1:
        indice_cliente_atual = (indice_cliente_atual + 1) % len(clientes_gemini)
        
    # Tentativas baseadas estritamente na quantidade de chaves.
    tentativas_maximas = len(clientes_gemini)
    
    for tentativa in range(1, tentativas_maximas + 1):
        cliente_atual = clientes_gemini[indice_cliente_atual]
        try:
            console.print(f"[cyan]➜ [2/2] Processando com {modelo_atual} (Tentativa {tentativa}/{tentativas_maximas} | Chave {indice_cliente_atual + 1}/{len(clientes_gemini)})...[/cyan]")
            config_kwargs = {
                "system_instruction": prompt,
                "temperature": 0.2,
            }
            if "RESULTADOS RECENTES" in prompt:
                config_kwargs["tools"] = [{"google_search": {}}]

            response = cliente_atual.models.generate_content(
                model=modelo_atual,
                contents=[texto_pdf],
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
                
                if tentativa < tentativas_maximas:
                    console.print(f"[yellow]  ⚠ {ultimo_erro} na Chave {indice_cliente_atual + 1} no {modelo_atual}. Alternando para a próxima chave...[/yellow]")
                    indice_cliente_atual = (indice_cliente_atual + 1) % len(clientes_gemini)
                    continue
                else:
                    raise Exception(f"Falha ao usar {modelo_atual} após testar todas as {tentativas_maximas} chave(s). Motivo final: {ultimo_erro}")
            else:
                raise Exception(f"Erro na API ({modelo_atual}): {erro_str}")
        except Exception as e:
             raise Exception(f"Erro inesperado ({modelo_atual}): {str(e)}")
                
    raise Exception(f"Todas as tentativas falharam para o {modelo_atual}. Motivo final: {ultimo_erro}")

def processar_pdf_texto(caminho_pdf, prompt):
    texto_limpo = ""
    arquivo_nuvem = None
    cliente_usado = None
    
    logging.info(f"Processando {caminho_pdf}")
    
    modelos_para_testar = []
    modelo_env = os.environ.get("GEMINI_MODEL")
    modelo_fallback = os.environ.get("GEMINI_MODEL_FALLBACK")
    
    if modelo_env:
        modelos_para_testar.append(modelo_env)
    if modelo_fallback:
        modelos_para_testar.append(modelo_fallback)
        
    # Adicionando outros modelos gratuitos como fallback de segurança
    modelos_para_testar.extend(["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash"])
    
    # Remover duplicatas mantendo a ordem
    modelos_para_testar = list(dict.fromkeys(modelos_para_testar))
    
    ia_padrao = os.environ.get("IA_PADRAO", "gemini").lower()
    
    # Força o uso do Perplexity para os arquivos do "Gol Contra"
    if os.path.basename(caminho_pdf).lower().startswith("gol"):
        ia_padrao = "perplexity"
        
    if ia_padrao == "perplexity":
        try:
            texto_limpo, _ = extrair_com_perplexity(caminho_pdf, prompt)
            return texto_limpo
        except Exception as e:
            console.print(f"[yellow]⚠ Erro no Perplexity (IA_PADRAO): {e}. Tentando Gemini como fallback...[/yellow]")

    if ia_padrao == "groq":
        try:
            texto_limpo, _ = extrair_com_groq(caminho_pdf, prompt)
            return texto_limpo
        except Exception as e:
            console.print(f"[yellow]⚠ Erro no Groq (IA_PADRAO): {e}. Tentando Gemini como fallback...[/yellow]")
            
    try:
        for modelo in modelos_para_testar:
            try:
                texto_limpo, arquivo_nuvem, cliente_usado = extrair_com_gemini(caminho_pdf, prompt, modelo_atual=modelo)
                return texto_limpo
            except Exception as e:
                console.print(f"[yellow]  ⚠ Erro com modelo {modelo}: {e}. Tentando próxima versão...[/yellow]")
                continue
                
        if ia_padrao != "groq":
            msg_erro = f"⚠ Falha total em todos os modelos Gemini testados. Tentando Groq como último recurso..."
            logging.warning(msg_erro)
            console.print(f"[bold yellow]{msg_erro}[/bold yellow]")
            
            try:
                texto_limpo, _ = extrair_com_groq(caminho_pdf, prompt)
                return texto_limpo
            except Exception as e:
                msg_erro_final = f"❌ Erro bruto no Groq: {e}"
                logging.error(msg_erro_final)
                console.print(f"[bold red]✖[/bold red] {msg_erro_final}")
                return "ERRO: Falha em todos os modelos (Gemini e Groq)."
        else:
            return "ERRO: Falha no Groq e no Gemini."


    finally:
        if arquivo_nuvem and cliente_usado:
            try:
                cliente_usado.files.delete(name=arquivo_nuvem.name)
                logging.info(f"Arquivo removido do Google GenAI: {arquivo_nuvem.name}")
            except Exception as e:
                logging.error(f"Falha ao deletar arquivo na nuvem: {e}")

# Lógica de Integração com Google Drive
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def autenticar_drive():
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
                console.print("[yellow]⚠ credentials.json não encontrado na pasta do projeto. Upload para o Drive será ignorado.[/yellow]")
                logging.warning("credentials.json não encontrado. Upload para o Drive desabilitado.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)

def upload_para_drive(service, caminho_arquivo, nome_arquivo):
    if not service:
        return
        
    nome_drive = nome_arquivo[:-3] if nome_arquivo.endswith('.md') else nome_arquivo
    file_metadata = {'name': nome_drive}
    
    max_tentativas = 3
    for tentativa in range(1, max_tentativas + 1):
        try:
            # É recomendado instanciar o MediaFileUpload a cada tentativa para evitar problemas de cursor de arquivo
            media = MediaFileUpload(caminho_arquivo, mimetype='text/markdown', resumable=True)
            
            console.print(f"[cyan]➜ [Drive] Fazendo upload de {nome_drive} (Tentativa {tentativa}/{max_tentativas})...[/cyan]")
            # execute(num_retries=3) adiciona também o backoff interno da biblioteca do Google
            file = service.files().create(body=file_metadata, media_body=media, fields='id').execute(num_retries=3)
            console.print(f"[bold green]✔ Upload concluído! ID no Drive: {file.get('id')}[/bold green]")
            return
        except Exception as e:
            logging.error(f"Erro no upload para o Drive ({nome_arquivo}) - Tentativa {tentativa}: {e}")
            if tentativa < max_tentativas:
                console.print(f"[yellow]⚠ Falha no upload: {e}. Tentando novamente em 5 segundos...[/yellow]")
                time.sleep(5)
            else:
                console.print(f"[bold red]✖ Erro final no upload para o Drive após {max_tentativas} tentativas: {e}[/bold red]")

MAPEAMENTO_PREFIXOS = {
    "bole": "Boletim Fora de Ordem",
    "fecha": "Fechamento de Caixa",
    "diag": "Diagnóstico Sincero",
    "gol": "Gol Contra",
    "ata": "Ata de Reunião",
    "aluci": "Alucinação Sintética",
    "hack": "Hackeando o Problema",
    "pipo": "Pipoca Vencida",
    "contro": "Controle Quebrado",
    "peak": "The Out of Order Dispatch",
}

def buscar_resultados_futebol(data_ontem_str, dia_semana_ontem):
    console.print(f"[cyan]➜ Buscando placares do dia {data_ontem_str} na web (ESPN)...[/cyan]")
    
    # data_ontem_str format is expected to be "DD/MM/YYYY"
    try:
        data_espn = data_ontem_str.split("/")
        data_espn.reverse()
        data_espn_str = "".join(data_espn) # YYYYMMDD
        
        url = f"https://www.espn.com.br/futebol/resultados/_/data/{data_espn_str}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=15)
        
        match = re.search(r'window\[\'__espnfitt__\'\]=(.*?);</script>', r.text)
        if not match:
            return "⚠️ Não foi possível encontrar os dados esportivos no site."
            
        data = json.loads(match.group(1))
        leagues = data.get('page', {}).get('content', {}).get('scoreboard', {}).get('gmsByLeague', [])
        
        if not leagues:
            return f"## RESULTADOS RECENTES ({data_ontem_str})\n\nNenhum jogo importante encontrado nesta data."
            
        resultados_md = [f"## RESULTADOS RECENTES ({data_ontem_str})"]
        
        for league in leagues:
            league_name = league.get('league', {}).get('name', 'Campeonato')
            
            # Filtro opcional para remover amistosos ou campeonatos muito irrelevantes, se quiser. 
            # Mas a ESPN já filtra os principais primeiro.
            if "Amistoso" in league_name:
                continue # Pula amistosos para focar nos principais
                
            resultados_md.append(f"\n### {league_name}")
            
            for evt in league.get('evts', []):
                competitors = evt.get('competitors', [])
                if len(competitors) == 2:
                    # competitors list can have home/away in any order, they have 'isHome' boolean
                    team1 = competitors[0]
                    team2 = competitors[1]
                    
                    if team1.get('isHome'):
                        home, away = team1, team2
                    else:
                        home, away = team2, team1
                    
                    team_home = home.get('displayName', 'Time Mandante')
                    score_home = home.get('score', '')
                    
                    team_away = away.get('displayName', 'Time Visitante')
                    score_away = away.get('score', '')
                    
                    resultados_md.append(f"{team_home} {score_home} x {score_away} {team_away}")
                    
        return "\n".join(resultados_md)
    except Exception as e:
        return f"⚠️ Erro ao buscar resultados na web: {e}"


def processar_lote_pdfs():
    tempo_inicio_total = time.time()
    console.print(Panel.fit("[bold cyan]Iniciando Extração com Espelho Mágico ✨[/bold cyan]"))
    
    pdfs = sorted(glob.glob("*.pdf"))
    if not pdfs:
        console.print("[yellow]Nenhum arquivo PDF encontrado no diretório.[/yellow]")
        return

    drive_service = autenticar_drive()

    prompt_espelho_magico = """
Você é o framework Espelho Mágico, um EXTRATOR DE DADOS rigoroso.
Sua única missão é analisar este documento (newsletter) e extrair o conteúdo informativo (títulos, notícias, agendas, quick takes e enquetes), removendo todo o resto.
O resultado alimentará um podcast gerado por IA (NotebookLM).

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
- Gere títulos únicos. Adicione contexto no título se houver repetição (ex: `## NBA - Lakers`).
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
    
    # Agrupar PDFs pelos prefixos
    grupos = {}
    for pdf in pdfs:
        nome_base = os.path.basename(pdf).lower()
        chave_encontrada = None
        for prefixo in MAPEAMENTO_PREFIXOS:
            if nome_base.startswith(prefixo):
                chave_encontrada = prefixo
                break
        
        if chave_encontrada:
            if chave_encontrada not in grupos:
                grupos[chave_encontrada] = []
            grupos[chave_encontrada].append(pdf)
        else:
            grupos[pdf] = [pdf] # Fallback: arquivo sem prefixo mapeado
            
    # Processar cada grupo
    for chave, arquivos_grupo in grupos.items():
        if chave in MAPEAMENTO_PREFIXOS:
            titulo_base = MAPEAMENTO_PREFIXOS[chave]
            console.print(f"\n[bold magenta]Encontrados os arquivos: {', '.join(arquivos_grupo)}[/bold magenta]")
            
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
            titulo_base = os.path.splitext(chave)[0]
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            nome_final = f"{titulo_base}_{timestamp}.md"
            
        console.print(f"\n[bold blue]Processando para gerar: {nome_final}[/bold blue]")
        
        textos_grupo = []
        for pdf in arquivos_grupo:
            prompt_atual = prompt_espelho_magico
            texto_resultados = ""
            if chave == "gol":
                hoje = datetime.datetime.now()
                ontem = hoje - datetime.timedelta(days=1)
                
                # Controle de datas já extraídas
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
                        console.print(f"[yellow]⚠ Erro ao ler {arquivo_controle}: {e}[/yellow]")

                # Se não tem histórico, ajusta para buscar apenas o "ontem" na primeira vez
                if not ultima_data:
                    ultima_data = ontem.date() - datetime.timedelta(days=1)

                datas_para_buscar = []
                data_atual = ultima_data + datetime.timedelta(days=1)
                
                # Gera a lista de datas pendentes até ontem
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
                        
                texto_resultados = "\n\n".join(resultados_lista)
                
                # Atualiza o arquivo de controle
                try:
                    with open(arquivo_controle, 'w', encoding='utf-8') as f:
                        json.dump({"ultima_data_extraida": ontem.strftime("%Y-%m-%d")}, f)
                except Exception as e:
                    console.print(f"[yellow]⚠ Erro ao salvar {arquivo_controle}: {e}[/yellow]")
                
                # Não altera o prompt principal (prompt_atual) para que ele foque APENAS em ler o PDF.

            texto = processar_pdf_texto(pdf, prompt_atual)
            if texto and not str(texto).startswith("ERRO:"):
                texto_final = (texto_resultados + "\n\n---\n\n" + texto.strip()) if texto_resultados else texto.strip()
                textos_grupo.append(texto_final)
            time.sleep(2) # Respiro entre arquivos
            
        if textos_grupo:
            texto_final_mesclado = "\n\n---\n\n".join(textos_grupo)
            
            # Pós-processamento inteligente contra MD024
            linhas = texto_final_mesclado.split("\n")
            linhas_processadas = []
            titulos_vistos = set()
            for linha in linhas:
                # Se for um heading ## ou maior, rastrear repetições
                match_heading = re.match(r'^(#{2,})\s+(.*)$', linha)
                if match_heading:
                    titulo = match_heading.group(2).strip().lower()
                    if titulo in titulos_vistos:
                        continue # Pula a linha se for um título duplicado
                    titulos_vistos.add(titulo)
                linhas_processadas.append(linha)
            
            texto_final_mesclado = "\n".join(linhas_processadas)
            
            caminho_final = os.path.join(pasta_resultados, nome_final)
            
            with open(caminho_final, "w", encoding="utf-8") as f:
                f.write(texto_final_mesclado + "\n")
                
            console.print(f"[bold green]✔[/bold green] Salvo: [bold]{caminho_final}[/bold]")
            resultados.append(f"✅ {nome_final} gerado com sucesso!")
            
            if drive_service:
                upload_para_drive(drive_service, caminho_final, nome_final)
                
            for pdf in arquivos_grupo:
                try:
                    os.remove(pdf)
                    console.print(f"[bold yellow]🗑️ Arquivo {pdf} excluído com sucesso.[/bold yellow]")
                except Exception as e:
                    console.print(f"[bold red]✖ Erro ao excluir o arquivo {pdf}: {e}[/bold red]")
        else:
            console.print(f"[bold red]✖ Falha ao gerar texto para {nome_final}[/bold red]")
            resultados.append(f"❌ Erro ao gerar {nome_final}")

    # Resumo final com estatísticas de tempo
    tempo_total = time.time() - tempo_inicio_total
    minutos = int(tempo_total // 60)
    segundos = int(tempo_total % 60)
    
    console.print("\n[bold cyan]Resumo do Processamento:[/bold cyan]")
    for r in resultados:
        console.print(r)
    console.print(f"\n[bold cyan]⏱ Tempo total: {minutos}min {segundos}s[/bold cyan]")
    logging.info(f"Processamento concluído em {minutos}min {segundos}s. {len(resultados)} grupo(s) processado(s).")

if __name__ == "__main__":
    processar_lote_pdfs()