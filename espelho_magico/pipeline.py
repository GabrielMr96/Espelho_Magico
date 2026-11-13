"""Pipeline: agrupamento de e-mails, numeração de edições, extração, pós-processamento e publicação."""
import concurrent.futures
import datetime
import email.utils
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from rich import box
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import config, futebol
from .drive_client import upload_para_drive
from .gmail_client import autenticar_google, buscar_emails, marcar_como_lido
from .html_cleaner import limpar_linhas, preparar_conteudo
from .llm import (
    Extrator,
    FalhaExtracao,
    dividir_conteudo_com_metadados,
    unir_partes_extraidas,
    verificar_completude,
)
from .log import console, contexto_grupo, escape, log_arquivo, logger
from .validacao import aplicar_dupla_verificacao, contar_noticias, renderizar_relatorio

SEPARADOR = "\n\n---\n\n"
MINIMO_CARACTERES_EDICAO = 250
ETAPAS_GRUPO = 5  # pré-limpeza/IA · dupla verificação · salvar · Drive/Gmail · concluído

_INSTRUCAO_PARTE = (
    "<modo_parte>\n"
    "Este conteúdo é a PARTE {i} de {n} de uma mesma edição, fatiada por tamanho para caber no limite da IA. "
    "Extraia normalmente TODAS as notícias desta parte seguindo as regras acima, mas NÃO invente "
    "`## Enquete do Dia` / `## Daily Poll`: inclua enquete apenas se ela existir de fato nesta parte da fonte "
    "(a enquete final é garantida depois, na dupla verificação).\n"
    "</modo_parte>"
)

_MESES_PT = (
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)
_MESES_EN = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


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


@dataclass
class ConfigGrupo:
    chave: str
    emails: list
    titulo_base: str
    nome_final: str
    unificar: bool = False


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

    def __str__(self):
        return self.mensagem


# --- Funções puras (testáveis) ------------------------------------------------

def carregar_prompt(nome):
    return (config.PROMPTS_DIR / f"{nome}.md").read_text(encoding="utf-8").strip()


def agrupar_emails(emails, mapeamento):
    """Agrupa por prefixo conhecido (no remetente ou no início do assunto); os demais ficam um por grupo."""
    grupos = {}
    for email_item in emails:
        assunto = email_item["assunto"].lower()
        remetente = email_item.get("remetente", "").lower()
        chave = next((p for p in mapeamento if p in remetente or assunto.startswith(p)), None)
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
    if "the news sports" in html_low and "tns sports" in mapeamento:
        return "tns sports"
    if "aidrops" in url_low and "aidrops" in mapeamento:
        return "aidrops"
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
    }


def buscar_email_de_url(url, filtros_grupo=None, mapeamento=None, get=requests.get, data_forcada=None):
    """Baixa uma edição online via URL e monta um dicionário compatível com `buscar_emails`."""
    logger.info(f"[cyan]➜ Baixando edição a partir da URL: {escape(url)}[/cyan]")
    resposta = get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resposta.raise_for_status()
    return _montar_item_de_html(url, resposta.text, filtros_grupo, mapeamento, data_forcada=data_forcada)


def _verificar_fonte_thenews(chave, titulo_base, arquivo_url, ultima_data, get=requests.get):
    """Verifica na página de arquivo do the news (`thenews.com.br/newsletter/<marca>`) se há edição mais recente."""
    slug_marca = arquivo_url.rstrip("/").split("/")[-1]
    prefixo_href = f"/newsletter/{slug_marca}/"
    resp = get(arquivo_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
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
        if ultima_data and dt <= ultima_data:
            continue
        if not ultima_data and (datetime.date.today() - dt).days > 2:
            continue
        url_edicao = urllib.parse.urljoin(arquivo_url, href)
        candidatos.append((dt, url_edicao))
        if len(candidatos) >= 3:
            break

    candidatos.sort(key=lambda par: par[0])
    itens = []
    for dt, url_edicao in candidatos:
        logger.info(
            f"[bold cyan]🌐 [Web] Edição de {dt:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
            f"(última local: {ultima_data.strftime('%d/%m/%Y') if ultima_data else 'nenhuma'}). Baixando...[/bold cyan]"
        )
        itens.append(buscar_email_de_url(url_edicao, filtros_grupo=[chave], get=get, data_forcada=dt))
    return itens


def _verificar_fonte_beehiiv(chave, titulo_base, arquivo_url, ultima_data, get=requests.get):
    """Verifica arquivo Beehiiv (`aidrops.com.br/archive`, `thejobs.com.br/archive`) por edição mais nova."""
    resp = get(arquivo_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    link = next((a for a in soup.find_all("a", href=True) if "/p/" in a["href"]), None)
    if not link:
        return []
    url_post = urllib.parse.urljoin(arquivo_url, link["href"].strip())
    dt_card = extrair_data_de_texto(link.get_text(" ", strip=True))
    if dt_card is not None:
        if ultima_data and dt_card <= ultima_data:
            return []
        if not ultima_data and (datetime.date.today() - dt_card).days > 2:
            return []
        logger.info(
            f"[bold cyan]🌐 [Web] Edição de {dt_card:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
            f"(última local: {ultima_data.strftime('%d/%m/%Y') if ultima_data else 'nenhuma'}). Baixando...[/bold cyan]"
        )
        return [buscar_email_de_url(url_post, filtros_grupo=[chave], get=get, data_forcada=dt_card)]

    # Quando o card de destaque não exibe a data na vitrine (ex.: AiDrops), inspeciona o cabeçalho do post
    resp_post = get(url_post, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    resp_post.raise_for_status()
    soup_post = BeautifulSoup(resp_post.text, "html.parser")
    texto_topo = soup_post.get_text(" ", strip=True)[:1500]
    dt_post = extrair_data_de_texto(texto_topo)
    if not dt_post:
        return []
    if ultima_data and dt_post <= ultima_data:
        return []
    if not ultima_data and (datetime.date.today() - dt_post).days > 2:
        return []
    logger.info(
        f"[bold cyan]🌐 [Web] Edição de {dt_post:%d/%m/%Y} encontrada no site para '{escape(titulo_base)}' "
        f"(última local: {ultima_data.strftime('%d/%m/%Y') if ultima_data else 'nenhuma'}). Baixando...[/bold cyan]"
    )
    return [_montar_item_de_html(url_post, resp_post.text, filtros_grupo=[chave], data_forcada=dt_post)]


def _verificar_fonte_thepeak(chave, titulo_base, arquivo_url, ultima_data, get=requests.get):
    """Verifica `readthepeak.com/newsletters/morning-brief` e reúne os artigos públicos da edição se faltar."""
    resp = get(arquivo_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
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


def buscar_edicoes_faltantes_na_web(
    emails_atuais, pasta_resultados=None, filtros_grupo=None, fontes_web=None, mapeamento=None, get=requests.get
):
    """Dupla verificação: para cada fonte externa sem e-mail não lido hoje, checa no site oficial
    se saiu edição com data mais recente que o último `.md` salvo em `pasta_resultados`.
    """
    pasta_resultados = pasta_resultados or config.RESULTADOS_DIR
    fontes_web = fontes_web if fontes_web is not None else config.FONTES_WEB
    mapeamento = mapeamento or config.MAPEAMENTO_PREFIXOS
    if not fontes_web:
        return []

    grupos_presentes = set(agrupar_emails(emails_atuais, mapeamento).keys())
    titulos_presentes = {mapeamento.get(c, c) for c in grupos_presentes}
    logger.info("[cyan]🔎 [Web] Verificando nos sites oficiais se alguma edição de hoje não chegou por e-mail...[/cyan]")

    recuperados = []
    for chave, cfg_web in fontes_web.items():
        titulo_base = mapeamento.get(chave, chave)
        if filtros_grupo and not any(
            f.lower() in chave.lower() or f.lower() in titulo_base.lower() for f in filtros_grupo
        ):
            continue
        if chave in grupos_presentes or titulo_base in titulos_presentes:
            continue

        ultima_data = ultima_data_extraida_local(pasta_resultados, titulo_base)
        tipo = (cfg_web.get("tipo") or "").lower()
        arquivo_url = cfg_web.get("arquivo_url") or ""
        if not arquivo_url:
            continue

        try:
            if tipo == "thenews":
                novos = _verificar_fonte_thenews(chave, titulo_base, arquivo_url, ultima_data, get=get)
            elif tipo == "beehiiv":
                novos = _verificar_fonte_beehiiv(chave, titulo_base, arquivo_url, ultima_data, get=get)
            elif tipo == "thepeak":
                novos = _verificar_fonte_thepeak(chave, titulo_base, arquivo_url, ultima_data, get=get)
            else:
                novos = []
        except Exception as e:
            logger.warning(f"[yellow]  ⚠ [Web] Não foi possível verificar '{escape(titulo_base)}' ({escape(arquivo_url)}): {escape(str(e))}[/yellow]")
            continue

        if novos:
            recuperados.extend(novos)
            titulos_presentes.add(titulo_base)

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
_RE_APROFUNDE = re.compile(r"\s*\(?(?:aprofund[ea]|leia mais|saiba mais)\)?", re.I)
_RE_TWITTER_LIXO = re.compile(r"(?:https?://t\.co/\S+|pic\.twitter\.com/\S+)")
_RE_TITULO_PONTUADO = re.compile(r"^(#{1,6}\s+.*?)[.?!:]\s*$")
_RE_CERCA_EXTERNA = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n\s*```\s*$", re.S | re.I)


def deduplicar_secoes(texto):
    """Remove seções com título repetido sem perder conteúdo.

    - Títulos de nível 2 (##) são deduplicados no escopo global do documento.
    - Subtítulos de nível 3+ (###) são deduplicados apenas dentro do mesmo ## pai,
      impedindo que subseções genéricas (ex.: '### Resumo') de matérias diferentes se fundam.
    - Mesmo título e mesmo corpo: a seção repetida é descartada.
    - Mesmo título e corpo diferente: o corpo é anexado à primeira ocorrência
      (antes, só o título era apagado e o corpo ficava "colado" na seção anterior).
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
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


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


def aplicar_cabecalho_edicao(texto, nome_final):
    """Inclui o nome do programa e o número da edição no cabeçalho H1 do topo para o NotebookLM:
    Ex.: '# 10 de novembro de 2026' + 'Controle Quebrado #274.md'
      -> '# Controle Quebrado #274 — 10 de novembro de 2026'
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
        if h1_atual.lower().startswith(identificador.lower()):
            return texto_limpo
        return f"# {identificador} — {h1_atual}{resto}"
    return f"# {identificador}\n\n{texto_limpo}"


def sanitizar_markdown_final(texto):
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

    linhas = texto.splitlines()
    linhas_processadas = []
    viu_h1 = False

    for linha in linhas:
        l_strip = linha.strip()

        # Remove imagens ou linhas que sejam apenas créditos de fotógrafo/agência residuais
        if _RE_CREDITO_RESIDUAL.match(l_strip) or _RE_LINHA_CREDITO_SOLTO.match(l_strip):
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
    limpo = deduplicar_secoes(limpo)
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
        self._lock_pendentes = threading.Lock()
        self._nao_iniciados = 0
        self._progress = None
        self._tarefas = {}

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

    def preparar_grupos(self, emails):
        grupos = agrupar_emails(emails, config.MAPEAMENTO_PREFIXOS)
        configs = []
        for chave, emails_grupo in grupos.items():
            if chave in config.MAPEAMENTO_PREFIXOS:
                titulo_base = config.MAPEAMENTO_PREFIXOS[chave]
            else:
                titulo_base, nome_final = nome_fallback(chave)

            if self.opcoes.grupos and not any(
                f.lower() in chave or f.lower() in titulo_base.lower() for f in self.opcoes.grupos
            ):
                continue

            if chave in config.MAPEAMENTO_PREFIXOS:
                logger.info(f"\n[bold magenta]Encontrados {len(emails_grupo)} e-mail(s) para: {escape(titulo_base)}[/bold magenta]")
                maior = maior_edicao_existente(config.RESULTADOS_DIR, titulo_base)
                if maior > 0:
                    numero = str(maior + 1)
                    logger.info(f"[bold green]▶ Número da edição automatizado: {numero} (última encontrada: {maior})[/bold green]")
                else:
                    numero = self._perguntar(f"▶ Qual o número da edição para '{titulo_base}'? ").strip()
                    if not numero:
                        numero = time.strftime("%Y%m%d")
                        logger.info(f"[yellow]▶ Sem número informado. Usando a data: {numero}[/yellow]")
                nome_final = f"{titulo_base} #{numero}.md"

            unificar = False
            if len(emails_grupo) >= 2:
                if self.opcoes.unificar == "sim":
                    unificar = True
                elif self.opcoes.unificar == "perguntar" and self.opcoes.interativo:
                    resposta = self._perguntar(
                        f"▶ {len(emails_grupo)} e-mails para '{titulo_base}'. Unificar de forma inteligente "
                        f"em um único texto (mesclando assuntos e evitando repetições)? (S/N): "
                    )
                    unificar = resposta.strip().lower() == "s"

            configs.append(ConfigGrupo(chave, emails_grupo, titulo_base, nome_final, unificar))
        return configs

    # -- extração --
    def _montar_prompt(self, chave, unificar=False):
        partes = [self.prompt_base]
        dica = config.DICAS_POR_PREFIXO.get((chave or "").lower())
        if dica:
            partes.append(f"<diretrizes_especificas_desta_fonte>\n{dica}\n</diretrizes_especificas_desta_fonte>")
        if unificar:
            partes.append(self.prompt_unificacao)
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

    def _extrair(self, conteudo, prompt, rotulo, cfg=None):
        partes = dividir_conteudo_com_metadados(conteudo, max_chars=config.MAX_CHARS_POR_CHAMADA)
        if len(partes) > 1:
            logger.info(
                f"[cyan]✂ Conteúdo extenso ({len(conteudo):,} caracteres): dividido em {len(partes)} partes "
                f"(~{len(conteudo) // len(partes):,} cada) para evitar timeout (504) e limite de tokens (413)[/cyan]"
            )
            extraidas = []
            for idx, parte in enumerate(partes, 1):
                rotulo_parte = f"{rotulo} [parte {idx}/{len(partes)}]"
                self._etapa(cfg, f"🤖 extraindo com IA · parte {idx}/{len(partes)}")
                logger.info(f"[cyan]🤖 Parte {idx}/{len(partes)} → IA[/cyan]")
                prompt_parte = f"{prompt}\n\n{_INSTRUCAO_PARTE.format(i=idx, n=len(partes))}"
                texto_parte = self._extrair_unico(parte, prompt_parte, rotulo_parte)
                if not texto_parte:
                    return None
                extraidas.append(texto_parte)
                if idx < len(partes):
                    time.sleep(2)
            return unir_partes_extraidas(extraidas)
        return self._extrair_unico(conteudo, prompt, rotulo)

    def _extrair_unico(self, conteudo, prompt, rotulo):
        try:
            texto = self.extrator.extrair(conteudo, prompt)
        except FalhaExtracao as e:
            logger.error(f"[bold red]✖ {escape(str(e))} ({escape(rotulo)})[/bold red]")
            return None
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
        return texto_limpo

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
        textos, emails_ok, entradas = [], [], []
        if cfg.unificar:
            partes_edicoes = "".join(
                f"\n\n--- INÍCIO DA EDIÇÃO {i + 1} ---\n\n{self._preparar(e, idioma=idioma, envolver_documento=False)}"
                for i, e in enumerate(cfg.emails)
            ).strip()
            combinado = f"<documento_entrada>\n{partes_edicoes}\n</documento_entrada>"
            entradas.append(combinado)
            logger.info(f"[cyan]🤖 Enviando {n_emails} e-mails agrupados para extração unificada...[/cyan]")
            self._etapa(cfg, "🤖 extraindo com IA (unificado)", 1)
            prompt_grupo = self._montar_prompt(cfg.chave, unificar=True)
            texto = self._extrair(combinado, prompt_grupo, cfg.titulo_base, cfg)
            if texto:
                textos.append(texto)
                emails_ok.extend(cfg.emails)
        else:
            prompt_grupo = self._montar_prompt(cfg.chave, unificar=False)
            for i, email_item in enumerate(cfg.emails):
                conteudo = self._preparar(email_item, idioma=idioma)
                entradas.append(conteudo)
                sufixo = f" · e-mail {i + 1}/{n_emails}" if n_emails > 1 else ""
                self._etapa(cfg, f"🤖 extraindo com IA{sufixo}", 1)
                texto = self._extrair(conteudo, prompt_grupo, email_item["assunto"], cfg)
                if texto:
                    textos.append(texto)
                    emails_ok.append(email_item)
                if i < n_emails - 1:
                    time.sleep(2)

        if not textos:
            logger.error(f"[bold red]✖ Falha ao gerar texto para {escape(cfg.nome_final)}[/bold red]")
            self._etapa(cfg, "[red]✖ falha na IA[/red]", ETAPAS_GRUPO)
            return ResultadoGrupo(nome, "erro", f"❌ Erro ao gerar {cfg.nome_final}", "Falha na IA (e-mails continuam não lidos)",
                                  time.time() - inicio)

        corpo_editorial = sanitizar_markdown_final(SEPARADOR.join(textos))
        if eh_extracao_vazia_ou_invalida(corpo_editorial):
            logger.warning(
                f"[bold yellow]⚠ E-mail(s) sem conteúdo editorial válido ({len(corpo_editorial)} caracteres). "
                f"Edição descartada para não gerar arquivo vazio.[/bold yellow]"
            )
            if not self.opcoes.dry_run and self.gmail:
                for email_item in emails_ok:
                    if email_item.get("id"):
                        marcar_como_lido(self.gmail, email_item["id"])
            self._etapa(cfg, "[yellow]⏭ ignorado (sem notícias)[/yellow]", ETAPAS_GRUPO)
            return ResultadoGrupo(nome, "ignorado", f"⏭️ {cfg.nome_final} ignorado (e-mail promocional/administrativo sem notícias)",
                                  "Ignorado: e-mail sem notícias", time.time() - inicio)

        if resultados_futebol:
            final = sanitizar_markdown_final(inserir_apos_h1(corpo_editorial, resultados_futebol))
        else:
            final = corpo_editorial

        # Dupla verificação das regras de extração (corrige o que é seguro e relata o resto)
        self._etapa(cfg, "🔎 dupla verificação das regras", 2)
        logger.info("[cyan]🔎 Dupla verificação: conferindo estrutura, integridade, idioma e enquete...[/cyan]")

        def gerar_enquete(conteudo, prompt):
            logger.info("[cyan]🗳 Fonte sem Enquete do Dia: pedindo à IA uma enquete sobre a pauta...[/cyan]")
            self._etapa(cfg, "🗳 criando Enquete do Dia com IA")
            return self.extrator.extrair(conteudo, prompt)

        def reparar_com_ia(conteudo, prompt):
            logger.info("[cyan]🔧 Dupla verificação: corrigindo desvios automaticamente com IA...[/cyan]")
            self._etapa(cfg, "🔧 corrigindo desvios com IA")
            return self.extrator.extrair(conteudo, prompt)

        final, relatorio = aplicar_dupla_verificacao(
            final, "\n\n".join(entradas), idioma,
            gerar=gerar_enquete if self.extrator else None,
            extrair_data=extrair_data_de_texto,
            reparar=reparar_com_ia if self.extrator else None,
        )
        final = aplicar_cabecalho_edicao(final, cfg.nome_final)
        console.print(renderizar_relatorio(relatorio, nome))
        log_arquivo.info(f"Dupla verificação · {nome} · {relatorio.resumo}\n{relatorio.como_texto()}")
        noticias = contar_noticias(final)

        self._etapa(cfg, "💾 salvando Markdown", 3)
        self.pasta_saida.mkdir(parents=True, exist_ok=True)
        caminho = self.pasta_saida / cfg.nome_final
        caminho.write_text(final + "\n", encoding="utf-8")
        logger.info(f"[bold green]💾 Salvo:[/bold green] [bold]{escape(str(caminho))}[/bold] [dim]({len(final):,} caracteres)[/dim]")

        base = dict(segundos=0.0, noticias=noticias, verificacao=relatorio.resumo, tem_aviso=relatorio.tem_aviso)
        if self.opcoes.dry_run:
            logger.info("[dim]🧪 Dry-run: sem upload, sem marcar e-mails e sem atualizar controle do futebol[/dim]")
            self._etapa(cfg, "[green]✔ concluído (dry-run)[/green]", ETAPAS_GRUPO)
            base["segundos"] = time.time() - inicio
            return ResultadoGrupo(nome, "dry", f"🧪 {cfg.nome_final} gerado em modo dry-run", "Gerado (dry-run, sem Drive)", **base)

        if data_futebol:
            try:
                futebol.salvar_ultima_data(data_futebol)
            except Exception as e:
                logger.warning(f"[yellow]⚠ Erro ao salvar controle do futebol: {escape(str(e))}[/yellow]")

        self._etapa(cfg, "☁ enviando ao Drive e marcando e-mails", 4)
        if self.drive:
            upload_para_drive(self.drive, caminho, cfg.nome_final)
        if self.gmail:
            for email_item in emails_ok:
                if email_item.get("id"):
                    marcar_como_lido(self.gmail, email_item["id"])

        self._etapa(cfg, "[green]✔ concluído[/green]", ETAPAS_GRUPO)
        base["segundos"] = time.time() - inicio
        parcial = len(emails_ok) < n_emails
        if parcial:
            sufixo = f" ({len(emails_ok)}/{n_emails} e-mails; os demais continuam não lidos)"
            return ResultadoGrupo(nome, "parcial", f"⚠️ {cfg.nome_final} gerado com sucesso!{sufixo}",
                                  f"Gerado com {len(emails_ok)}/{n_emails} e-mails", **base)
        return ResultadoGrupo(nome, "ok", f"✅ {cfg.nome_final} gerado com sucesso!",
                              "Gerado e enviado ao Drive" if self.drive else "Gerado", **base)

    # -- execução paralela --
    def executar(self, configs):
        workers = max(1, min(self.opcoes.max_workers or config.MAX_WORKERS, len(configs)))
        self._nao_iniciados = len(configs)
        console.rule(f"[bold cyan]④ Extração com IA · {len(configs)} programa(s) · {workers} em paralelo[/bold cyan]")

        resultados = []
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=18),
            TaskProgressColumn(), TimeElapsedColumn(), console=console,
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
    grade.add_row("🔎 Dupla verificação", "estrutura · integridade dos números · tabelas · idioma · enquete")
    pasta = config.DRY_RUN_DIR if opcoes.dry_run else config.RESULTADOS_DIR
    destino = str(pasta) + ("  [yellow](dry-run: sem Drive e sem marcar e-mails)[/yellow]" if opcoes.dry_run else "  + Google Drive")
    grade.add_row("💾 Saída", destino)
    if opcoes.grupos:
        grade.add_row("🎯 Filtro", ", ".join(opcoes.grupos))
    titulo = "✨ Espelho Mágico" + (" [yellow][DRY-RUN][/yellow]" if opcoes.dry_run else "")
    return Panel(grade, title=titulo, title_align="left", border_style="cyan", expand=False)


def _tabela_plano(configs):
    tabela = Table(title="🗂  Programas a processar", title_justify="left", title_style="bold",
                   box=box.SIMPLE_HEAVY, expand=False)
    tabela.add_column("#", justify="right", style="dim")
    tabela.add_column("Programa", style="bold")
    tabela.add_column("Fonte")
    tabela.add_column("Itens", justify="center")
    tabela.add_column("Modo")
    tabela.add_column("Idioma", justify="center")
    for i, cfg in enumerate(configs, 1):
        da_web = any(not e.get("id") for e in cfg.emails)
        fonte = "🌐 site oficial" if da_web else "📧 Gmail"
        modo = "unificado" if cfg.unificar else ("separado" if len(cfg.emails) > 1 else "único")
        idioma = "🇬🇧 EN" if (cfg.chave or "").lower() in config.PREFIXOS_INGLES else "🇧🇷 PT"
        tabela.add_row(str(i), escape(Path(cfg.nome_final).stem), fonte, str(len(cfg.emails)), modo, idioma)
    return tabela


def _tabela_resumo(resultados, total_segundos):
    tabela = Table(title="📊 Resumo do processamento", title_justify="left", title_style="bold cyan",
                   box=box.ROUNDED, expand=False)
    tabela.add_column("", justify="center")
    tabela.add_column("Programa", style="bold")
    tabela.add_column("Notícias", justify="center")
    tabela.add_column("Dupla verificação")
    tabela.add_column("Tempo", justify="right")
    tabela.add_column("Resultado")
    for r in sorted(resultados, key=lambda x: x.nome):
        verif = r.verificacao or "—"
        if r.verificacao:
            verif = f"[yellow]{verif}[/yellow]" if r.tem_aviso else f"[green]{verif}[/green]"
        cor = {"ok": "green", "dry": "green", "parcial": "yellow", "ignorado": "yellow", "erro": "red"}.get(r.status, "white")
        tempo = f"{int(r.segundos // 60)}m{int(r.segundos % 60):02d}s" if r.segundos else "—"
        tabela.add_row(
            _ICONE_STATUS.get(r.status, "•"), escape(r.nome), str(r.noticias) if r.noticias else "—",
            verif, tempo, f"[{cor}]{escape(r.resumo or r.mensagem)}[/{cor}]",
        )
    tabela.caption = f"⏱ Tempo total: {int(total_segundos // 60)}min {int(total_segundos % 60)}s"
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
            console.rule("[bold cyan]② Dupla verificação nos sites oficiais · edições que não chegaram por e-mail[/bold cyan]")
            emails.extend(buscar_edicoes_faltantes_na_web(emails, config.RESULTADOS_DIR, opcoes.grupos))
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
    if not configs:
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
