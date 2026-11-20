"""Deduplicação de notícias repetidas (título + data/hora) contra extrações anteriores.

Aplicada antes da extração com IA para os programas configurados em
`config.PREFIXOS_DEDUPLICAR_NOTICIAS` (Hackeando o Problema, Pipoca Vencida e
Controle Quebrado), evitando reextrair notícias idênticas reenviadas pelos feeds.
"""
import datetime
import email.utils
import json
import os
import re
import threading
from pathlib import Path

from bs4 import BeautifulSoup

from . import config
from .gmail_client import buscar_emails_anteriores
from .log import escape, logger

MAX_HISTORICO_POR_PROGRAMA = 500
_FUSO_BRT = datetime.timezone(datetime.timedelta(hours=-3))
_CONTROLE_LOCK = threading.Lock()

_RE_DATA_HORA_NOTICIA = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*(?:[àa]s|[-–,])?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$",
    re.I,
)
_RE_H2_MD = re.compile(r"^##\s+(.+?)\s*$")


def deve_deduplicar_noticias(chave_grupo):
    """Retorna True se o grupo está habilitado para remoção de notícias repetidas."""
    return (chave_grupo or "").strip().lower() in config.PREFIXOS_DEDUPLICAR_NOTICIAS


def _limpar_texto(texto):
    limpo = (texto or "").replace("\xa0", " ").replace("\u200b", "").replace("\u200c", "")
    return " ".join(limpo.split())


def normalizar_data_hora(data_hora):
    """Normaliza 'DD/MM/YYYY às HH:MM' para 'DD/MM/YYYY HH:MM' (ou string limpa se outro formato)."""
    limpo = _limpar_texto(data_hora)
    m = _RE_DATA_HORA_NOTICIA.match(limpo)
    if not m:
        return limpo.casefold()
    dia, mes, ano, hora, minuto = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4)), m.group(5)
    return f"{dia:02d}/{mes:02d}/{ano} {hora:02d}:{minuto}"


def normalizar_chave_noticia(titulo, data_hora):
    """Chave exata por título + data/hora para identificar notícias repetidas (não apenas parecidas)."""
    t_norm = _limpar_texto(titulo).casefold()
    dh_norm = normalizar_data_hora(data_hora)
    return f"{t_norm}|{dh_norm}"


def _container_card_html(h2, h2s_datados):
    """Devolve o <div>/<article> individual do card da notícia, evitando contêineres com múltiplos cards."""
    atual = h2.parent
    if not atual or atual.name not in ("div", "article", "section", "li"):
        return None
    if atual.find("h1") is not None:
        return None
    if any(outro is not h2 and outro in atual.descendants for outro in h2s_datados):
        return None
    return atual


def _mapear_cards_html(soup):
    """Localiza pares (card_tag, titulo, data_hora) no HTML dos feeds de notícias."""
    candidatos = []
    for h2 in soup.find_all("h2"):
        p_dt = h2.find_next_sibling("p")
        if not p_dt:
            continue
        dt_txt = _limpar_texto(p_dt.get_text())
        if not _RE_DATA_HORA_NOTICIA.match(dt_txt):
            continue
        titulo = _limpar_texto(h2.get_text())
        if titulo:
            candidatos.append((h2, titulo, dt_txt))

    h2s_datados = [h2 for h2, _, _ in candidatos]
    cards = []
    for h2, titulo, dt_txt in candidatos:
        container = _container_card_html(h2, h2s_datados)
        if container is not None:
            cards.append((container, titulo, dt_txt))
    return cards


def _extrair_noticias_markdown(texto):
    """Encontra blocos de notícias em Markdown cujo `## Título` é seguido por `DD/MM/YYYY às HH:MM`."""
    linhas = (texto or "").splitlines()
    inicios = []
    for i, linha in enumerate(linhas):
        m = _RE_H2_MD.match(linha.strip())
        if not m:
            continue
        j = i + 1
        while j < len(linhas) and not linhas[j].strip():
            j += 1
        if j < len(linhas):
            dt_txt = _limpar_texto(linhas[j])
            if _RE_DATA_HORA_NOTICIA.match(dt_txt):
                titulo = _limpar_texto(m.group(1))
                if titulo:
                    inicios.append((i, titulo, dt_txt))
    return linhas, inicios


def extrair_noticias_email(email_item):
    """Extrai a lista de dicts `{titulo, data_hora, chave}` contida em um e-mail."""
    conteudo = (email_item or {}).get("conteudo_html") or ""
    if not conteudo:
        return []
    if email_item.get("eh_html", True):
        soup = BeautifulSoup(conteudo, "html.parser")
        cards = _mapear_cards_html(soup)
        if cards:
            return [
                {"titulo": titulo, "data_hora": dt_txt, "chave": normalizar_chave_noticia(titulo, dt_txt)}
                for _, titulo, dt_txt in cards
            ]

    _, inicios = _extrair_noticias_markdown(conteudo)
    return [
        {"titulo": titulo, "data_hora": dt_txt, "chave": normalizar_chave_noticia(titulo, dt_txt)}
        for _, titulo, dt_txt in inicios
    ]


def filtrar_noticias_repetidas_html(html, chaves_vistas):
    """Remove cards de notícias cujo (título, data/hora) já está em `chaves_vistas`.

    Atualiza `chaves_vistas` com as notícias mantidas para evitar duplicatas no mesmo lote.
    Retorna `(novo_html, removidas, mantidas, total_cards)`.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    cards = _mapear_cards_html(soup)
    if not cards:
        return html, [], [], 0

    removidas, mantidas = [], []
    for container, titulo, dt_txt in cards:
        chave = normalizar_chave_noticia(titulo, dt_txt)
        if chave in chaves_vistas:
            removidas.append({"titulo": titulo, "data_hora": dt_txt, "chave": chave})
            container.decompose()
        else:
            chaves_vistas.add(chave)
            mantidas.append({"titulo": titulo, "data_hora": dt_txt, "chave": chave})
    return str(soup), removidas, mantidas, len(cards)


def filtrar_noticias_repetidas_markdown(texto, chaves_vistas):
    """Fallback em Markdown: remove blocos `## Título` + `DD/MM/YYYY às HH:MM` já presentes em `chaves_vistas`."""
    linhas, inicios = _extrair_noticias_markdown(texto)
    if not inicios:
        return texto, [], [], 0

    removidas, mantidas = [], []
    partes = []
    primeiro_ini = inicios[0][0]
    if primeiro_ini > 0:
        partes.extend(linhas[:primeiro_ini])

    for idx, (pos_ini, titulo, dt_txt) in enumerate(inicios):
        pos_fim = inicios[idx + 1][0] if idx + 1 < len(inicios) else len(linhas)
        chave = normalizar_chave_noticia(titulo, dt_txt)
        if chave in chaves_vistas:
            removidas.append({"titulo": titulo, "data_hora": dt_txt, "chave": chave})
        else:
            chaves_vistas.add(chave)
            mantidas.append({"titulo": titulo, "data_hora": dt_txt, "chave": chave})
            partes.extend(linhas[pos_ini:pos_fim])

    novo_texto = re.sub(r"\n{3,}", "\n\n", "\n".join(partes)).strip()
    return novo_texto, removidas, mantidas, len(inicios)


def filtrar_email_noticias_repetidas(email_item, chaves_vistas):
    """Filtra notícias repetidas de `email_item` sem alterar o dict original.

    Retorna `(email_filtrado, removidas, mantidas, total_cards)`.
    """
    conteudo = email_item.get("conteudo_html") or ""
    if not conteudo:
        return dict(email_item), [], [], 0

    eh_html = email_item.get("eh_html", True)
    if eh_html:
        novo_html, removidas, mantidas, total = filtrar_noticias_repetidas_html(conteudo, chaves_vistas)
        if total > 0:
            return {**email_item, "conteudo_html": novo_html}, removidas, mantidas, total

    novo_md, removidas, mantidas, total = filtrar_noticias_repetidas_markdown(conteudo, chaves_vistas)
    if total > 0:
        return {**email_item, "conteudo_html": novo_md}, removidas, mantidas, total
    return dict(email_item), [], [], 0


# --- Persistência e bootstrap de histórico -----------------------------------

def _ler_arquivo_controle(caminho):
    if not caminho or not Path(caminho).exists():
        return {}
    try:
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception as e:
        logger.warning(f"[yellow]⚠ Erro ao ler histórico de notícias ({escape(str(caminho))}): {escape(str(e))}[/yellow]")
        return {}


def _escrever_arquivo_controle(caminho, dados):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


def _data_brt_email(email_item):
    raw = (email_item or {}).get("data") or ""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_FUSO_BRT).date()
    except Exception:
        return None


def _bootstrap_historico_gmail(chave_grupo, titulo_base, gmail_service, ids_excluir=None, pasta_resultados=None, ultima_data=None):
    """Coleta (título, data/hora) das extrações anteriores já lidas no Gmail quando o cache ainda não foi iniciado."""
    if not gmail_service:
        return []
    if ultima_data is None and pasta_resultados:
        from .pipeline import ultima_data_extraida_local
        ultima_data = ultima_data_extraida_local(pasta_resultados, titulo_base)
    if ultima_data is None:
        return []

    ids_excluir = {i for i in (ids_excluir or []) if i}
    anteriores = buscar_emails_anteriores(gmail_service, chave_grupo)
    itens = []
    vistos = set()
    for email_ant in anteriores:
        if email_ant.get("id") in ids_excluir:
            continue
        dt_email = _data_brt_email(email_ant)
        if dt_email is not None and dt_email > ultima_data:
            continue
        for item in extrair_noticias_email(email_ant):
            if item["chave"] not in vistos:
                vistos.add(item["chave"])
                itens.append(item)
    return itens


def carregar_historico_noticias(
    chave_grupo,
    titulo_base,
    gmail_service=None,
    ids_excluir=None,
    caminho_controle=None,
    pasta_resultados=None,
    ultima_data=None,
):
    """Carrega o conjunto de chaves já extraídas para `chave_grupo`.

    Se o programa ainda não foi inicializado no controle local e `gmail_service` estiver
    disponível, carrega automaticamente as edições recentes já lidas do Gmail.
    Retorna `(chaves_vistas, itens_bootstrap, inicializado_agora)`.
    """
    chave_low = (chave_grupo or "").strip().lower()
    caminho = Path(caminho_controle or config.CONTROLE_NOTICIAS_PATH)
    with _CONTROLE_LOCK:
        dados = _ler_arquivo_controle(caminho)
    bloco = dados.get(chave_low) or {}
    itens_salvos = bloco.get("itens") or []
    chaves_vistas = {
        item.get("chave") or normalizar_chave_noticia(item.get("titulo", ""), item.get("data_hora", ""))
        for item in itens_salvos
        if item.get("chave") or item.get("titulo")
    }
    if bloco.get("inicializado"):
        return chaves_vistas, [], False

    itens_bootstrap = _bootstrap_historico_gmail(
        chave_low,
        titulo_base,
        gmail_service,
        ids_excluir=ids_excluir,
        pasta_resultados=pasta_resultados,
        ultima_data=ultima_data,
    )
    for item in itens_bootstrap:
        chaves_vistas.add(item["chave"])
    return chaves_vistas, itens_bootstrap, bool(gmail_service)


def registrar_noticias_extraidas(
    chave_grupo,
    itens_novos,
    itens_bootstrap=None,
    marcar_inicializado=True,
    caminho_controle=None,
):
    """Persiste de forma thread-safe e atômica os itens extraídos para `chave_grupo`."""
    chave_low = (chave_grupo or "").strip().lower()
    caminho = Path(caminho_controle or config.CONTROLE_NOTICIAS_PATH)
    combinar = list(itens_bootstrap or []) + list(itens_novos or [])
    if not combinar and not marcar_inicializado:
        return

    with _CONTROLE_LOCK:
        dados = _ler_arquivo_controle(caminho)
        bloco = dados.get(chave_low) or {"inicializado": False, "itens": []}
        atuais = list(bloco.get("itens") or [])
        vistos = {item.get("chave") for item in atuais if item.get("chave")}

        for item in combinar:
            chave = item.get("chave") or normalizar_chave_noticia(item.get("titulo", ""), item.get("data_hora", ""))
            if not chave or chave in vistos:
                continue
            vistos.add(chave)
            atuais.append({
                "titulo": item.get("titulo", ""),
                "data_hora": item.get("data_hora", ""),
                "chave": chave,
            })

        if len(atuais) > MAX_HISTORICO_POR_PROGRAMA:
            atuais = atuais[-MAX_HISTORICO_POR_PROGRAMA:]

        dados[chave_low] = {
            "inicializado": bool(bloco.get("inicializado") or marcar_inicializado),
            "itens": atuais,
        }
        _escrever_arquivo_controle(caminho, dados)
