"""Pipeline: agrupamento de e-mails, numeração de edições, extração, pós-processamento e publicação."""
import concurrent.futures
import datetime
import email.utils
import json
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from rich import box
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import config, futebol
from .deduplicacao import (
    carregar_historico_noticias,
    deve_deduplicar_noticias,
    extrair_noticias_email,
    filtrar_email_noticias_repetidas,
    normalizar_chave_url,
    registrar_noticias_extraidas,
)
from .drive_client import upload_para_drive
from .gmail_client import autenticar_google, buscar_emails, marcar_como_lido
from .html_cleaner import limpar_linhas, preparar_conteudo
from .llm import (
    Extrator,
    FalhaExtracao,
    dividir_conteudo_com_metadados,
    modelo_eh_inferior,
    unir_partes_extraidas,
    verificar_completude,
)
from .log import console, contexto_grupo, escape, log_arquivo, logger
from .rss_client import coletar_feed_rss, janela_horaria_liberada
from .validacao import _numeros, aplicar_dupla_verificacao, contar_noticias, renderizar_relatorio

SEPARADOR = "\n\n---\n\n"
MINIMO_CARACTERES_EDICAO = 250
ETAPAS_GRUPO = 6  # pré-limpeza · IA · consolidação · dupla verificação · salvar · Drive/concluído

_INSTRUCAO_PARTE = (
    "<modo_parte>\n"
    "Este conteúdo é a PARTE {i} de {n} de uma mesma edição, fatiada por tamanho para caber no limite da IA. "
    "Extraia normalmente TODAS as notícias desta parte seguindo as regras acima, mas NÃO invente "
    "`## Enquete do Dia` / `## Daily Poll`: inclua enquete apenas se ela existir de fato nesta parte da fonte "
    "(a enquete final é garantida depois, na dupla verificação). "
    "NUNCA escreva avisos meta como 'A resposta oficial não consta nesta parte da edição' em quizzes/curiosidades: "
    "se o gabarito não estiver nesta parte, preencha `**Resposta:** ...` diretamente com a resposta factual.\n"
    "</modo_parte>"
)

_INSTRUCAO_SEGUNDA_PASSAGEM = (
    "<segunda_passagem_revisao>\n"
    "ATENÇÃO: A primeira tentativa deste trecho precisou usar um modelo leve de fallback. "
    "Reextraia o conteúdo abaixo do zero com rigor máximo do modelo principal:\n"
    "- Se este trecho for 100% comercial, cupom de desconto, oferta de Black Friday ou publieditorial sem notícia real, "
    "responda APENAS com `[SEM_CONTEUDO_EDITORIAL]`.\n"
    "- Para todas as notícias legítimas, copie 100% dos fatos, números, parágrafos e citações na íntegra (sem resumir).\n"
    "</segunda_passagem_revisao>"
)

_MESES_PT = (
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)
_MESES_EN = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_DIAS_SEMANA_PT = (
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
)
_DIAS_SEMANA_EN = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)
_RE_DIA_SEMANA_PT = re.compile(
    r"\b(?:segunda|ter[cç]a|quarta|quinta|sexta)(?:-feira)?\b|\bs[aá]bado\b|\bdomingo\b",
    re.I,
)
_RE_DIA_SEMANA_EN = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_ALIASES_PREFIXO = {
    "money drops": "moneydrops",
    "tech drops": "techdrops",
    "ai drops": "aidrops",
}
_FUSO_BRT = datetime.timezone(datetime.timedelta(hours=-3))


def formatar_data_com_dia_semana(dt, idioma="pt"):
    """Formata um `datetime.date` incluindo o dia da semana ('terça-feira, 24 de novembro de 2026' / 'Tuesday, November 24, 2026')."""
    if not dt:
        return ""
    if idioma == "en":
        return f"{_DIAS_SEMANA_EN[dt.weekday()]}, {_MESES_EN[dt.month]} {dt.day}, {dt.year}"
    return f"{_DIAS_SEMANA_PT[dt.weekday()]}, {dt.day} de {_MESES_PT[dt.month]} de {dt.year}"


def formatar_data_email(data_rfc2822, idioma="pt"):
    """Converte o cabeçalho Date do Gmail (RFC 2822) para 'DD de mês de AAAA' (pt) ou 'Month DD, YYYY' (en)."""
    if not data_rfc2822:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(data_rfc2822)
        if idioma == "en":
            return f"{_MESES_EN[dt.month]} {dt.day}, {dt.year}"
        return f"{dt.day} de {_MESES_PT[dt.month]} de {dt.year}"
    except Exception:
        return ""


@dataclass
class Opcoes:
    dry_run: bool = False
    unificar: str = "perguntar"  # "perguntar" | "sim" | "nao"
    grupos: list = field(default_factory=list)
    interativo: bool = True
    max_workers: int = 0
    ia: str = ""  # "" = IA_PADRAO do .env | "gemini" | "alt" | "perplexity"
    urls: list = field(default_factory=list)
    verificar_web: bool = True
    forcar_pares: bool = False


@dataclass
class ConfigGrupo:
    chave: str
    emails: list
    titulo_base: str
    nome_final: str
    unificar: bool = False
    chaves_fontes: list = field(default_factory=list)
    contexto_edicao_anterior: str = ""


@dataclass
class ItemStandBy:
    titulo_base: str
    fonte_recebida: str
    data_recebida: datetime.date
    aguardando: str
    data_esperada: datetime.date
    expira_em: datetime.date


@dataclass
class ResultadoGrupo:
    nome: str
    status: str  # "ok" | "parcial" | "erro" | "ignorado" | "dry"
    mensagem: str
    resumo: str = ""
    segundos: float = 0.0
    noticias: int = 0
    verificacao: str = ""
    tem_aviso: bool = False
    caracteres: int = 0

    def __str__(self):
        return self.mensagem


# --- Funções puras (testáveis) ------------------------------------------------

def carregar_prompt(nome):
    return (config.PROMPTS_DIR / f"{nome}.md").read_text(encoding="utf-8").strip()


def agrupar_emails(emails, mapeamento):
    """Agrupa por prefixo conhecido (no remetente ou no início do assunto); os demais ficam um por grupo."""
    grupos = {}
    for email_item in emails:
        fonte_explicita = (email_item.get("fonte_chave") or "").lower()
        if fonte_explicita and fonte_explicita in mapeamento:
            grupos.setdefault(fonte_explicita, []).append(email_item)
            continue
        assunto = email_item["assunto"].lower()
        remetente = email_item.get("remetente", "").lower()
        chave = next((p for p in mapeamento if p in remetente or assunto.startswith(p)), None)
        if chave is None:
            for alias, destino in _ALIASES_PREFIXO.items():
                if destino in mapeamento and (alias in remetente or assunto.startswith(alias)):
                    chave = destino
                    break
        grupos.setdefault(chave or assunto, []).append(email_item)
    return grupos


def maior_edicao_existente(pasta, titulo_base):
    maior = 0
    for arq in Path(pasta).glob(f"{glob_escape(titulo_base)} #*.md"):
        m = re.search(r"#(\d+)\.md$", arq.name)
        if m:
            maior = max(maior, int(m.group(1)))
    return maior


def glob_escape(texto):
    return re.sub(r"([\[\]\*\?])", r"[\1]", texto)


def nome_fallback(assunto, agora=None):
    agora = agora or datetime.datetime.now()
    base = re.sub(r'[\\/*?:"<>|]', "", assunto)[:50].strip() or "newsletter"
    return base, f"{base}_{agora:%Y%m%d_%H%M%S}.md"


_SLUG_URL_PARA_PREFIXO = {
    "/newsletter/the-news": "the news",
    "/newsletter/sports": "tns sports",
    "/newsletter/money": "tns money",
    "/newsletter/health": "tns health",
    "/newsletter/business": "tns business",
    "/newsletter/better-work": "tns better work",
    "/newsletter/betterwork": "tns better work",
}

_MAPA_MESES_NOME = {
    "janeiro": 1, "jan": 1, "january": 1,
    "fevereiro": 2, "fev": 2, "feb": 2, "february": 2,
    "março": 3, "marco": 3, "mar": 3, "march": 3,
    "abril": 4, "abr": 4, "apr": 4, "april": 4,
    "maio": 5, "mai": 5, "may": 5,
    "junho": 6, "jun": 6, "june": 6,
    "julho": 7, "jul": 7, "july": 7,
    "agosto": 8, "ago": 8, "aug": 8, "august": 8,
    "setembro": 9, "set": 9, "sep": 9, "sept": 9, "september": 9,
    "outubro": 10, "out": 10, "oct": 10, "october": 10,
    "novembro": 11, "nov": 11, "november": 11,
    "dezembro": 12, "dez": 12, "dec": 12, "december": 12,
}

_RE_DATA_PT = re.compile(
    r"\b(\d{1,2})\s+de\s+(janeiro|fevereiro|março|marco|abril|maio|junho|julho|"
    r"agosto|setembro|outubro|novembro|dezembro)\s+de\s+(20\d{2})\b",
    re.I,
)
_RE_DATA_EN = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.I,
)
_RE_DATA_DMY = re.compile(r"\b(\d{2})[-/](\d{2})[-/](20\d{2})\b")
_RE_DATA_YMD = re.compile(r"\b(20\d{2})[-/](\d{2})[-/](\d{2})\b")


def extrair_data_de_texto(texto):
    """Extrai um `datetime.date` de strings em PT ('10 de novembro de 2026'), EN ('Nov 10, 2026'), DD-MM-AAAA ou AAAA-MM-DD."""
    if not texto:
        return None
    m = _RE_DATA_PT.search(texto)
    if m:
        try:
            return datetime.date(int(m.group(3)), _MAPA_MESES_NOME[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            pass
    m = _RE_DATA_EN.search(texto)
    if m:
        try:
            return datetime.date(int(m.group(3)), _MAPA_MESES_NOME[m.group(1).lower().rstrip(".")], int(m.group(2)))
        except ValueError:
            pass
    m = _RE_DATA_DMY.search(texto)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = _RE_DATA_YMD.search(texto)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def ultima_data_extraida_local(pasta, titulo_base):
    """Lê os últimos arquivos `.md` de `titulo_base` em `pasta` e devolve a maior `datetime.date` do cabeçalho `# Data`."""
    arquivos = []
    for arq in Path(pasta).glob(f"{glob_escape(titulo_base)} #*.md"):
        m = re.search(r"#(\d+)\.md$", arq.name)
        if m:
            arquivos.append((int(m.group(1)), arq))
    if not arquivos:
        return None
    arquivos.sort(key=lambda par: par[0], reverse=True)
    datas = []
    for _, arq in arquivos[:3]:
        try:
            primeiras = "\n".join(arq.read_text(encoding="utf-8", errors="ignore").splitlines()[:5])
            dt = extrair_data_de_texto(primeiras)
            if dt:
                datas.append(dt)
        except Exception:
            continue
    return max(datas) if datas else None


def _data_para_rfc2822(data_date):
    dt = datetime.datetime(data_date.year, data_date.month, data_date.day, 9, 0, tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(dt)


def _inferir_data_de_url(url, html="", data_forcada=None):
    """Extrai data DD-MM-AAAA, AAAA-MM-DD ou por extenso da URL/HTML e devolve em formato RFC 2822."""
    if isinstance(data_forcada, datetime.date):
        return _data_para_rfc2822(data_forcada)
    for fonte in (url, html[:6000]):
        dt = extrair_data_de_texto(fonte)
        if dt:
            return _data_para_rfc2822(dt)
    return email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))


_RE_NIGHT = re.compile(r"(?<![a-z])night(?![a-z])", re.I)


def _eh_edicao_night(email_item):
    """Identifica se um item de 'the news' corresponde à edição noturna (NIGHT)."""
    if not email_item:
        return False
    ass = (email_item.get("assunto") or "").lower()
    rem = (email_item.get("remetente") or "").lower()
    url = (email_item.get("url") or "").lower()
    return (
        bool(_RE_NIGHT.search(ass))
        or bool(_RE_NIGHT.search(rem))
        or "🌖" in ass
        or "/p/night" in url
    )


def _chave_cronologica_email(e):
    """Ordena e-mails do grupo em ordem cronológica crescente (noite anterior antes da manhã de hoje)."""
    fuso_brt = datetime.timezone(datetime.timedelta(hours=-3))
    try:
        dt = email.utils.parsedate_to_datetime(e.get("data", ""))
        dt = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
        dt_brt = dt.astimezone(fuso_brt)
    except Exception:
        dt = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        dt_brt = dt
    eh_night = 1 if _eh_edicao_night(e) else 0
    return (dt_brt.date(), eh_night, dt)


def _inferir_prefixo_de_url(url, html, filtros_grupo, mapeamento):
    """Identifica o prefixo de `mapeamento` a partir de `--grupo`, do caminho da URL ou do HTML."""
    for filtro in filtros_grupo or []:
        f_low = filtro.lower().strip()
        if f_low in mapeamento:
            return f_low
        for k, v in mapeamento.items():
            if f_low == k.lower() or f_low == v.lower() or f_low in k.lower() or f_low in v.lower():
                return k

    url_low = url.lower()
    for slug, prefixo in _SLUG_URL_PARA_PREFIXO.items():
        if slug in url_low and prefixo in mapeamento:
            return prefixo

    html_low = html[:8000].lower()
    if "thenewscc.beehiiv.com" in url_low and "the news" in mapeamento:
        return "the news"
    if "the news sports" in html_low and "tns sports" in mapeamento:
        return "tns sports"
    if "aidrops" in url_low and "aidrops" in mapeamento:
        return "aidrops"
    if ("moneydrops" in url_low or "money-drops" in url_low) and "moneydrops" in mapeamento:
        return "moneydrops"
    if ("techdrops" in url_low or "tech-drops" in url_low) and "techdrops" in mapeamento:
        return "techdrops"
    if "thejobs.com.br" in url_low and "tns better work" in mapeamento:
        return "tns better work"
    if "readthepeak.com" in url_low and "peak" in mapeamento:
        return "peak"
    for k in sorted(mapeamento, key=len, reverse=True):
        if k.lower() in url_low or k.lower() in html_low:
            return k
    if "thenews.com.br" in url_low and "the news" in mapeamento:
        return "the news"
    return None


def _montar_item_de_html(url, html, filtros_grupo=None, mapeamento=None, data_forcada=None):
    mapeamento = mapeamento or config.MAPEAMENTO_PREFIXOS
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    titulo_pagina = (
        (h1.get_text(" ", strip=True) if h1 else "")
        or (soup.title.get_text(" ", strip=True) if soup.title else "")
        or url
    )
    prefixo = _inferir_prefixo_de_url(url, html, filtros_grupo, mapeamento)
    assunto = f"{prefixo}: {titulo_pagina}" if prefixo else titulo_pagina
    data_rfc = _inferir_data_de_url(url, soup.get_text(" ", strip=True)[:3000], data_forcada=data_forcada)
    return {
        "id": None,
        "assunto": assunto,
        "remetente": prefixo or url,
        "data": data_rfc,
        "conteudo_html": html,
        "eh_html": True,
        "url": url,
        "fonte_chave": prefixo or "",
    }


def _get_sem_cache(get, url, timeout=20):
    """Executa GET tentando contornar cache de CDN quando usando `requests.get` real."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    alvo = url
    if get is requests.get:
        sep = "&" if "?" in url else "?"
        alvo = f"{url}{sep}_cb={int(time.time())}"
    return get(alvo, headers=headers, timeout=timeout)


def buscar_email_de_url(url, filtros_grupo=None, mapeamento=None, get=requests.get, data_forcada=None):
    """Baixa uma edição online via URL e monta um dicionário compatível com `buscar_emails`."""
    logger.info(f"[cyan]➜ Baixando edição a partir da URL: {escape(url)}[/cyan]")
    resposta = _get_sem_cache(get, url, timeout=30)
    resposta.raise_for_status()
    return _montar_item_de_html(url, resposta.text, filtros_grupo, mapeamento, data_forcada=data_forcada)


_LOCK_CONTROLE_FONTES = threading.Lock()


def carregar_controle_fontes(caminho=None):
    """Lê `controle_fontes.json` com a última data e URLs já extraídas por fonte."""
    arq = Path(caminho) if caminho else config.CONTROLE_FONTES_PATH
    if not arq.exists():
        return {}
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def salvar_controle_fontes(dados, caminho=None):
    arq = Path(caminho) if caminho else config.CONTROLE_FONTES_PATH
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _extrair_data_item(email_item):
    """Extrai a data calendário (BRT) de um item de e-mail ou página web."""
    if not email_item:
        return None
    bruto = email_item.get("data", "")
    if bruto:
        try:
            dt = email.utils.parsedate_to_datetime(bruto)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(_FUSO_BRT).date()
        except Exception:
            pass
    return extrair_data_de_texto(email_item.get("assunto", ""))


def _extrair_data_html_beehiiv(html_post, url_post=""):
    """Extrai a data de publicação (BRT) de um post Beehiiv priorizando `article:published_time` / `<time>`."""
    soup_post = BeautifulSoup(html_post, "html.parser")
    meta_pub = soup_post.find("meta", {"property": "article:published_time"})
    tag_time = soup_post.find("time")
    raw_iso = (
        (meta_pub.get("content") if meta_pub else "")
        or (tag_time.get("datetime") if tag_time else "")
        or ""
    ).strip()
    if raw_iso:
        try:
            dt_iso = datetime.datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
            dt_iso = dt_iso if dt_iso.tzinfo else dt_iso.replace(tzinfo=datetime.timezone.utc)
            return dt_iso.astimezone(_FUSO_BRT).date()
        except Exception:
            dt_fallback = extrair_data_de_texto(raw_iso)
            if dt_fallback:
                return dt_fallback
    texto_topo = soup_post.get_text(" ", strip=True)[:1500]
    return extrair_data_de_texto(texto_topo) or extrair_data_de_texto(url_post)


def registrar_fontes_processadas(emails_ok, chave_padrao="", caminho=None):
    """Atualiza `controle_fontes.json` com a última data e URLs extraídas de cada fonte processada."""
    if not emails_ok:
        return
    with _LOCK_CONTROLE_FONTES:
        dados = carregar_controle_fontes(caminho)
        alterou = False
        for item in emails_ok:
            chave = (item.get("fonte_chave") or chave_padrao or "").lower().strip()
            if not chave:
                continue
            reg = dados.setdefault(chave, {"ultima_data": "", "urls": []})
            dt = _extrair_data_item(item)
            if dt:
                iso = dt.isoformat()
                if not reg.get("ultima_data") or iso > reg["ultima_data"]:
                    reg["ultima_data"] = iso
                    alterou = True
            url = (item.get("url") or "").strip()
            if url:
                urls = list(reg.get("urls") or [])
                if url not in urls:
                    urls.append(url)
                    reg["urls"] = urls[-25:]
                    alterou = True
        if alterou:
            salvar_controle_fontes(dados, caminho)


def montar_contexto_edicao_anterior(pasta, titulo_base, data_referencia, janela_dias=2):
    """Resume os títulos e inícios das matérias da última edição recente de `titulo_base` (evita repetição na sexta)."""
    if not pasta or not titulo_base or not data_referencia:
        return ""
    arquivos = []
    for arq in Path(pasta).glob(f"{glob_escape(titulo_base)} #*.md"):
        m = re.search(r"#(\d+)\.md$", arq.name)
        if m:
            arquivos.append((int(m.group(1)), arq))
    arquivos.sort(key=lambda par: par[0], reverse=True)

    ignorar_titulos = ("enquete do dia", "daily poll", "voz dos ouvintes", "listener feedback", "hoje eu aprendi", "pensei no chuveiro")
    blocos_edicoes = []
    for _, arq in arquivos[:5]:
        try:
            conteudo = arq.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        primeiras = "\n".join(conteudo.splitlines()[:5])
        dt_arq = extrair_data_de_texto(primeiras)
        if not dt_arq:
            continue
        delta = (data_referencia - dt_arq).days
        if delta <= 0 or delta > janela_dias:
            continue
        itens = []
        secoes = re.split(r"(?m)^##\s+", conteudo)[1:]
        for sec in secoes:
            linhas = [l.strip() for l in sec.splitlines() if l.strip()]
            if not linhas:
                continue
            titulo_sec = linhas[0].strip()
            if any(titulo_sec.lower().startswith(ig) for ig in ignorar_titulos):
                continue
            corpo_sec = " ".join(l for l in linhas[1:] if not l.startswith("#"))[:220]
            itens.append(f"- {titulo_sec}: {corpo_sec}" if corpo_sec else f"- {titulo_sec}")
        if itens:
            blocos_edicoes.append(
                f"Edição anterior: {arq.stem} ({dt_arq:%d/%m/%Y})\n" + "\n".join(itens[:12])
            )
            break
    return "\n\n".join(blocos_edicoes)


def _verificar_fonte_thenews(chave, titulo_base, arquivo_url, ultima_data, get=requests.get, datas_necessarias=None):
    """Verifica na página de arquivo do the news (`thenews.com.br/newsletter/<marca>`) se há edição mais recente."""
    datas_necessarias = set(datas_necessarias or ())
    slug_marca = arquivo_url.rstrip("/").split("/")[-1]
    prefixo_href = f"/newsletter/{slug_marca}/"
    resp = _get_sem_cache(get, arquivo_url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidatos = []
    vistos = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(prefixo_href) or "?" in href or href in vistos:
            continue
        vistos.add(href)
        texto_card = a.get_text(" ", strip=True)
        dt = extrair_data_de_texto(texto_card) or extrair_data_de_texto(href)
        if not dt:
            continue
        eh_necessaria = dt in datas_necessarias
        if not eh_necessaria:
            if ultima_data and dt <= ultima_data:
                continue
            if not ultima_data and (datetime.date.today() - dt).days > 2:
                continue
        url_edicao = urllib.parse.urljoin(arquivo_url, href)
        candidatos.append((dt, url_edicao, None))
        if len(candidatos) >= 3:
            break

    # Fallback direto pela URL canônica (/DD-MM-AAAA) caso o cache de CDN da vitrine ainda não tenha atualizado
    hoje = datetime.date.today()
    datas_encontradas = {dt for dt, _, _ in candidatos}
    probes = sorted(set((hoje - datetime.timedelta(days=1), hoje)) | datas_necessarias)
    for dt_probe in probes:
        if dt_probe in datas_encontradas:
            continue
        if dt_probe not in datas_necessarias and ultima_data and dt_probe <= ultima_data:
            continue
        url_direta = f"{arquivo_url.rstrip('/')}/{dt_probe:%d-%m-%Y}"
        try:
            resp_d = _get_sem_cache(get, url_direta, timeout=15)
            if getattr(resp_d, "status_code", None) != 200:
                continue
            soup_d = BeautifulSoup(resp_d.text, "html.parser")
            if not soup_d.find("h1"):
                continue
            dt_pag = extrair_data_de_texto(soup_d.get_text(" ", strip=True)[:3000])
            if dt_pag == dt_probe:
                candidatos.append((dt_probe, url_direta, resp_d.text))
        except Exception:
            continue

    candidatos.sort(key=lambda trio: trio[0])
    itens = []
    for dt, url_edicao, html_direto in candidatos:
        logger.info(
            f"[bold cyan]🌐 [Web] Edição de {dt:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
            f"(última local: {ultima_data.strftime('%d/%m/%Y') if ultima_data else 'nenhuma'}). Baixando...[/bold cyan]"
        )
        if html_direto is not None:
            itens.append(_montar_item_de_html(url_edicao, html_direto, filtros_grupo=[chave], data_forcada=dt))
        else:
            itens.append(buscar_email_de_url(url_edicao, filtros_grupo=[chave], get=get, data_forcada=dt))
    return itens


_STOPWORDS_RESUMO = {
    "sobre", "contra", "entre", "depois", "antes", "ainda", "mesmo", "todos", "todas",
    "mundo", "brasil", "feira", "quando", "porque", "estão", "foram", "sendo", "pode",
    "podem", "fazer", "feita", "feito", "disse", "afirma", "segundo", "após", "durante",
    "melhor", "maior", "menos", "muito", "muita", "outros", "outras", "novo", "nova",
    "novos", "novas", "principais", "notícias", "desta", "quarta", "quinta", "sexta",
    "terça", "segunda", "sábado", "domingo", "comprar", "vender", "começa", "parte",
    "quase",
}


def _texto_edicoes_da_data(pasta, titulo_base, data_alvo):
    """Lê o texto em minúsculas dos últimos `.md` de `titulo_base` referentes a `data_alvo`."""
    if not pasta:
        return ""
    arquivos = []
    for arq in Path(pasta).glob(f"{glob_escape(titulo_base)} #*.md"):
        m = re.search(r"#(\d+)\.md$", arq.name)
        if m:
            arquivos.append((int(m.group(1)), arq))
    arquivos.sort(key=lambda par: par[0], reverse=True)
    textos = []
    for _, arq in arquivos[:4]:
        try:
            conteudo = arq.read_text(encoding="utf-8", errors="ignore")
            primeiras = "\n".join(conteudo.splitlines()[:5])
            if extrair_data_de_texto(primeiras) == data_alvo:
                textos.append(conteudo.lower())
        except Exception:
            continue
    if textos:
        return "\n".join(textos)
    if arquivos:
        try:
            return arquivos[0][1].read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            return ""
    return ""


def _palavras_relevantes(texto):
    """Extrai palavras únicas >= 5 letras (fora das stopwords) em minúsculas."""
    brutos = re.findall(r"[a-záéíóúâêôãõçA-ZÁÉÍÓÚÂÊÔÃÕÇ]{5,}", texto or "")
    unicos = []
    for termo in brutos:
        low = termo.lower()
        if low not in _STOPWORDS_RESUMO and low not in unicos:
            unicos.append(low)
    return unicos


def _post_ja_extraido_localmente(html_post, texto_local):
    """Confere por parágrafos editoriais do corpo (ou chamada 'Para hoje…' em HTMLs curtos) se o post já está no `.md`."""
    if not texto_local:
        return False
    texto_low = texto_local.lower()
    soup = BeautifulSoup(html_post, "html.parser")

    paragrafos_corpo = []
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if len(txt) < 70:
            continue
        if re.search(r"^(?:Para hoje|the news\b|Tempo de leitura|Veja mais|👉|🔋)", txt, re.I):
            continue
        paragrafos_corpo.append(txt)

    if len(paragrafos_corpo) >= 3:
        blocos_locais = [b for b in re.split(r"\n{2,}", texto_low) if len(b.strip()) >= 40]
        avaliados = 0
        encontrados = 0
        for p_txt in paragrafos_corpo[:10]:
            palavras = _palavras_relevantes(p_txt)
            if len(palavras) < 4:
                continue
            avaliados += 1
            if any(sum(1 for w in palavras if w in bloco) / len(palavras) >= 0.65 for bloco in blocos_locais):
                encontrados += 1
        if avaliados:
            return encontrados >= 2 and (encontrados / avaliados) >= 0.35

    para_hoje = next(
        (p.get_text(" ", strip=True) for p in soup.find_all(["p", "div"]) if "Para hoje" in p.get_text() and len(p.get_text()) < 500),
        "",
    )
    if para_hoje:
        miolo = re.sub(r"^.*?Para hoje[….:\s]+", "", para_hoje)
        miolo = re.sub(r"(?:Segunda|Terça|Quarta|Quinta|Sexta|Sábado|Domingo)[^;.]*$", "", miolo, flags=re.I)
        unicos = _palavras_relevantes(miolo)
    else:
        cabecalhos = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h3", "h5"]) if len(h.get_text(strip=True)) > 15]
        unicos = _palavras_relevantes(" ".join(cabecalhos[:5]))

    if not unicos:
        return False
    hits = sum(1 for termo in unicos if termo in texto_low)
    return (hits / len(unicos)) >= 0.6


def _agrupar_cards_beehiiv(soup, arquivo_url):
    """Agrupa links `/p/` por URL canônica juntando os textos de âncoras duplicadas (imagem + título)."""
    cards = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/p/" not in href or "?" in href:
            continue
        url_post = urllib.parse.urljoin(arquivo_url, href)
        texto = a.get_text(" ", strip=True)
        prev = cards.get(url_post, "")
        if texto and texto not in prev:
            cards[url_post] = f"{prev} {texto}".strip() if prev else texto
        elif url_post not in cards:
            cards[url_post] = ""
    return list(cards.items())


def _verificar_fonte_beehiiv(
    chave,
    titulo_base,
    arquivo_url,
    ultima_data,
    get=requests.get,
    pasta_resultados=None,
    emails_atuais_grupo=None,
    urls_processadas=None,
    datas_necessarias=None,
    permitir_mesma_data_nao_extraida=False,
):
    """Verifica arquivo Beehiiv (`thenewscc.beehiiv.com/archive`, `aidrops/moneydrops/techdrops.com.br/archive`, `thejobs.com.br/archive`)."""
    urls_processadas = set(urls_processadas or ())
    datas_necessarias = set(datas_necessarias or ())
    min_necessaria = min(datas_necessarias) if datas_necessarias else None
    resp = _get_sem_cache(get, arquivo_url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    cards = _agrupar_cards_beehiiv(soup, arquivo_url)
    if not cards:
        return []

    tem_night_gmail = any(_eh_edicao_night(e) for e in (emails_atuais_grupo or []))
    tem_manha_gmail = any(not _eh_edicao_night(e) for e in (emails_atuais_grupo or []))

    itens = []
    for idx, (url_post, texto_card) in enumerate(cards[:4]):
        if url_post in urls_processadas:
            continue
        eh_night = "/p/night" in url_post.lower() or "night #" in texto_card.lower()
        if emails_atuais_grupo and ((eh_night and tem_night_gmail) or (not eh_night and tem_manha_gmail)):
            continue

        dt_card = extrair_data_de_texto(texto_card) or extrair_data_de_texto(url_post)
        if dt_card is not None:
            eh_necessaria = dt_card in datas_necessarias
            if not eh_necessaria:
                if ultima_data:
                    if dt_card < ultima_data:
                        continue
                    if dt_card == ultima_data:
                        if not (eh_night or permitir_mesma_data_nao_extraida):
                            continue
                        resp_post = get(url_post, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
                        resp_post.raise_for_status()
                        texto_local = _texto_edicoes_da_data(pasta_resultados, titulo_base, dt_card) if pasta_resultados else ""
                        if _post_ja_extraido_localmente(resp_post.text, texto_local):
                            continue
                        rotulo_ed = "Edição noturna" if eh_night else f"Edição de '{chave}'"
                        logger.info(
                            f"[bold cyan]🌐 [Web] {rotulo_ed} de {dt_card:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
                            f"(ausente no último arquivo local). Baixando...[/bold cyan]"
                        )
                        itens.append(_montar_item_de_html(url_post, resp_post.text, filtros_grupo=[chave], data_forcada=dt_card))
                        continue
                elif (datetime.date.today() - dt_card).days > 2:
                    continue

            logger.info(
                f"[bold cyan]🌐 [Web] Edição de {dt_card:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
                f"(fonte: {escape(chave)} · última local: {ultima_data.strftime('%d/%m/%Y') if ultima_data else 'nenhuma'}). Baixando...[/bold cyan]"
            )
            itens.append(buscar_email_de_url(url_post, filtros_grupo=[chave], get=get, data_forcada=dt_card))
            continue

        # Quando o card da vitrine não exibe a data (ex.: AiDrops, MoneyDrops, TechDrops), inspeciona os 3 posts mais recentes
        if idx < 3:
            resp_post = get(url_post, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            resp_post.raise_for_status()
            dt_post = _extrair_data_html_beehiiv(resp_post.text, url_post)
            if not dt_post:
                continue
            eh_necessaria = dt_post in datas_necessarias
            if eh_necessaria:
                texto_local = _texto_edicoes_da_data(pasta_resultados, titulo_base, dt_post) if pasta_resultados else ""
                if _post_ja_extraido_localmente(resp_post.text, texto_local):
                    continue
            elif ultima_data:
                if dt_post < ultima_data:
                    if not min_necessaria or dt_post < min_necessaria:
                        break
                    continue
                if dt_post == ultima_data:
                    if not permitir_mesma_data_nao_extraida:
                        continue
                    texto_local = _texto_edicoes_da_data(pasta_resultados, titulo_base, dt_post) if pasta_resultados else ""
                    if _post_ja_extraido_localmente(resp_post.text, texto_local):
                        continue
            elif (datetime.date.today() - dt_post).days > 2:
                break
            logger.info(
                f"[bold cyan]🌐 [Web] Edição de {dt_post:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
                f"(fonte: {escape(chave)} · última local: {ultima_data.strftime('%d/%m/%Y') if ultima_data else 'nenhuma'}). Baixando...[/bold cyan]"
            )
            itens.append(_montar_item_de_html(url_post, resp_post.text, filtros_grupo=[chave], data_forcada=dt_post))
    return itens


def _verificar_fonte_thepeak(chave, titulo_base, arquivo_url, ultima_data, get=requests.get):
    """Verifica `readthepeak.com/newsletters/morning-brief` e reúne os artigos públicos da edição se faltar."""
    resp = _get_sem_cache(get, arquivo_url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    link = next(
        (a for a in soup.find_all("a", href=True) if "/newsletters/morning-brief/" in a["href"] and a.get_text(strip=True)),
        None,
    )
    if not link:
        return []
    url_edicao = urllib.parse.urljoin(arquivo_url, link["href"].strip())
    resp_ed = get(url_edicao, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp_ed.raise_for_status()
    soup_ed = BeautifulSoup(resp_ed.text, "html.parser")
    tag_time = soup_ed.find("time")
    texto_data = (tag_time.get("datetime") or tag_time.get_text(" ", strip=True)) if tag_time else soup_ed.get_text(" ", strip=True)[:1500]
    dt_ed = extrair_data_de_texto(texto_data)
    if not dt_ed:
        return []
    if ultima_data and dt_ed <= ultima_data:
        return []
    if not ultima_data and (datetime.date.today() - dt_ed).days > 2:
        return []

    logger.info(
        f"[bold cyan]🌐 [Web] Edição de {dt_ed:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
        f"(última local: {ultima_data.strftime('%d/%m/%Y') if ultima_data else 'nenhuma'}). Baixando...[/bold cyan]"
    )
    # Se a página da edição exigir login, compõe o HTML a partir dos artigos públicos do dia no sitemap
    url_sitemap = f"https://readthepeak.com/sitemaps/articles-{dt_ed:%Y-%m}.xml"
    artigos_html = []
    try:
        resp_sm = get(url_sitemap, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if resp_sm.status_code == 200:
            ontem_iso = (dt_ed - datetime.timedelta(days=1)).isoformat()
            hoje_iso = dt_ed.isoformat()
            for bloco in re.findall(r"<url>.*?</url>", resp_sm.text, flags=re.S):
                m_loc = re.search(r"<loc>(https://readthepeak\.com/articles/[^<]+)</loc>", bloco)
                m_mod = re.search(r"<lastmod>([^<]+)</lastmod>", bloco)
                if not (m_loc and m_mod):
                    continue
                mod = m_mod.group(1)
                if mod.startswith(hoje_iso) or (mod.startswith(ontem_iso) and mod[11:13] >= "14"):
                    r_art = get(m_loc.group(1), headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                    if r_art.status_code == 200:
                        s_art = BeautifulSoup(r_art.text, "html.parser")
                        art_tag = s_art.find("article") or s_art.find("main")
                        if art_tag:
                            artigos_html.append(str(art_tag))
    except Exception as e:
        logger.warning(f"[yellow]⚠ Falha ao consultar sitemap do The Peak: {escape(str(e))}[/yellow]")

    if artigos_html:
        h1_txt = soup_ed.h1.get_text(" ", strip=True) if soup_ed.h1 else "Morning Brief"
        html_combinado = f"<html><body><h1>{h1_txt}</h1>\n" + "\n<hr/>\n".join(artigos_html) + "</body></html>"
        return [_montar_item_de_html(url_edicao, html_combinado, filtros_grupo=[chave], data_forcada=dt_ed)]
    return [_montar_item_de_html(url_edicao, resp_ed.text, filtros_grupo=[chave], data_forcada=dt_ed)]


def _datas_par_necessarias(chave, itens_coletados, mapeamento, pares_fontes):
    """Calcula datas esperadas de `chave` necessárias para parear com itens já coletados da fonte parceira."""
    necessarias = set()
    if not pares_fontes or not itens_coletados:
        return necessarias
    grupos_tmp = agrupar_emails(itens_coletados, mapeamento)
    chave_low = chave.lower()
    for par in pares_fontes:
        for regra in par.get("regras_pareamento", []):
            if (regra.get("aguarda") or "").lower() != chave_low:
                continue
            origem = (regra.get("fonte") or "").lower()
            dias = set(regra.get("dias") or ())
            delta = int(regra.get("delta_dias", 0))
            for item in grupos_tmp.get(origem, []):
                dt = _extrair_data_item(item)
                if dt and dt.weekday() in dias:
                    necessarias.add(dt + datetime.timedelta(days=delta))
    return necessarias


def buscar_edicoes_faltantes_na_web(
    emails_atuais,
    pasta_resultados=None,
    filtros_grupo=None,
    fontes_web=None,
    mapeamento=None,
    get=requests.get,
    caminho_controle_fontes=None,
    pares_fontes=None,
    caminho_controle_noticias=None,
    forcar_pares=False,
    agora=None,
):
    """Dupla verificação: para cada fonte externa sem e-mail não lido hoje (e feeds RSS nativos),
    checa nos sites/feeds oficiais se há edições ou notícias mais recentes que o controle local.
    """
    pasta_resultados = pasta_resultados or config.RESULTADOS_DIR
    fontes_web = fontes_web if fontes_web is not None else config.FONTES_WEB
    mapeamento = mapeamento or config.MAPEAMENTO_PREFIXOS
    pares_fontes = pares_fontes if pares_fontes is not None else config.PARES_FONTES
    if not fontes_web:
        return []

    if caminho_controle_fontes is None:
        if Path(pasta_resultados).resolve() == Path(config.RESULTADOS_DIR).resolve():
            caminho_controle_fontes = config.CONTROLE_FONTES_PATH
        else:
            caminho_controle_fontes = Path(pasta_resultados) / "controle_fontes.json"
    if caminho_controle_noticias is None:
        if Path(pasta_resultados).resolve() == Path(config.RESULTADOS_DIR).resolve():
            caminho_controle_noticias = config.CONTROLE_NOTICIAS_PATH
        else:
            caminho_controle_noticias = Path(pasta_resultados) / "controle_noticias.json"
    controle_fontes = carregar_controle_fontes(caminho_controle_fontes)

    agora_brt = agora or datetime.datetime.now(_FUSO_BRT)
    if agora_brt.tzinfo is not None:
        agora_brt = agora_brt.astimezone(_FUSO_BRT)
    else:
        agora_brt = agora_brt.replace(tzinfo=_FUSO_BRT)

    fontes_pareadas = {
        f.lower() for par in (pares_fontes or []) for f in (par.get("fontes") or [])
    }
    parceiros_por_fonte = {}
    for par in (pares_fontes or []):
        fs = [f.lower() for f in (par.get("fontes") or [])]
        for f in fs:
            parceiros_por_fonte[f] = fs

    grupos_map = agrupar_emails(emails_atuais, mapeamento)
    grupos_presentes = set(grupos_map.keys())
    titulos_presentes = {mapeamento.get(c, c) for c in grupos_presentes}
    logger.info("[cyan]🔎 [Web] Verificando nos sites e feeds oficiais se há edições ou notícias novas...[/cyan]")

    recuperados = []
    rss_aguardando_hora = []
    rss_ja_extraidos_hoje = []
    for chave, cfg_web in fontes_web.items():
        titulo_base = mapeamento.get(chave, chave)
        chave_low = chave.lower()
        forcado_filtro = False
        if filtros_grupo:
            termos_match = [chave_low, titulo_base.lower()] + parceiros_por_fonte.get(chave_low, [])
            for alias, dest in _ALIASES_PREFIXO.items():
                if dest in termos_match:
                    termos_match.append(alias)
            if not any(f.lower() in termo for f in filtros_grupo for termo in termos_match):
                continue
            forcado_filtro = True

        emails_grupo = grupos_map.get(chave, [])
        tipo = (cfg_web.get("tipo") or "").lower()
        eh_pareada = chave_low in fontes_pareadas
        esperado_dia = 2 if chave == "the news" else 1
        if tipo != "rss":
            if len(emails_grupo) >= esperado_dia:
                continue
            if not eh_pareada and esperado_dia == 1 and titulo_base in titulos_presentes:
                continue

        reg_fonte = controle_fontes.get(chave_low) or {}
        ultima_data_str = reg_fonte.get("ultima_data") or ""
        ultima_data = None
        if ultima_data_str:
            try:
                ultima_data = datetime.date.fromisoformat(ultima_data_str)
            except ValueError:
                ultima_data = None
        if ultima_data is None:
            ultima_data = ultima_data_extraida_local(pasta_resultados, titulo_base)

        if tipo == "rss":
            forcar_rss = forcar_pares or forcado_filtro
            hora_min = cfg_web.get("hora_minima") or ""
            if not forcar_rss and hora_min and not janela_horaria_liberada(hora_min, agora_brt):
                rss_aguardando_hora.append((titulo_base, hora_min))
                continue
            max_por_dia = int(cfg_web.get("max_por_dia", 0) or 0)
            if not forcar_rss and max_por_dia >= 1 and ultima_data == agora_brt.date():
                rss_ja_extraidos_hoje.append(titulo_base)
                continue

            chaves_vistas, _, _ = carregar_historico_noticias(
                chave_low,
                titulo_base,
                gmail_service=None,
                caminho_controle=caminho_controle_noticias,
                pasta_resultados=pasta_resultados,
                ultima_data=ultima_data,
            )
            for e_ant in emails_grupo:
                for it in extrair_noticias_email(e_ant):
                    chaves_vistas.add(it["chave"])
                    cu = normalizar_chave_url(it.get("url", ""))
                    if cu:
                        chaves_vistas.add(cu)
            try:
                novos = coletar_feed_rss(
                    chave,
                    titulo_base,
                    cfg_web,
                    chaves_vistas=chaves_vistas,
                    ultima_data=ultima_data,
                    get=get,
                    agora=agora_brt,
                )
            except Exception as e:
                logger.warning(f"[yellow]  ⚠ [RSS] Não foi possível verificar '{escape(titulo_base)}': {escape(str(e))}[/yellow]")
                continue
            if novos:
                recuperados.extend(novos)
            continue

        urls_processadas = set(reg_fonte.get("urls") or ())
        datas_necessarias = _datas_par_necessarias(chave_low, list(emails_atuais) + recuperados, mapeamento, pares_fontes)
        permitir_mesma_data = eh_pareada and not reg_fonte

        arquivo_url = cfg_web.get("arquivo_url") or ""
        if not arquivo_url:
            continue

        try:
            if tipo == "thenews":
                novos = _verificar_fonte_thenews(
                    chave, titulo_base, arquivo_url, ultima_data, get=get, datas_necessarias=datas_necessarias
                )
            elif tipo == "beehiiv":
                novos = _verificar_fonte_beehiiv(
                    chave,
                    titulo_base,
                    arquivo_url,
                    ultima_data,
                    get=get,
                    pasta_resultados=pasta_resultados,
                    emails_atuais_grupo=emails_grupo,
                    urls_processadas=urls_processadas,
                    datas_necessarias=datas_necessarias,
                    permitir_mesma_data_nao_extraida=permitir_mesma_data,
                )
            elif tipo == "thepeak":
                novos = _verificar_fonte_thepeak(chave, titulo_base, arquivo_url, ultima_data, get=get)
            else:
                novos = []
        except Exception as e:
            logger.warning(f"[yellow]  ⚠ [Web] Não foi possível verificar '{escape(titulo_base)}' ({escape(arquivo_url)}): {escape(str(e))}[/yellow]")
            continue

        if novos:
            recuperados.extend(novos)
            if not eh_pareada and len(emails_grupo) + len(novos) >= esperado_dia:
                titulos_presentes.add(titulo_base)

    if rss_aguardando_hora:
        nomes_h = ", ".join(t for t, _ in rss_aguardando_hora)
        hora_ref = rss_aguardando_hora[0][1]
        logger.info(
            f"[yellow]  ⏸ [RSS] Aguardando janela das {escape(hora_ref)}: {escape(nomes_h)} "
            f"(use --grupo ou --forcar-pares para antecipar)[/yellow]"
        )
    if rss_ja_extraidos_hoje:
        nomes_j = ", ".join(rss_ja_extraidos_hoje)
        logger.info(
            f"[dim]  ⏸ [RSS] Já extraído(s) hoje ({agora_brt:%d/%m}): {escape(nomes_j)} "
            f"— novas notícias acumulando para amanhã (use --grupo ou --forcar-pares para forçar)[/dim]"
        )

    if not recuperados:
        logger.info("[dim]  ✔ [Web] Todas as fontes externas estão em dia com os arquivos locais.[/dim]")
    return recuperados


_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")


def _normalizar(linhas):
    return " ".join(" ".join(linhas).split()).lower()


def _separar_rabo(linhas):
    """Separa as linhas finais em branco ou '---' (separadores entre edições)."""
    fim = len(linhas)
    while fim > 0 and linhas[fim - 1].strip() in ("", "---"):
        fim -= 1
    return linhas[:fim], linhas[fim:]


_RE_CREDITO_RESIDUAL = re.compile(
    r"^\[(?:descrição da imagem|imagem):\s*[^\n|/–—]{1,40}\s*(?:[|/–—]|\bvia\b)\s*"
    r"(?:reprodu[cç][aã]o|getty(?:\s*images)?|globo|uol|afp|reuters|estad[aã]o|folha|x|twitter|"
    r"unsplash|divulga[cç][aã]o|fortune|bloomberg|ap|shutterstock|vatican|axios|metr[oó]poles|ge)\s*\]\s*$",
    re.I,
)
_RE_LINHA_CREDITO_SOLTO = re.compile(
    r"^(?:source|photo|image|credit|illustration|artwork(?:\s+by)?|foto|cr[eé]dito|imagem)\s*:\s*[^\n]{1,80}$",
    re.I,
)
_RE_APROFUNDE = re.compile(
    r"\s*(?:\([*_]{0,3}(?:aprofund[ea]|leia mais|saiba mais)[*_]{0,3}\)|"
    r"\b(?:aprofund[ea]|leia mais|saiba mais)\b(?=\s*(?:[.!?]\s*)?(?:\n|$)))",
    re.I,
)
_RE_TWITTER_LIXO = re.compile(r"(?:https?://t\.co/\S+|pic\.twitter\.com/\S+)")
_RE_TITULO_PONTUADO = re.compile(r"^(#{1,6}\s+.*?)[.?!:]\s*$")
_RE_CERCA_EXTERNA = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n\s*```\s*$", re.S | re.I)
_RE_MOEDA_DUPLICADA = re.compile(r"(?<!\w)(US\$|R\$|€|£)\s+\1(?=\s|\d|$)")
_RE_NAO_PEGADINHA_RISOS = re.compile(r"^\s*\*?N[aã]o\s+[eé]\s+pegadinha,?\s*risos\.?\*?[ \t]*", re.I | re.M)
_RE_RISOS_CACOETE = re.compile(r"(?:\s*\(\s*risos\.?\s*\)|(?:,\s*|\.\s+)risos\.?(?=\s|[\)*_.,;!?]|$))", re.I)
_RE_SUBTITULO_ESTAGI = re.compile(r"^(#{2,4}\s+)Estagi(?:\b[,!?:]?\s+.*)$", re.I | re.M)
_RE_CONECTOR_ORFAO_SECAO = re.compile(r"(^#{2,3}\s+[^\n]+\n+)Pelo\s+contr[aá]rio,\s+([^\s])", re.M)
_RE_TEXTO_MOSTRA = re.compile(r"\b(O|o)\s+texto\s+mostra\s+como\b")


def _norm_paragrafo(texto):
    limpo = re.sub(r"[*_`\"'“”‘’]", "", texto or "")
    return " ".join(limpo.lower().split())


def _tokens_paragrafo(norm_txt):
    return {w for w in re.findall(r"[a-záéíóúâêôãõç0-9]{4,}", norm_txt)}


_RE_RODAPE_SEO_CURTO = re.compile(
    r"\b(?:está disponível para|disponível para ps5|encerrou um hiato de|"
    r"tem lançamento marcado para|chega aos cinemas em|estreia nos cinemas em)\b",
    re.I,
)


def _eh_rodape_seo_curto(norm_txt):
    return 28 <= len(norm_txt) < 160 and bool(_RE_RODAPE_SEO_CURTO.search(norm_txt))


def _paragrafo_ja_visto(nb, tb, vistos):
    eh_seo = _eh_rodape_seo_curto(nb)
    for v_norm, v_tok in vistos:
        if nb == v_norm:
            return True
        if eh_seo and _eh_rodape_seo_curto(v_norm):
            if SequenceMatcher(None, nb, v_norm).ratio() >= 0.78:
                return True
        if len(nb) >= 90 and len(v_norm) >= 90:
            if SequenceMatcher(None, nb, v_norm).ratio() >= 0.72:
                return True
            if len(tb) >= 10 and len(v_tok) >= 10:
                if len(tb & v_tok) / min(len(tb), len(v_tok)) >= 0.75:
                    return True
    return False


def _remover_paragrafos_redundantes(texto):
    """Remove parágrafos de ficha técnica/sinopse repetidos entre seções `##` distintas e descarta
    seções `##` subsequentes cujo corpo (>65% dos parágrafos, min 3) já foi coberto em seção anterior.
    """
    secoes = [{"titulo": None, "linhas": []}]
    for linha in texto.split("\n"):
        if linha.startswith("## "):
            secoes.append({"titulo": linha[3:].strip(), "linhas": [linha]})
        else:
            secoes[-1]["linhas"].append(linha)

    vistos = []
    mantidas = []
    for s in secoes:
        tit = (s["titulo"] or "").lower()
        if (
            not s["titulo"]
            or tit.startswith("resultados")
            or "enquete" in tit
            or "daily poll" in tit
            or "voz dos" in tit
            or "listener" in tit
        ):
            mantidas.append("\n".join(s["linhas"]).strip())
            continue

        miolo, rabo = _separar_rabo(s["linhas"][1:])
        corpo_txt = "\n".join(miolo).strip()
        blocos = [b.strip() for b in re.split(r"\n{2,}", corpo_txt) if b.strip()]
        novos_blocos = []
        prosa_total = 0
        prosa_dup = 0
        for b in blocos:
            eh_prosa = not b.startswith("#") and not re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", b) and b != "---"
            nb = _norm_paragrafo(b)
            tb = _tokens_paragrafo(nb)
            rastreavel = len(nb) >= 55 or _eh_rodape_seo_curto(nb)
            if eh_prosa and rastreavel:
                if len(nb) >= 55:
                    prosa_total += 1
                if _paragrafo_ja_visto(nb, tb, vistos):
                    if len(nb) >= 55:
                        prosa_dup += 1
                    continue
            novos_blocos.append(b)

        if prosa_total >= 3 and prosa_dup / prosa_total >= 0.65:
            if rabo:
                mantidas.append("\n".join(rabo).strip())
            continue
        if not novos_blocos and blocos:
            novos_blocos = [blocos[0]]

        for b in novos_blocos:
            nb = _norm_paragrafo(b)
            if len(nb) >= 55 or _eh_rodape_seo_curto(nb):
                vistos.append((nb, _tokens_paragrafo(nb)))

        partes_sec = [s["linhas"][0]]
        if novos_blocos:
            partes_sec.append("\n\n".join(novos_blocos))
        if rabo:
            partes_sec.append("\n".join(rabo).strip())
        mantidas.append("\n\n".join(p for p in partes_sec if p))

    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(p for p in mantidas if p)).strip()


_STOPWORDS_TOPICO = _STOPWORDS_RESUMO | {
    "nesta", "nesse", "nisso", "disso", "deste", "agencia", "agência", "afirmou",
    "revelou", "revela", "apontou", "aponta", "mostrou", "mostra", "confirma", "confirmou",
    "explica", "explicou", "ganha", "ganhou", "comenta", "comentou", "garante", "garantiu",
    "defende", "defendeu", "critica", "criticou", "anunciou", "lançou", "lancou",
    "promete", "prometeu", "falou", "citou", "pediu", "passa", "segue", "entra",
    "tenta", "relembra", "empresa", "empresas", "governo", "estados", "unidos",
    "americana", "americano", "bilhões", "bilhoes", "milhões", "milhoes",
    "anuncia", "lança", "lanca", "chega", "volta", "primeira", "primeiro",
    "segunda", "segundo", "terceira", "terceiro", "grande", "grandes",
    "poderia", "deveria", "terão", "terao", "foram", "ações", "acoes", "plano",
    "grupo", "setor", "ontem", "agora", "filme", "filmes", "série", "serie",
    "séries", "series", "jogos", "elenco", "personagem", "personagens", "diretor",
    "diretora", "atriz", "estúdio", "estudio", "temporada", "lançamento", "lancamento",
    "estreia", "história", "historia", "detalhes", "versão", "versao", "franquia",
    "cinema", "cinemas", "público", "publico", "texto", "vídeo", "video", "trailer",
    "imagem", "imagens", "partes", "melhores",
}

_SECOES_GIRO = (
    "need to know", "giro de notícias", "giro de noticias", "um giro pelas notícias",
    "um giro pelas noticias", "giro pelas notícias", "tour de manchetes", "tour de headlines",
    "headlines", "radar", "radar headlines", "quick takes", "quick picks",
    "destaques rápidos", "destaques rapidos",
)

_PREFIXOS_ESPECIAIS_NAO_FUNDIR = (
    "resultados", "fechamento de mercado", "markets", "agenda", "voz dos",
    "listener", "enquete", "daily poll", "aspas do dia", "quote of the day",
    "curiosidade", "game do", "trivia", "drop the pin", "around the web",
    "confira a lista", "panorama",
)


def _palavras_topico(texto):
    """Extrai palavras relevantes de tópico (>= 5 letras, excluindo stopwords genéricas)."""
    return {
        w.lower()
        for w in re.findall(r"[a-záéíóúâêôãõçA-ZÁÉÍÓÚÂÊÔÃÕÇ]{5,}", texto or "")
        if w.lower() not in _STOPWORDS_TOPICO
    }


def _eh_secao_giro(titulo):
    tit = (titulo or "").strip().lower()
    return any(tit == g or tit.startswith(g) for g in _SECOES_GIRO)


def _eh_secao_especial_nao_fundir(titulo):
    tit = (titulo or "").strip().lower()
    if not tit:
        return True
    return _eh_secao_giro(tit) or any(tit.startswith(p) for p in _PREFIXOS_ESPECIAIS_NAO_FUNDIR)


def _dividir_h2(texto):
    secoes = [{"titulo": None, "linhas": []}]
    for linha in texto.split("\n"):
        if linha.startswith("## "):
            secoes.append({"titulo": linha[3:].strip(), "linhas": [linha]})
        else:
            secoes[-1]["linhas"].append(linha)
    return secoes


def _absorver_bullets_redundantes(secoes):
    """Remove bullets curtos em seções de giro já cobertos integralmente em outra seção do documento."""
    textos_alvo = []
    for idx, s in enumerate(secoes):
        tit = (s["titulo"] or "").strip().lower()
        if not tit or any(tit.startswith(p) for p in ("resultados", "fechamento de mercado", "markets", "agenda", "voz dos", "listener", "enquete", "daily poll")):
            continue
        corpo = "\n".join(s["linhas"])
        textos_alvo.append((idx, corpo.lower(), set(_numeros(corpo))))

    absorvidos = []
    nova_lista = []
    for i, s in enumerate(secoes):
        if not s["titulo"] or not _eh_secao_giro(s["titulo"]):
            nova_lista.append(s)
            continue

        novas_linhas = [s["linhas"][0]]
        bullets_restantes = 0
        for linha in s["linhas"][1:]:
            if not re.match(r"^\s*[-*•]\s+", linha):
                novas_linhas.append(linha)
                continue
            b_limpo = re.sub(r"^\s*[-*•]\s*(?:\*\*→\*\*\s*|→\s*)?", "", linha).strip()
            if not b_limpo or len(b_limpo) > 240:
                novas_linhas.append(linha)
                bullets_restantes += 1
                continue

            pal_b = _palavras_topico(b_limpo)
            nums_b = set(_numeros(b_limpo))
            eh_redundante = False
            if len(pal_b) >= 3:
                for j, txt_j_low, nums_j in textos_alvo:
                    if j == i:
                        continue
                    if nums_b and (nums_b - nums_j):
                        continue
                    hits = [w for w in pal_b if w in txt_j_low]
                    prop = len(hits) / len(pal_b)
                    tem_proprio = bool(re.search(r"(?<!^)\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]{3,}", b_limpo))
                    if (len(hits) >= 3 and prop >= 0.60) or (len(hits) >= 2 and len(pal_b) == 3 and prop >= 0.66 and tem_proprio):
                        eh_redundante = True
                        break
            if eh_redundante:
                absorvidos.append(b_limpo[:60])
                continue
            novas_linhas.append(linha)
            bullets_restantes += 1

        tem_conteudo = any(
            l.strip() and l.strip() != "---" and not l.strip().startswith("#")
            for l in novas_linhas[1:]
        )
        if bullets_restantes > 0 or tem_conteudo:
            nova_lista.append({"titulo": s["titulo"], "linhas": novas_linhas})
    return nova_lista, absorvidos


def consolidar_secoes_mesmo_assunto_detalhado(texto):
    """Concatena seções `##` do mesmo assunto sob `###` e absorve bullets redundantes sem perder números.

    Retorna `(texto_consolidado, {"secoes_concatenadas": [...], "bullets_absorvidos": [...]})`.
    """
    info = {"secoes_concatenadas": [], "bullets_absorvidos": []}
    if not texto or not texto.strip():
        return texto, info

    nums_antes = set(_numeros(texto))
    secoes, absorvidos = _absorver_bullets_redundantes(_dividir_h2(texto))
    info["bullets_absorvidos"] = absorvidos

    consolidadas = []
    meta_norm = []
    for s in secoes:
        tit = s["titulo"]
        if _eh_secao_especial_nao_fundir(tit):
            consolidadas.append(s)
            meta_norm.append(None)
            continue

        t_nov = _palavras_topico(tit)
        miolo_nov, rabo_nov = _separar_rabo(s["linhas"][1:])
        corpo_nov_txt = "\n".join(miolo_nov).strip()
        c_nov = _palavras_topico(corpo_nov_txt)

        idx_alvo = None
        melhor_score = None
        if corpo_nov_txt:
            for k, meta in enumerate(meta_norm):
                if meta is None:
                    continue
                t_ant, c_ant = meta
                comum_tit = t_ant & t_nov
                if not comum_tit:
                    continue
                comum_total = (t_ant | c_ant) & (t_nov | c_nov)
                comum_corpo = c_ant & c_nov
                ratio_corpo = len(comum_corpo) / max(1, min(len(c_ant), len(c_nov)))
                if (len(comum_tit) >= 2 and len(comum_total) >= 5) or (
                    len(comum_tit) >= 1 and len(comum_corpo) >= 6 and ratio_corpo >= 0.45
                ):
                    score = (len(comum_corpo), len(comum_total), k)
                    if melhor_score is None or score > melhor_score:
                        melhor_score = score
                        idx_alvo = k

        if idx_alvo is None:
            consolidadas.append(s)
            meta_norm.append((t_nov, c_nov))
            continue

        alvo = consolidadas[idx_alvo]
        conteudo_alvo, rabo_alvo = _separar_rabo(alvo["linhas"])
        vistos_alvo = [
            (_norm_paragrafo(b), _tokens_paragrafo(_norm_paragrafo(b)))
            for b in re.split(r"\n{2,}", "\n".join(conteudo_alvo[1:]))
            if len(_norm_paragrafo(b)) >= 55
        ]
        blocos_novos = []
        for b in re.split(r"\n{2,}", corpo_nov_txt):
            b_str = b.strip()
            if not b_str:
                continue
            eh_prosa = not b_str.startswith("#") and not re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", b_str) and b_str != "---"
            nb = _norm_paragrafo(b_str)
            if eh_prosa and len(nb) >= 55 and _paragrafo_ja_visto(nb, _tokens_paragrafo(nb), vistos_alvo):
                continue
            blocos_novos.append(b_str)

        if blocos_novos:
            extra_linhas = [f"### {tit}", ""] + "\n\n".join(blocos_novos).split("\n")
            alvo["linhas"] = conteudo_alvo + [""] + extra_linhas + (rabo_nov or rabo_alvo)
            t_ant, c_ant = meta_norm[idx_alvo]
            meta_norm[idx_alvo] = (t_ant | t_nov, c_ant | c_nov)
        elif rabo_nov and not rabo_alvo:
            alvo["linhas"] = conteudo_alvo + rabo_nov
        info["secoes_concatenadas"].append(f"{tit} → {alvo['titulo']}")

    reconstruido = "\n\n".join(
        re.sub(r"\n{3,}", "\n\n", "\n".join(s["linhas"]).strip())
        for s in consolidadas
        if any(l.strip() for l in s["linhas"])
    )
    reconstruido = re.sub(r"\n{3,}", "\n\n", reconstruido).strip()

    if nums_antes - set(_numeros(reconstruido)):
        return texto, {"secoes_concatenadas": [], "bullets_absorvidos": []}
    return reconstruido, info


def consolidar_secoes_mesmo_assunto(texto):
    """Versão simplificada de `consolidar_secoes_mesmo_assunto_detalhado`: retorna apenas o Markdown."""
    novo, _ = consolidar_secoes_mesmo_assunto_detalhado(texto)
    return novo


def deduplicar_secoes(texto, consolidar=True):
    """Remove seções com título repetido e parágrafos/seções redundantes sem perder conteúdo.

    - Títulos de nível 2 (##) são deduplicados no escopo global do documento.
    - Subtítulos de nível 3+ (###) são deduplicados apenas dentro do mesmo ## pai,
      impedindo que subseções genéricas (ex.: '### Resumo') de matérias diferentes se fundam.
    - Mesmo título e mesmo corpo: a seção repetida é descartada.
    - Mesmo título e corpo diferente: o corpo é anexado à primeira ocorrência
      (antes, só o título era apagado e o corpo ficava "colado" na seção anterior).
    - Parágrafos de sinopse/ficha técnica repetidos entre seções `##` distintas são removidos.
    """
    blocos = [{"nivel": 0, "titulo": None, "escopo_h2": None, "linhas": []}]
    escopo_h2 = None
    for linha in texto.split("\n"):
        m = _HEADING.match(linha)
        if m:
            nivel = len(m.group(1))
            titulo = m.group(2).strip().lower()
            if nivel == 2:
                escopo_h2 = titulo
            blocos.append({"nivel": nivel, "titulo": titulo, "escopo_h2": escopo_h2, "linhas": [linha]})
        else:
            blocos[-1]["linhas"].append(linha)

    saida = []
    vistos = {}
    for bloco in blocos:
        titulo = bloco["titulo"]
        if titulo is None:
            saida.append(bloco)
            continue
        nivel = bloco["nivel"]
        chave = ("h2", bloco["escopo_h2"]) if nivel == 2 else ("sub", bloco["escopo_h2"], nivel, titulo)
        primeiro = vistos.get(chave)
        if primeiro is None:
            vistos[chave] = bloco
            saida.append(bloco)
            continue

        miolo, rabo = _separar_rabo(bloco["linhas"][1:])
        corpo_novo = _normalizar(miolo)
        if corpo_novo and corpo_novo not in _normalizar(primeiro["linhas"][1:]):
            conteudo_primeiro, rabo_primeiro = _separar_rabo(primeiro["linhas"])
            primeiro["linhas"] = conteudo_primeiro + [""] + miolo + rabo_primeiro
        if rabo:
            saida.append({"nivel": 0, "titulo": None, "escopo_h2": None, "linhas": rabo})

    linhas = [linha for b in saida for linha in b["linhas"]]
    unificado = re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()
    unificado = _remover_paragrafos_redundantes(unificado)
    return consolidar_secoes_mesmo_assunto(unificado) if consolidar else unificado


def inserir_apos_h1(corpo, bloco_extra):
    """Insere um bloco extra (ex.: placares de futebol) logo após o cabeçalho # da linha 1."""
    if not bloco_extra or not bloco_extra.strip():
        return corpo
    corpo_limpo = corpo.strip()
    extra_limpo = bloco_extra.strip()
    m = re.match(r"^(#\s+[^\n]+)\n*(.*)$", corpo_limpo, flags=re.S)
    if m:
        h1, resto = m.group(1).strip(), m.group(2).strip()
        if resto:
            return f"{h1}\n\n{extra_limpo}{SEPARADOR}{resto}"
        return f"{h1}\n\n{extra_limpo}"
    return f"{extra_limpo}{SEPARADOR}{corpo_limpo}"


def aplicar_cabecalho_edicao(texto, nome_final, idioma="pt"):
    """Inclui nome do programa, número da edição e dia da semana no cabeçalho H1 do topo para o NotebookLM:
    Ex.: '# 10 de novembro de 2026' + 'Controle Quebrado #274.md'
      -> '# Controle Quebrado #274 — terça-feira, 10 de novembro de 2026'
    """
    if not texto or not nome_final:
        return texto
    identificador = Path(nome_final).stem.strip()
    if not identificador:
        return texto
    texto_limpo = texto.strip()
    m = re.match(r"^#\s+([^\n]+)(.*)$", texto_limpo, flags=re.S)
    if m:
        h1_atual = m.group(1).strip()
        resto = m.group(2)
        re_dia = _RE_DIA_SEMANA_EN if idioma == "en" else _RE_DIA_SEMANA_PT
        dt = extrair_data_de_texto(h1_atual)
        if h1_atual.lower().startswith(identificador.lower()):
            if dt and not re_dia.search(h1_atual):
                data_fmt = formatar_data_com_dia_semana(dt, idioma=idioma)
                return f"# {identificador} — {data_fmt}{resto}"
            return texto_limpo
        if dt and not re_dia.search(h1_atual):
            h1_atual = formatar_data_com_dia_semana(dt, idioma=idioma)
        return f"# {identificador} — {h1_atual}{resto}"
    return f"# {identificador}\n\n{texto_limpo}"


def sanitizar_markdown_final(texto, consolidar=True):
    """Higieniza o Markdown final antes de salvar e subir para o Drive:
    - Remove eventuais cercas ```markdown externas devolvidas pela IA.
    - Garante no máximo 1 título de nível 1 (#) no documento (rebaixa H1 extras para H2).
    - Remove pontuações indesejadas no final de cabeçalhos (regras para o NotebookLM).
    - Remove descrições de imagem que sejam apenas créditos de agência/fotógrafo.
    - Remove CTAs residuais como (Aprofunde) e links quebrados de redes sociais.
    - Executa a deduplicação de seções repetidas.
    """
    m_cerca = _RE_CERCA_EXTERNA.match(texto)
    if m_cerca:
        texto = m_cerca.group(1)

    texto = _RE_APROFUNDE.sub("", texto)
    texto = _RE_TWITTER_LIXO.sub("", texto)
    texto = _RE_MOEDA_DUPLICADA.sub(r"\1", texto)
    texto = _RE_NAO_PEGADINHA_RISOS.sub("", texto)
    texto = _RE_RISOS_CACOETE.sub(lambda m: "" if m.group(0).lstrip().startswith("(") else ".", texto)
    texto = _RE_SUBTITULO_ESTAGI.sub(r"\1Entenda o contexto", texto)
    texto = _RE_CONECTOR_ORFAO_SECAO.sub(lambda m: m.group(1) + m.group(2).upper(), texto)
    texto = _RE_TEXTO_MOSTRA.sub(
        lambda m: ("A" if m.group(1) == "O" else "a") + " análise mostra como",
        texto,
    )

    linhas = texto.splitlines()
    linhas_processadas = []
    viu_h1 = False

    for linha in linhas:
        l_strip = linha.strip()

        # Remove imagens ou linhas que sejam apenas créditos de fotógrafo/agência ou aspas/embeds vazios
        if (
            _RE_CREDITO_RESIDUAL.match(l_strip)
            or _RE_LINHA_CREDITO_SOLTO.match(l_strip)
            or re.match(r"^\*{0,2}[“\"”❝❞]\*{0,2}$", l_strip)
            or re.match(r"^>\s*[—–\-]\s*\(@\)\s*$", l_strip)
        ):
            continue

        # Cabeçalho H1 (#)
        if re.match(r"^#\s+", linha):
            if not viu_h1:
                viu_h1 = True
                linhas_processadas.append(linha)
            else:
                # Rebaixa H1 subsequente para H2
                linhas_processadas.append("#" + linha)
            continue

        # Remove pontuação no fim de títulos (## Titulo? -> ## Titulo)
        m_tit = _RE_TITULO_PONTUADO.match(linha)
        if m_tit and linha.startswith("#"):
            linha = m_tit.group(1)

        linhas_processadas.append(linha)

    limpo = "\n".join(linhas_processadas)
    limpo = deduplicar_secoes(limpo, consolidar=consolidar)
    return re.sub(r"\n{3,}", "\n\n", limpo).strip()


def eh_extracao_vazia_ou_invalida(texto):
    """Detecta quando o e-mail era puramente promocional/administrativo e não gerou pauta real."""
    if not texto:
        return True
    limpo = texto.strip()
    if "[SEM_CONTEUDO_EDITORIAL]" in limpo:
        return True
    # Remove o título H1 da data para medir se sobrou corpo editorial real
    sem_h1 = re.sub(r"^#\s+.*$", "", limpo, flags=re.M).strip()
    return len(sem_h1) < MINIMO_CARACTERES_EDICAO


# --- Pipeline -------------------------------------------------------------------

class Pipeline:
    def __init__(self, opcoes, extrator, drive_service, gmail_service):
        self.opcoes = opcoes
        self.extrator = extrator
        self.drive = drive_service
        self.gmail = gmail_service
        self.prompt_base = carregar_prompt("espelho_magico")
        self.prompt_unificacao = carregar_prompt("unificacao")
        self.pasta_saida = config.DRY_RUN_DIR if opcoes.dry_run else config.RESULTADOS_DIR
        self.pasta_historico = config.RESULTADOS_DIR
        self.caminho_controle_noticias = config.CONTROLE_NOTICIAS_PATH
        self.caminho_controle_fontes = config.CONTROLE_FONTES_PATH
        self.pares_fontes = config.PARES_FONTES
        self.em_standby = []
        self._hoje_ref = None
        self._lock_pendentes = threading.Lock()
        self._nao_iniciados = 0
        self._progress = None
        self._tarefas = {}
        self._tls_extracao = threading.local()

    # -- painel de progresso por programa --
    def _etapa(self, cfg, texto, passo=None):
        """Atualiza a linha do programa no painel de progresso (ex.: '🤖 IA · parte 2/3')."""
        if not self._progress or cfg is None:
            return
        tarefa = self._tarefas.get(cfg.nome_final)
        if tarefa is None:
            return
        dados = {"description": f"[bold]{escape(Path(cfg.nome_final).stem)}[/bold] [dim]·[/dim] {texto}"}
        if passo is not None:
            dados["completed"] = passo
        self._progress.update(tarefa, **dados)
        if passo is not None and passo >= ETAPAS_GRUPO:
            self._progress.stop_task(tarefa)

    # -- etapa interativa (sequencial) --
    def _perguntar(self, mensagem):
        return input(mensagem) if self.opcoes.interativo else ""

    def _proximo_nome_edicao(self, titulo_base, prox_por_titulo):
        if titulo_base in prox_por_titulo:
            numero = str(prox_por_titulo[titulo_base])
            prox_por_titulo[titulo_base] += 1
            logger.info(f"[bold green]▶ Número da edição automatizado (sequencial): {numero}[/bold green]")
            return f"{titulo_base} #{numero}.md"
        maior = maior_edicao_existente(self.pasta_historico, titulo_base)
        if maior > 0:
            numero = str(maior + 1)
            prox_por_titulo[titulo_base] = maior + 2
            logger.info(f"[bold green]▶ Número da edição automatizado: {numero} (última encontrada: {maior})[/bold green]")
        else:
            numero = self._perguntar(f"▶ Qual o número da edição para '{titulo_base}'? ").strip()
            if not numero:
                numero = time.strftime("%Y%m%d")
                logger.info(f"[yellow]▶ Sem número informado. Usando a data: {numero}[/yellow]")
            elif numero.isdigit():
                prox_por_titulo[titulo_base] = int(numero) + 1
        return f"{titulo_base} #{numero}.md"

    def _corresponde_filtro(self, chaves, titulo_base):
        if not self.opcoes.grupos:
            return True
        termos = [titulo_base.lower()] + [c.lower() for c in chaves]
        for alias, dest in _ALIASES_PREFIXO.items():
            if dest in termos:
                termos.append(alias)
        return any(f.lower() in termo for f in self.opcoes.grupos for termo in termos)

    def _decidir_unificar(self, titulo_base, emails_grupo, forcar_auto=False):
        if len(emails_grupo) < 2:
            return False
        if self.opcoes.unificar == "nao":
            return False
        if forcar_auto or self.opcoes.unificar == "sim":
            return True
        if self.opcoes.unificar == "perguntar" and self.opcoes.interativo:
            resposta = self._perguntar(
                f"▶ {len(emails_grupo)} e-mails para '{titulo_base}'. Unificar de forma inteligente "
                f"em um único texto (mesclando assuntos e evitando repetições)? (S/N): "
            )
            return resposta.strip().lower() == "s"
        return False

    def _preparar_grupos_pareados(self, grupos, prox_por_titulo):
        """Agrupa fontes pareadas (ex.: tns money+moneydrops, techdrops+tns business), aplicando stand by e dedup."""
        hoje = self._hoje_ref or datetime.date.today()
        configs_par = []
        chaves_consumidas = set()

        for par in (self.pares_fontes or []):
            titulo_base = par.get("titulo") or ""
            fontes_par = [f.lower() for f in (par.get("fontes") or [])]
            if not titulo_base or not fontes_par:
                continue
            chaves_consumidas.update(fontes_par)

            itens = []
            for f in fontes_par:
                for e in grupos.get(f, []):
                    e.setdefault("fonte_chave", f)
                    dt_item = _extrair_data_item(e) or hoje
                    itens.append((f, e, dt_item))
            if not itens or not self._corresponde_filtro(fontes_par, titulo_base):
                continue

            ordem_fonte = {f: idx for idx, f in enumerate(fontes_par)}
            itens.sort(key=lambda trio: (trio[2], ordem_fonte.get(trio[0], 99), _chave_cronologica_email(trio[1])))
            regras = par.get("regras_pareamento") or []
            expira_dias = int(par.get("expirar_standby_dias", 1))
            cfg_evitar = par.get("evitar_repeticao_com_ultima_edicao") or {}

            def buscar_regra(fonte, dt):
                return next(
                    (r for r in regras if (r.get("fonte") or "").lower() == fonte and dt.weekday() in (r.get("dias") or [])),
                    None,
                )

            usados = set()
            # 1ª passagem: monta pares completos (fonte A + fonte B na data esperada)
            for i, (f1, e1, d1) in enumerate(itens):
                if i in usados:
                    continue
                regra = buscar_regra(f1, d1)
                if not regra:
                    continue
                alvo_f = (regra.get("aguarda") or "").lower()
                alvo_d = d1 + datetime.timedelta(days=int(regra.get("delta_dias", 0)))
                indices_par = [i]
                achou_parceiro = False
                for j, (f2, _, d2) in enumerate(itens):
                    if j == i or j in usados:
                        continue
                    if f2 == alvo_f and d2 == alvo_d:
                        indices_par.append(j)
                        achou_parceiro = True
                    elif f2 == f1 and d2 == d1:
                        indices_par.append(j)
                if achou_parceiro:
                    for idx_u in indices_par:
                        usados.add(idx_u)
                    lote = [itens[idx_u] for idx_u in indices_par]
                    lote.sort(key=lambda trio: (trio[2], ordem_fonte.get(trio[0], 99), _chave_cronologica_email(trio[1])))
                    emails_lote = [trio[1] for trio in lote]
                    chaves_lote = list(dict.fromkeys(trio[0] for trio in lote))
                    logger.info(
                        f"\n[bold magenta]🔗 Par completo para {escape(titulo_base)}: "
                        f"{' + '.join(chaves_lote)} ({len(emails_lote)} item(ns))[/bold magenta]"
                    )
                    nome_final = self._proximo_nome_edicao(titulo_base, prox_por_titulo)
                    unificar = self._decidir_unificar(titulo_base, emails_lote, forcar_auto=True)
                    configs_par.append(
                        ConfigGrupo(
                            chaves_lote[0],
                            emails_lote,
                            titulo_base,
                            nome_final,
                            unificar=unificar,
                            chaves_fontes=chaves_lote,
                        )
                    )

            # 2ª passagem: itens sem par (saem sozinhos nos dias avulsos, entram em stand by ou expiram)
            for i, (f1, e1, d1) in enumerate(itens):
                if i in usados:
                    continue
                usados.add(i)
                emails_solo = [e1]
                for j, (f2, e2, d2) in enumerate(itens):
                    if j not in usados and f2 == f1 and d2 == d1:
                        usados.add(j)
                        emails_solo.append(e2)

                regra = buscar_regra(f1, d1)
                if regra:
                    alvo_f = (regra.get("aguarda") or "").lower()
                    alvo_d = d1 + datetime.timedelta(days=int(regra.get("delta_dias", 0)))
                    expirado = (hoje - alvo_d).days >= expira_dias
                    if not (self.opcoes.forcar_pares or expirado):
                        expira_em = alvo_d + datetime.timedelta(days=expira_dias)
                        self.em_standby.append(
                            ItemStandBy(
                                titulo_base=titulo_base,
                                fonte_recebida=f1,
                                data_recebida=d1,
                                aguardando=alvo_f,
                                data_esperada=alvo_d,
                                expira_em=expira_em,
                            )
                        )
                        logger.info(
                            f"[yellow]⏸ {escape(titulo_base)} em stand by: chegou '{escape(f1)}' ({d1:%d/%m}), "
                            f"aguardando '{escape(alvo_f)}' ({alvo_d:%d/%m}). E-mail mantido não lido.[/yellow]"
                        )
                        continue
                    motivo = "--forcar-pares" if self.opcoes.forcar_pares else f"stand by expirado ({alvo_f} de {alvo_d:%d/%m} não saiu)"
                    logger.info(
                        f"\n[yellow]⏰ Liberando {escape(titulo_base)} ({escape(f1)} {d1:%d/%m}) sem par: {escape(motivo)}[/yellow]"
                    )
                else:
                    logger.info(
                        f"\n[bold magenta]Encontrados {len(emails_solo)} item(ns) avulso(s) para: "
                        f"{escape(titulo_base)} ({escape(f1)})[/bold magenta]"
                    )

                contexto_ant = ""
                if (
                    cfg_evitar
                    and (cfg_evitar.get("fonte") or "").lower() == f1
                    and d1.weekday() in (cfg_evitar.get("dias") or [])
                ):
                    contexto_ant = montar_contexto_edicao_anterior(
                        self.pasta_historico,
                        titulo_base,
                        d1,
                        janela_dias=int(cfg_evitar.get("janela_dias", 2)),
                    )
                    if contexto_ant:
                        logger.info(
                            f"[cyan]🧹 Filtro anti-repetição ativo para {escape(titulo_base)} ({escape(f1)} sexta): "
                            f"evitando pautas já cobertas na edição anterior[/cyan]"
                        )

                nome_final = self._proximo_nome_edicao(titulo_base, prox_por_titulo)
                unificar = self._decidir_unificar(titulo_base, emails_solo, forcar_auto=False)
                configs_par.append(
                    ConfigGrupo(
                        f1,
                        emails_solo,
                        titulo_base,
                        nome_final,
                        unificar=unificar,
                        chaves_fontes=[f1],
                        contexto_edicao_anterior=contexto_ant,
                    )
                )

        return configs_par, chaves_consumidas

    def _aguardar_edicao_night_sozinha(self, chave, titulo_base, emails_grupo):
        """Quando 'the news' (Boletim Fora de Ordem) traz apenas a edição noturna (NIGHT),
        pergunta ao usuário se quer extrair sozinha agora ou aguardar a edição da manhã do dia seguinte.
        Em modo não interativo (--sem-interacao), mantém em stand by até expirar (1 dia após a manhã esperada).
        """
        if (chave or "").lower() != "the news" or not emails_grupo:
            return False
        if not all(_eh_edicao_night(e) for e in emails_grupo):
            return False
        if self.opcoes.urls or self.opcoes.forcar_pares:
            return False

        hoje = self._hoje_ref or datetime.date.today()
        datas_validas = [d for d in (_extrair_data_item(e) for e in emails_grupo) if d is not None]
        dt_night = max(datas_validas) if datas_validas else hoje
        data_esperada = dt_night + datetime.timedelta(days=1)
        expira_em = data_esperada + datetime.timedelta(days=1)
        expirado = (hoje - data_esperada).days >= 1

        if self.opcoes.interativo:
            resposta = self._perguntar(
                f"\n▶ {titulo_base}: encontrada apenas a edição noturna ({dt_night:%d/%m}), "
                f"que normalmente faz par com a edição da manhã de {data_esperada:%d/%m}. "
                f"Extrair sozinha agora [S] ou aguardar o dia seguinte [N]? (s/N): "
            )
            if resposta.strip().lower() == "s":
                return False
        elif expirado:
            logger.info(
                f"\n[yellow]⏰ Liberando {escape(titulo_base)} (edição noturna {dt_night:%d/%m}) sem par: "
                f"stand by expirado (edição da manhã de {data_esperada:%d/%m} não saiu)[/yellow]"
            )
            return False

        self.em_standby.append(
            ItemStandBy(
                titulo_base=titulo_base,
                fonte_recebida="the news night",
                data_recebida=dt_night,
                aguardando="the news manhã",
                data_esperada=data_esperada,
                expira_em=expira_em,
            )
        )
        tem_gmail = any(e.get("id") for e in emails_grupo)
        detalhe_guarda = "E-mail mantido não lido." if tem_gmail else "Edição não marcada como extraída."
        logger.info(
            f"[yellow]⏸ {escape(titulo_base)} em stand by: chegou apenas a edição noturna ({dt_night:%d/%m}), "
            f"aguardando a edição da manhã ({data_esperada:%d/%m}). {detalhe_guarda}[/yellow]"
        )
        return True

    def preparar_grupos(self, emails):
        self.em_standby = []
        grupos = agrupar_emails(emails, config.MAPEAMENTO_PREFIXOS)
        prox_por_titulo = {}
        configs, chaves_pareadas = self._preparar_grupos_pareados(grupos, prox_por_titulo)

        for chave, emails_grupo in grupos.items():
            if chave.lower() in chaves_pareadas:
                continue
            for e in emails_grupo:
                e.setdefault("fonte_chave", chave)
            if chave in config.MAPEAMENTO_PREFIXOS:
                titulo_base = config.MAPEAMENTO_PREFIXOS[chave]
            else:
                titulo_base, nome_final = nome_fallback(chave)

            if not self._corresponde_filtro([chave], titulo_base):
                continue

            if self._aguardar_edicao_night_sozinha(chave, titulo_base, emails_grupo):
                continue

            if chave in config.MAPEAMENTO_PREFIXOS:
                logger.info(f"\n[bold magenta]Encontrados {len(emails_grupo)} e-mail(s) para: {escape(titulo_base)}[/bold magenta]")
                nome_final = self._proximo_nome_edicao(titulo_base, prox_por_titulo)

            emails_grupo.sort(key=_chave_cronologica_email)
            unificar = self._decidir_unificar(titulo_base, emails_grupo, forcar_auto=False)
            configs.append(ConfigGrupo(chave, emails_grupo, titulo_base, nome_final, unificar, chaves_fontes=[chave]))
        return configs

    # -- extração --
    def _montar_prompt(self, chave, unificar=False, chaves_fontes=None, contexto_edicao_anterior=""):
        partes = [self.prompt_base]
        chaves_todas = list(dict.fromkeys((c or "").lower() for c in (chaves_fontes or [chave]) if c))
        dicas = []
        for c in chaves_todas:
            dica = config.DICAS_POR_PREFIXO.get(c)
            if dica:
                prefixo_dica = f"[{c}] " if len(chaves_todas) > 1 else ""
                dicas.append(f"{prefixo_dica}{dica}")
        if dicas:
            bloco_dicas = "\n\n".join(dicas)
            partes.append(f"<diretrizes_especificas_desta_fonte>\n{bloco_dicas}\n</diretrizes_especificas_desta_fonte>")
        if unificar:
            partes.append(self.prompt_unificacao)
        if contexto_edicao_anterior:
            partes.append(
                "<evitar_repeticao_edicao_anterior>\n"
                "ATENÇÃO — EVITAR REPETIÇÃO COM A EDIÇÃO RECENTE ANTERIOR DESTE PROGRAMA:\n"
                f"{contexto_edicao_anterior}\n\n"
                "REGRAS ESTRITAS:\n"
                "1. Se alguma matéria da fonte de hoje tratar do MESMO fato/acontecimento já coberto nessa edição anterior "
                "sem novidade relevante, DESCARTE essa matéria repetida por completo (não reescreva nem resuma).\n"
                "2. Se houver um desdobramento genuinamente novo sobre tema de ontem, extraia APENAS a novidade em si.\n"
                "3. Extraia na íntegra TODAS as demais notícias inéditas e o quadro inicial ('Hoje eu aprendi', se houver).\n"
                "4. Se a edição inteira só tiver assuntos repetidos, responda APENAS com `[SEM_CONTEUDO_EDITORIAL]`.\n"
                "</evitar_repeticao_edicao_anterior>"
            )
        return "\n\n".join(partes)

    @staticmethod
    def _preparar(email_item, idioma="pt", envolver_documento=True):
        """Conteúdo a enviar para a IA: metadados de data/assunto + HTML pré-limpo em Markdown."""
        bruto = email_item["conteudo_html"]
        if email_item.get("eh_html", True):
            conteudo = preparar_conteudo(bruto, config.HTML_PRECLEAN)
        else:
            conteudo = limpar_linhas(bruto)
        if conteudo is not bruto and len(bruto):
            reducao = 100 - (len(conteudo) * 100 // len(bruto))
            logger.info(f"[dim]✂ Pré-limpeza: {len(bruto):,} → {len(conteudo):,} caracteres (-{reducao}%)[/dim]")

        data_fmt = formatar_data_email(email_item.get("data", ""), idioma=idioma)
        assunto = email_item.get("assunto", "")
        meta = []
        if idioma == "en":
            if data_fmt:
                meta.append(f"Email Date: {data_fmt}")
            if assunto:
                meta.append(f"Subject: {assunto}")
            meta.append("Output Language: 100% English")
        else:
            if data_fmt:
                meta.append(f"Data do e-mail: {data_fmt}")
            if assunto:
                meta.append(f"Assunto: {assunto}")

        corpo_final = f"<documento_entrada>\n{conteudo}\n</documento_entrada>" if envolver_documento else conteudo
        if meta:
            bloco_meta = f"<metadados>\n[METADADOS DO E-MAIL — {' | '.join(meta)}]\n</metadados>"
            return f"{bloco_meta}\n\n{corpo_final}"
        return corpo_final

    def _extrair_tier_principal(self, conteudo, prompt):
        """Chama o extrator restringindo aos modelos principais quando suportado."""
        try:
            return self.extrator.extrair(conteudo, prompt, apenas_tier_principal=True)
        except TypeError:
            return self.extrator.extrair(conteudo, prompt)

    def _revisar_parte_rebaixada(self, parte, prompt_base, rotulo, modelos_anteriores, cfg=None):
        """Reextrai uma parte que caiu em modelo leve (lite/gemma) tentando o tier principal."""
        modelos_str = ", ".join(dict.fromkeys(m for m in modelos_anteriores if modelo_eh_inferior(m)))
        logger.info(
            f"[cyan]🔄 Segunda passagem ({escape(rotulo)}): extraída por modelo leve ({escape(modelos_str)}) "
            f"→ revisando com modelo principal...[/cyan]"
        )
        self._etapa(cfg, f"🔄 2ª passagem (modelo principal)")
        prompt_rev = f"{prompt_base}\n\n{_INSTRUCAO_SEGUNDA_PASSAGEM}"
        if hasattr(self.extrator, "resetar_modelos_usados"):
            self.extrator.resetar_modelos_usados()
        try:
            texto_rev = self._extrair_tier_principal(parte, prompt_rev)
        except FalhaExtracao as e:
            logger.warning(
                f"[yellow]⚠ Segunda passagem ({escape(rotulo)}) não conseguiu modelo principal "
                f"({escape(str(e))}); mantendo extração inicial.[/yellow]"
            )
            return None
        if not texto_rev or not texto_rev.strip():
            return None
        logger.info(f"[green]✔ Segunda passagem ({escape(rotulo)}) concluída com modelo principal![/green]")
        return texto_rev.strip()

    def _extrair(self, conteudo, prompt, rotulo, cfg=None):
        partes = dividir_conteudo_com_metadados(conteudo, max_chars=config.MAX_CHARS_POR_CHAMADA)
        if len(partes) > 1:
            self._tls_extracao.foi_fatiado = True
            logger.info(
                f"[cyan]✂ Conteúdo extenso ({len(conteudo):,} caracteres): dividido em {len(partes)} partes "
                f"(~{len(conteudo) // len(partes):,} cada) para evitar timeout (504) e limite de tokens (413)[/cyan]"
            )
            extraidas = []
            rebaixadas = []
            for idx, parte in enumerate(partes, 1):
                rotulo_parte = f"{rotulo} [parte {idx}/{len(partes)}]"
                self._etapa(cfg, f"🤖 extraindo com IA · parte {idx}/{len(partes)}")
                logger.info(f"[cyan]🤖 Parte {idx}/{len(partes)} → IA[/cyan]")
                prompt_parte = f"{prompt}\n\n{_INSTRUCAO_PARTE.format(i=idx, n=len(partes))}"
                texto_parte, modelos_parte = self._extrair_unico(parte, prompt_parte, rotulo_parte)
                if not texto_parte:
                    return None
                extraidas.append(texto_parte)
                if any(modelo_eh_inferior(m) for m in modelos_parte):
                    rebaixadas.append((idx - 1, parte, prompt_parte, rotulo_parte, modelos_parte))
                if idx < len(partes):
                    time.sleep(2)

            if rebaixadas and config.DUPLA_PASSAGEM_REVISAO:
                self._tls_extracao.houve_downgrade = True
                for pos, parte, prompt_parte, rotulo_parte, modelos_parte in rebaixadas:
                    revisada = self._revisar_parte_rebaixada(parte, prompt_parte, rotulo_parte, modelos_parte, cfg)
                    if revisada:
                        extraidas[pos] = revisada
            elif rebaixadas:
                self._tls_extracao.houve_downgrade = True

            return unir_partes_extraidas(extraidas)

        texto, modelos_usados = self._extrair_unico(conteudo, prompt, rotulo)
        if not texto:
            return None
        if len(modelos_usados) > 1:
            self._tls_extracao.foi_fatiado = True
        if any(modelo_eh_inferior(m) for m in modelos_usados):
            self._tls_extracao.houve_downgrade = True
            if config.DUPLA_PASSAGEM_REVISAO:
                revisada = self._revisar_parte_rebaixada(conteudo, prompt, rotulo, modelos_usados, cfg)
                if revisada:
                    texto = revisada
        return texto

    def _extrair_unico(self, conteudo, prompt, rotulo):
        if hasattr(self.extrator, "resetar_modelos_usados"):
            self.extrator.resetar_modelos_usados()
        try:
            texto = self.extrator.extrair(conteudo, prompt)
        except FalhaExtracao as e:
            logger.error(f"[bold red]✖ {escape(str(e))} ({escape(rotulo)})[/bold red]")
            return None, []
        modelos_usados = (
            self.extrator.obter_modelos_usados() if hasattr(self.extrator, "obter_modelos_usados") else []
        )
        if config.HTML_PRECLEAN and not verificar_completude(conteudo, texto):
            logger.warning(
                f"[yellow]⚠ Extração de '{escape(rotulo)}' parece incompleta "
                f"({len(texto):,} caracteres gerados para {len(conteudo):,} de entrada). Vale revisar.[/yellow]"
            )
        texto_limpo = texto.strip()
        if "[SEM_CONTEUDO_EDITORIAL]" not in texto_limpo and not re.search(r"^#\s+\S+", texto_limpo, flags=re.M):
            logger.warning(
                f"[yellow]⚠ Extração de '{escape(rotulo)}' veio sem cabeçalho '# Data' de nível 1.[/yellow]"
            )
        return texto_limpo, modelos_usados

    def processar_grupo(self, cfg):
        with self._lock_pendentes:
            self._nao_iniciados -= 1
        try:
            with contexto_grupo(Path(cfg.nome_final).stem):
                return self._processar_grupo(cfg)
        finally:
            with self._lock_pendentes:
                ha_mais = self._nao_iniciados > 0
            if ha_mais and config.PAUSA_ENTRE_GRUPOS > 0:
                logger.info(f"[dim]⏳ Aguardando {config.PAUSA_ENTRE_GRUPOS}s para não sobrecarregar a API...[/dim]")
                time.sleep(config.PAUSA_ENTRE_GRUPOS)

    def _processar_grupo(self, cfg):
        inicio = time.time()
        self._tls_extracao.foi_fatiado = False
        self._tls_extracao.houve_downgrade = False
        nome = Path(cfg.nome_final).stem
        n_emails = len(cfg.emails)
        logger.info(
            f"\n[bold blue]▶ Iniciando {escape(cfg.nome_final)}[/bold blue] "
            f"[dim]({n_emails} e-mail(s){' · unificados' if cfg.unificar else ''})[/dim]"
        )
        self._etapa(cfg, "✂ pré-limpeza do HTML", 0)

        resultados_futebol, data_futebol = "", None
        if cfg.chave == config.PREFIXO_FUTEBOL:
            self._etapa(cfg, "⚽ buscando placares na ESPN")
            resultados_futebol, data_futebol = futebol.coletar_resultados()

        idioma = "en" if (cfg.chave or "").lower() in config.PREFIXOS_INGLES else "pt"
        dedup_ativo = deve_deduplicar_noticias(cfg.chave)
        itens_bootstrap, inicializado_agora = [], False
        emails_para_extrair = [(e, []) for e in cfg.emails]
        emails_so_duplicados = []

        if dedup_ativo:
            ids_atuais = [e.get("id") for e in cfg.emails if e.get("id")]
            chaves_vistas, itens_bootstrap, inicializado_agora = carregar_historico_noticias(
                cfg.chave,
                cfg.titulo_base,
                gmail_service=self.gmail,
                ids_excluir=ids_atuais,
                caminho_controle=self.caminho_controle_noticias,
                pasta_resultados=self.pasta_historico,
            )
            if itens_bootstrap:
                logger.info(
                    f"[dim]📚 Histórico carregado das extrações anteriores: "
                    f"{len(itens_bootstrap)} notícia(s) registrada(s)[/dim]"
                )

            filtrados = []
            for e in cfg.emails:
                e_filtrado, removidas, mantidas, total_cards = filtrar_email_noticias_repetidas(e, chaves_vistas)
                if removidas:
                    amostra = ", ".join(f"'{r['titulo'][:45]}' ({r['data_hora']})" for r in removidas[:3])
                    sufixo_am = "…" if len(removidas) > 3 else ""
                    logger.info(
                        f"[cyan]🧹 Removida(s) {len(removidas)} notícia(s) repetida(s) de extrações anteriores: "
                        f"{escape(amostra)}{sufixo_am}[/cyan]"
                    )
                if total_cards > 0 and not mantidas:
                    logger.info(
                        f"[yellow]⏭ E-mail '{escape(e.get('assunto', ''))}' continha apenas "
                        f"notícias repetidas ({len(removidas)}). Ignorado.[/yellow]"
                    )
                    emails_so_duplicados.append(e)
                else:
                    filtrados.append((e_filtrado, mantidas))
            emails_para_extrair = filtrados

            if not emails_para_extrair:
                if not self.opcoes.dry_run:
                    if inicializado_agora:
                        registrar_noticias_extraidas(
                            cfg.chave, [], itens_bootstrap=itens_bootstrap,
                            marcar_inicializado=True, caminho_controle=self.caminho_controle_noticias,
                        )
                    if self.gmail:
                        for email_item in emails_so_duplicados:
                            if email_item.get("id"):
                                marcar_como_lido(self.gmail, email_item["id"])
                self._etapa(cfg, "[yellow]⏭ ignorado (notícias repetidas)[/yellow]", ETAPAS_GRUPO)
                return ResultadoGrupo(
                    nome,
                    "ignorado",
                    f"⏭️ {cfg.nome_final} ignorado (apenas notícias repetidas)",
                    "Ignorado: apenas notícias repetidas",
                    time.time() - inicio,
                )

        textos, emails_ok, entradas, itens_extraidos_ok = [], [], [], []
        chaves_fontes = cfg.chaves_fontes or [cfg.chave]
        if cfg.unificar and len(emails_para_extrair) >= 2:
            def _rotulo_edicao(idx, item):
                fk = (item.get("fonte_chave") or "").strip()
                return f"--- INÍCIO DA EDIÇÃO {idx} ({fk}) ---" if fk and len(chaves_fontes) > 1 else f"--- INÍCIO DA EDIÇÃO {idx} ---"

            partes_edicoes = "".join(
                f"\n\n{_rotulo_edicao(i + 1, e)}\n\n{self._preparar(e, idioma=idioma, envolver_documento=False)}"
                for i, (e, _) in enumerate(emails_para_extrair)
            ).strip()
            combinado = f"<documento_entrada>\n{partes_edicoes}\n</documento_entrada>"
            entradas.append(combinado)
            logger.info(f"[cyan]🤖 Enviando {len(emails_para_extrair)} e-mails agrupados para extração unificada...[/cyan]")
            self._etapa(cfg, "🤖 extraindo com IA (unificado)", 1)
            prompt_grupo = self._montar_prompt(
                cfg.chave,
                unificar=True,
                chaves_fontes=chaves_fontes,
                contexto_edicao_anterior=cfg.contexto_edicao_anterior,
            )
            texto = self._extrair(combinado, prompt_grupo, cfg.titulo_base, cfg)
            if texto:
                textos.append(texto)
                for e, mantidas in emails_para_extrair:
                    emails_ok.append(e)
                    itens_extraidos_ok.extend(mantidas)
        else:
            prompt_grupo = self._montar_prompt(
                cfg.chave,
                unificar=False,
                chaves_fontes=chaves_fontes,
                contexto_edicao_anterior=cfg.contexto_edicao_anterior,
            )
            n_ext = len(emails_para_extrair)
            for i, (email_item, mantidas) in enumerate(emails_para_extrair):
                conteudo = self._preparar(email_item, idioma=idioma)
                entradas.append(conteudo)
                sufixo = f" · e-mail {i + 1}/{n_ext}" if n_ext > 1 else ""
                self._etapa(cfg, f"🤖 extraindo com IA{sufixo}", 1)
                texto = self._extrair(conteudo, prompt_grupo, email_item["assunto"], cfg)
                if texto:
                    textos.append(texto)
                    emails_ok.append(email_item)
                    itens_extraidos_ok.extend(mantidas)
                if i < n_ext - 1:
                    time.sleep(2)

        if not textos:
            logger.error(f"[bold red]✖ Falha ao gerar texto para {escape(cfg.nome_final)}[/bold red]")
            self._etapa(cfg, "[red]✖ falha na IA[/red]", ETAPAS_GRUPO)
            return ResultadoGrupo(nome, "erro", f"❌ Erro ao gerar {cfg.nome_final}", "Falha na IA (e-mails continuam não lidos)",
                                  time.time() - inicio)

        corpo_editorial = sanitizar_markdown_final(SEPARADOR.join(textos), consolidar=False)
        if eh_extracao_vazia_ou_invalida(corpo_editorial):
            logger.warning(
                f"[bold yellow]⚠ E-mail(s) sem conteúdo editorial válido ({len(corpo_editorial)} caracteres). "
                f"Edição descartada para não gerar arquivo vazio.[/bold yellow]"
            )
            if not self.opcoes.dry_run:
                registrar_fontes_processadas(emails_ok + emails_so_duplicados, cfg.chave, self.caminho_controle_fontes)
                if self.gmail:
                    for email_item in emails_ok + emails_so_duplicados:
                        if email_item.get("id"):
                            marcar_como_lido(self.gmail, email_item["id"])
            self._etapa(cfg, "[yellow]⏭ ignorado (sem notícias)[/yellow]", ETAPAS_GRUPO)
            return ResultadoGrupo(nome, "ignorado", f"⏭️ {cfg.nome_final} ignorado (e-mail promocional/administrativo ou repetido sem novidades)",
                                  "Ignorado: sem notícias novas", time.time() - inicio)

        if resultados_futebol and "## RESULTADOS RECENTES" in resultados_futebol:
            final = sanitizar_markdown_final(inserir_apos_h1(corpo_editorial, resultados_futebol), consolidar=False)
        else:
            final = corpo_editorial

        self._etapa(cfg, "🔗 consolidando pautas do mesmo assunto", 2)
        final, info_consol = consolidar_secoes_mesmo_assunto_detalhado(final)
        if info_consol["secoes_concatenadas"] or info_consol["bullets_absorvidos"]:
            partes_log = []
            if info_consol["secoes_concatenadas"]:
                partes_log.append(f"{len(info_consol['secoes_concatenadas'])} seção(ões) concatenada(s)")
            if info_consol["bullets_absorvidos"]:
                partes_log.append(f"{len(info_consol['bullets_absorvidos'])} bullet(s) curto(s) absorvido(s)")
            logger.info(f"[cyan]🔗 Consolidação de pautas: {', '.join(partes_log)} (sem perda de conteúdo)[/cyan]")

        # Dupla verificação das regras de extração (corrige o que é seguro e relata o resto)
        self._etapa(cfg, "🔎 dupla verificação (regras e cobertura)", 3)
        logger.info("[cyan]🔎 Dupla verificação: conferindo estrutura, cobertura das pautas, idioma e enquete...[/cyan]")

        def notificar_verificacao(evento, regra, **dados):
            rotulo = escape(f"[{regra}]")
            if evento == "ia_inicio":
                self._etapa(cfg, dados.get("etapa") or f"🔧 IA · {escape(regra)}")
                logger.info(
                    f"[cyan]🔎 Dupla verificação {rotulo}:[/cyan] "
                    f"encontrado {escape(dados.get('encontrado', ''))} → {escape(dados.get('acao', 'corrigindo com IA...'))}"
                )
            elif evento == "corrigido":
                corr = escape(dados.get("corrigido", ""))
                if dados.get("usou_ia"):
                    logger.info(f"[cyan]🔧 Dupla verificação {rotulo}:[/cyan] corrigido — {corr}")
                else:
                    enc = escape(dados.get("encontrado", ""))
                    logger.info(f"[cyan]🔧 Dupla verificação {rotulo}:[/cyan] encontrado {enc} → corrigido: {corr}")
            elif evento == "aviso":
                enc = escape(dados.get("encontrado", ""))
                logger.warning(f"[yellow]⚠ Dupla verificação {rotulo}:[/yellow] encontrado {enc}")

        def gerar_enquete(conteudo, prompt):
            return self.extrator.extrair(conteudo, prompt)

        def reparar_com_ia(conteudo, prompt):
            return self.extrator.extrair(conteudo, prompt)

        foi_fatiado = bool(getattr(self._tls_extracao, "foi_fatiado", False))
        houve_downgrade = bool(getattr(self._tls_extracao, "houve_downgrade", False))
        revisar_estrutura = (
            self._extrair_tier_principal
            if (config.DUPLA_PASSAGEM_REVISAO and self.extrator and (foi_fatiado or houve_downgrade))
            else None
        )

        final, relatorio = aplicar_dupla_verificacao(
            final, "\n\n".join(entradas), idioma,
            gerar=gerar_enquete if self.extrator else None,
            extrair_data=extrair_data_de_texto,
            reparar=reparar_com_ia if self.extrator else None,
            notificar=notificar_verificacao,
            revisar_estrutura=revisar_estrutura,
            contexto_edicao_anterior=cfg.contexto_edicao_anterior,
        )
        final = consolidar_secoes_mesmo_assunto(final)
        final = aplicar_cabecalho_edicao(final, cfg.nome_final, idioma=idioma)
        console.print(renderizar_relatorio(relatorio, nome))
        log_arquivo.info(f"Dupla verificação · {nome} · {relatorio.resumo}\n{relatorio.como_texto()}")
        noticias = contar_noticias(final)
        kb = _formatar_kb(len(final))

        self._etapa(cfg, "💾 salvando Markdown", 4)
        self.pasta_saida.mkdir(parents=True, exist_ok=True)
        caminho = self.pasta_saida / cfg.nome_final
        caminho.write_text(final + "\n", encoding="utf-8")
        logger.info(f"[bold green]💾 Salvo:[/bold green] [bold]{escape(str(caminho))}[/bold] [dim]({len(final):,} caracteres · {kb})[/dim]")

        base = dict(
            segundos=0.0,
            noticias=noticias,
            verificacao=relatorio.resumo,
            tem_aviso=relatorio.tem_aviso,
            caracteres=len(final),
        )
        if self.opcoes.dry_run:
            logger.info("[dim]🧪 Dry-run: sem upload, sem marcar e-mails e sem atualizar controle do futebol[/dim]")
            self._etapa(cfg, f"[green]✔ concluído · {kb} (dry-run)[/green]", ETAPAS_GRUPO)
            base["segundos"] = time.time() - inicio
            return ResultadoGrupo(nome, "dry", f"🧪 {cfg.nome_final} gerado em modo dry-run", "Gerado (dry-run, sem Drive)", **base)

        if dedup_ativo and (itens_extraidos_ok or inicializado_agora):
            registrar_noticias_extraidas(
                cfg.chave,
                itens_extraidos_ok,
                itens_bootstrap=itens_bootstrap,
                marcar_inicializado=True,
                caminho_controle=self.caminho_controle_noticias,
            )

        if data_futebol:
            try:
                futebol.salvar_ultima_data(data_futebol)
            except Exception as e:
                logger.warning(f"[yellow]⚠ Erro ao salvar controle do futebol: {escape(str(e))}[/yellow]")

        self._etapa(cfg, "☁ enviando ao Drive e marcando e-mails", 5)
        registrar_fontes_processadas(emails_ok + emails_so_duplicados, cfg.chave, self.caminho_controle_fontes)
        if self.drive:
            upload_para_drive(self.drive, caminho, cfg.nome_final)
        if self.gmail:
            for email_item in emails_ok + emails_so_duplicados:
                if email_item.get("id"):
                    marcar_como_lido(self.gmail, email_item["id"])

        self._etapa(cfg, f"[green]✔ concluído · {kb}[/green]", ETAPAS_GRUPO)
        base["segundos"] = time.time() - inicio
        processados = len(emails_ok) + len(emails_so_duplicados)
        parcial = processados < n_emails
        if parcial:
            sufixo = f" ({processados}/{n_emails} e-mails; os demais continuam não lidos)"
            return ResultadoGrupo(nome, "parcial", f"⚠️ {cfg.nome_final} gerado com sucesso!{sufixo}",
                                  f"Gerado com {processados}/{n_emails} e-mails", **base)
        return ResultadoGrupo(nome, "ok", f"✅ {cfg.nome_final} gerado com sucesso!",
                              "Gerado e enviado ao Drive" if self.drive else "Gerado", **base)

    # -- execução paralela --
    def executar(self, configs):
        workers = max(1, min(self.opcoes.max_workers or config.MAX_WORKERS, len(configs)))
        self._nao_iniciados = len(configs)
        console.rule(f"[bold cyan]④ Extração com IA · {len(configs)} programa(s) · {workers} em paralelo[/bold cyan]")

        resultados = []
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("{task.description}"),
            BarColumn(bar_width=20, style="dim", complete_style="cyan", finished_style="green"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            self._progress = progress
            self._tarefas = {
                cfg.nome_final: progress.add_task(
                    f"[bold]{escape(Path(cfg.nome_final).stem)}[/bold] [dim]· ⏳ na fila[/dim]", total=ETAPAS_GRUPO
                )
                for cfg in configs
            }
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="grupo") as executor:
                futuros = {executor.submit(self.processar_grupo, cfg): cfg for cfg in configs}
                for futuro in concurrent.futures.as_completed(futuros):
                    cfg = futuros[futuro]
                    try:
                        resultados.append(futuro.result())
                    except Exception as e:
                        logger.exception(f"Erro crítico no grupo {cfg.titulo_base}")
                        self._etapa(cfg, "[red]✖ erro crítico[/red]", ETAPAS_GRUPO)
                        resultados.append(ResultadoGrupo(
                            Path(cfg.nome_final).stem, "erro", f"❌ Erro crítico no grupo {cfg.titulo_base}: {e}",
                            f"Erro crítico: {e}",
                        ))
            self._progress = None
        return resultados


# --- Apresentação no terminal ---------------------------------------------------

_ICONE_STATUS = {"ok": "✅", "parcial": "⚠️", "erro": "❌", "ignorado": "⏭️", "dry": "🧪"}


def _painel_configuracao(opcoes):
    grade = Table.grid(padding=(0, 2))
    grade.add_column(style="bold cyan", no_wrap=True)
    grade.add_column()
    web = not opcoes.urls and opcoes.verificar_web and config.VERIFICAR_FONTES_WEB
    origem = "URL(s) informada(s)" if opcoes.urls else "Gmail (e-mails não lidos)"
    grade.add_row("📥 Origem", origem + (" + fallback nos sites oficiais" if web else ""))
    ia = (opcoes.ia or config.IA_PADRAO).lower()
    grade.add_row("🤖 IA principal", ia)
    if ia != "perplexity":
        grade.add_row("🧠 Modelos (ordem)", " → ".join(config.GEMINI_MODELS))
        grade.add_row("🔑 Chaves Gemini", f"{len(config.GEMINI_API_KEYS)} (rotação automática em caso de cota 429)")
    grade.add_row("⚙  Paralelismo", f"até {opcoes.max_workers or config.MAX_WORKERS} programa(s) ao mesmo tempo")
    grade.add_row("✂  Fatiamento", f"partes de até {config.MAX_CHARS_POR_CHAMADA:,} caracteres (anti-504/413)")
    grade.add_row("🔎 Dupla verificação", "estrutura · 2ª passagem seletiva · integridade · tabelas · idioma · enquete")
    pasta = config.DRY_RUN_DIR if opcoes.dry_run else config.RESULTADOS_DIR
    destino = str(pasta) + ("  [yellow](dry-run: sem Drive e sem marcar e-mails)[/yellow]" if opcoes.dry_run else "  + Google Drive")
    grade.add_row("💾 Saída", destino)
    if opcoes.grupos:
        grade.add_row("🎯 Filtro", ", ".join(opcoes.grupos))
    titulo = "✨ Espelho Mágico" + (" [yellow][DRY-RUN][/yellow]" if opcoes.dry_run else "")
    return Panel(grade, title=titulo, title_align="left", border_style="cyan", expand=False)


def _formatar_kb(caracteres):
    """Formata contagem de caracteres/bytes aproximada em KB com vírgula decimal."""
    return f"{caracteres / 1024:.1f} KB".replace(".", ",")


def _resumo_datas_grupo(emails):
    """Formata as datas das edições de um grupo em ordem cronológica (ex.: '17/11 Noite, 18/11')."""
    if not emails:
        return "—"
    rotulos = []
    for item in emails:
        dt = None
        if item.get("data"):
            try:
                dt = email.utils.parsedate_to_datetime(item["data"])
            except Exception:
                dt = None
        assunto = (item.get("assunto") or "").lower()
        if dt is not None:
            sufixo = " Noite" if ("night" in assunto or "noite" in assunto or dt.hour >= 17) else ""
            rotulos.append(f"{dt.day:02d}/{dt.month:02d}{sufixo}")
        else:
            d = extrair_data_de_texto(item.get("assunto", ""))
            rotulos.append(f"{d.day:02d}/{d.month:02d}" if d else "—")
    vistos = list(dict.fromkeys(rotulos))
    return ", ".join(vistos[:3]) + ("…" if len(vistos) > 3 else "")


def _painel_standby(itens_standby):
    linhas = []
    for item in itens_standby:
        linhas.append(
            f"⏸ [bold]{escape(item.titulo_base)}[/bold]: chegou [cyan]{escape(item.fonte_recebida)}[/cyan] "
            f"({item.data_recebida:%d/%m}), aguardando [cyan]{escape(item.aguardando)}[/cyan] ({item.data_esperada:%d/%m}) "
            f"[dim]· libera sozinho em {item.expira_em:%d/%m} ou com --forcar-pares[/dim]"
        )
    return Panel("\n".join(linhas), title="⏸ Em stand by (aguardando par)", title_align="left", border_style="yellow", expand=False)


def _tabela_plano(configs):
    tabela = Table(
        title="🗂  Plano de extração",
        title_justify="left",
        title_style="bold cyan",
        box=box.ROUNDED,
        border_style="cyan",
        expand=False,
    )
    tabela.add_column("#", justify="right", style="dim")
    tabela.add_column("Programa", style="bold")
    tabela.add_column("Fonte")
    tabela.add_column("Edições · Datas")
    tabela.add_column("Itens", justify="center")
    tabela.add_column("Modo")
    tabela.add_column("Idioma", justify="center")
    for i, cfg in enumerate(configs, 1):
        da_web = any(not e.get("id") for e in cfg.emails)
        do_gmail = any(e.get("id") for e in cfg.emails)
        if da_web and do_gmail:
            fonte = "📧+🌐 Gmail + site"
        elif da_web:
            fonte = "🌐 site oficial"
        else:
            fonte = "📧 Gmail"
        chaves_f = cfg.chaves_fontes or [cfg.chave]
        if len(chaves_f) > 1:
            modo = f"🔗 par ({' + '.join(chaves_f)})"
        elif cfg.contexto_edicao_anterior:
            modo = "🧹 único (sem rep.)"
        else:
            modo = "🔗 unificado" if cfg.unificar else ("✂ separado" if len(cfg.emails) > 1 else "📄 único")
        idioma = "🇬🇧 EN" if (cfg.chave or "").lower() in config.PREFIXOS_INGLES else "🇧🇷 PT"
        tabela.add_row(
            str(i),
            escape(Path(cfg.nome_final).stem),
            fonte,
            _resumo_datas_grupo(cfg.emails),
            str(len(cfg.emails)),
            modo,
            idioma,
        )
    return tabela


def _tabela_resumo(resultados, total_segundos):
    tabela = Table(
        title="📊 Resumo do processamento",
        title_justify="left",
        title_style="bold cyan",
        box=box.ROUNDED,
        border_style="cyan",
        expand=False,
    )
    tabela.add_column("", justify="center")
    tabela.add_column("Programa", style="bold")
    tabela.add_column("Notícias", justify="center")
    tabela.add_column("Tamanho", justify="right")
    tabela.add_column("Dupla verificação")
    tabela.add_column("Tempo", justify="right")
    tabela.add_column("Resultado")
    total_noticias = 0
    total_chars = 0
    for r in sorted(resultados, key=lambda x: x.nome):
        verif = r.verificacao or "—"
        if r.verificacao:
            verif = f"[yellow]{verif}[/yellow]" if r.tem_aviso else f"[green]{verif}[/green]"
        cor = {"ok": "green", "dry": "green", "parcial": "yellow", "ignorado": "yellow", "erro": "red"}.get(r.status, "white")
        tempo = f"{int(r.segundos // 60)}m{int(r.segundos % 60):02d}s" if r.segundos else "—"
        tamanho = _formatar_kb(r.caracteres) if r.caracteres else "—"
        total_noticias += r.noticias or 0
        total_chars += r.caracteres or 0
        tabela.add_row(
            _ICONE_STATUS.get(r.status, "•"),
            escape(r.nome),
            str(r.noticias) if r.noticias else "—",
            tamanho,
            verif,
            tempo,
            f"[{cor}]{escape(r.resumo or r.mensagem)}[/{cor}]",
        )
    resumo_totais = f"⏱ Tempo total: {int(total_segundos // 60)}min {int(total_segundos % 60)}s · {len(resultados)} programa(s)"
    if total_noticias:
        resumo_totais += f" · {total_noticias} notícia(s)"
    if total_chars:
        resumo_totais += f" · {_formatar_kb(total_chars)}"
    tabela.caption = resumo_totais
    tabela.caption_justify = "left"
    return tabela


def executar(opcoes):
    inicio = time.time()
    console.print()
    console.print(_painel_configuracao(opcoes))

    if not config.GEMINI_API_KEYS and config.IA_PADRAO != "perplexity":
        logger.error("[bold red]Erro:[/bold red] Nenhuma GEMINI_API_KEY encontrada no .env.")
        return 1

    drive_service, gmail_service = None, None
    if not opcoes.dry_run or not opcoes.urls:
        try:
            drive_service, gmail_service = autenticar_google()
        except Exception as e:
            logger.error(f"[bold red]Falha na autenticação do Google: {escape(str(e))}[/bold red]")
            return 1

    if opcoes.urls:
        console.rule("[bold cyan]① Coleta · baixando edição(ões) pela URL[/bold cyan]")
        emails = []
        for u in opcoes.urls:
            try:
                emails.append(buscar_email_de_url(u, filtros_grupo=opcoes.grupos))
            except Exception as e:
                logger.error(f"[bold red]Falha ao baixar URL '{escape(u)}': {escape(str(e))}[/bold red]")
                return 1
    else:
        console.rule("[bold cyan]① Coleta · e-mails não lidos no Gmail[/bold cyan]")
        emails = buscar_emails(gmail_service)
        logger.info(f"[bold]📬 {len(emails)} e-mail(s) não lido(s) encontrado(s)[/bold]")
        if opcoes.verificar_web and config.VERIFICAR_FONTES_WEB:
            console.rule("[bold cyan]② Dupla verificação nos sites e feeds oficiais · edições e RSS[/bold cyan]")
            emails.extend(
                buscar_edicoes_faltantes_na_web(
                    emails,
                    config.RESULTADOS_DIR,
                    opcoes.grupos,
                    forcar_pares=opcoes.forcar_pares,
                )
            )
    if not emails:
        console.print(Panel("[yellow]Nada para processar agora: nenhum e-mail novo e todas as fontes em dia.[/yellow]",
                            border_style="yellow", expand=False))
        return 0

    try:
        extrator = Extrator(ia_padrao=opcoes.ia or None)
    except ValueError as e:
        logger.error(f"[bold red]Erro de configuração da IA:[/bold red] {escape(str(e))}")
        return 1
    pipeline = Pipeline(opcoes, extrator, drive_service, gmail_service)
    console.rule("[bold cyan]③ Planejamento · agrupando por programa e numerando edições[/bold cyan]")
    configs = pipeline.preparar_grupos(emails)
    if pipeline.em_standby:
        console.print()
        console.print(_painel_standby(pipeline.em_standby))
    if not configs:
        if not pipeline.em_standby:
            logger.info("[yellow]Nenhum grupo corresponde aos filtros informados.[/yellow]")
        return 0
    console.print()
    console.print(_tabela_plano(configs))

    resultados = pipeline.executar(configs)

    total = time.time() - inicio
    console.rule("[bold cyan]⑤ Resumo[/bold cyan]")
    console.print(_tabela_resumo(resultados, total))
    log_arquivo.info("Resumo: " + " | ".join(r.mensagem for r in resultados) + f" | tempo total {total:.0f}s")
    if any(r.tem_aviso for r in resultados):
        console.print("[yellow]⚠ Há avisos na dupla verificação: vale abrir o(s) arquivo(s) marcados em amarelo antes de gerar o podcast.[/yellow]")
    return 0 if all(r.status != "erro" for r in resultados) else 2


def interativo_disponivel():
    return sys.stdin.isatty()
