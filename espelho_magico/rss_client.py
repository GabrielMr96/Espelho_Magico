"""Coletor nativo de feeds RSS (WordPress / Atom) com paginação automática e deduplicação.

Substitui o fluxo externo do `canaltech_bot` / `LeitorPodcast.exe` diretamente dentro
do Espelho Mágico, sem dependências adicionais (usa apenas `requests` + `xml.etree.ElementTree`).
"""
import datetime
import email.utils
import html as html_lib
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from .deduplicacao import (
    normalizar_chave_noticia,
    normalizar_chave_url,
    normalizar_url_noticia,
    registrar_noticias_extraidas,
)
from .log import escape, logger

_FUSO_BRT = datetime.timezone(datetime.timedelta(hours=-3))
_NS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_NS_DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
_NS_ATOM = "{http://www.w3.org/2005/Atom}"
_HEADERS_RSS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def parsear_hora_minima(texto):
    """Converte 'HH:MM' em `(hora, minuto)` ou devolve `None` se vazio/inválido."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", texto or "")
    if not m:
        return None
    hora, minuto = int(m.group(1)), int(m.group(2))
    if not (0 <= hora <= 23 and 0 <= minuto <= 59):
        return None
    return (hora, minuto)


def janela_horaria_liberada(hora_minima_str, agora=None):
    """Retorna True se o horário atual em BRT atingiu ou passou `hora_minima_str` ('HH:MM')."""
    hm = parsear_hora_minima(hora_minima_str)
    if hm is None:
        return True
    agora_dt = agora or datetime.datetime.now(_FUSO_BRT)
    if agora_dt.tzinfo is not None:
        agora_dt = agora_dt.astimezone(_FUSO_BRT)
    return (agora_dt.hour, agora_dt.minute) >= hm


def _parsear_data_rss(texto):
    """Converte `pubDate` (RFC 2822) ou ISO-8601 para `datetime` em BRT (-03:00)."""
    bruto = (texto or "").strip()
    if not bruto:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(bruto)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_FUSO_BRT)
    except Exception:
        pass
    try:
        dt = datetime.datetime.fromisoformat(bruto.replace("Z", "+00:00"))
        dt = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_FUSO_BRT)
    except Exception:
        return None


def _texto_tag(elemento, *nomes):
    for nome in nomes:
        filho = elemento.find(nome)
        if filho is not None and filho.text and filho.text.strip():
            return filho.text.strip()
    return ""


def parsear_feed_rss(conteudo_xml):
    """Extrai itens de um feed RSS 2.0 (ou Atom) como dicts `{titulo, url, data_pub, data_hora, conteudo_html}`."""
    if not conteudo_xml:
        return []
    bruto = conteudo_xml.decode("utf-8", errors="replace") if isinstance(conteudo_xml, bytes) else str(conteudo_xml)
    bruto = bruto.lstrip("\ufeff \t\r\n")
    idx_xml = bruto.find("<")
    if idx_xml > 0:
        bruto = bruto[idx_xml:]
    root = ET.fromstring(bruto.encode("utf-8"))

    artigos = []
    # RSS 2.0
    itens_rss = root.findall("./channel/item")
    for item in itens_rss:
        titulo = html_lib.unescape(" ".join(_texto_tag(item, "title").split()))
        url = normalizar_url_noticia(_texto_tag(item, "link", "guid"))
        dt_pub = _parsear_data_rss(_texto_tag(item, "pubDate", _NS_DC_DATE))
        data_hora = dt_pub.strftime("%d/%m/%Y às %H:%M") if dt_pub else ""
        corpo = _texto_tag(item, _NS_CONTENT, "description")
        if not titulo or not corpo:
            continue
        artigos.append({
            "titulo": titulo,
            "url": url,
            "data_pub": dt_pub,
            "data_hora": data_hora,
            "conteudo_html": corpo,
        })

    if artigos or itens_rss:
        return artigos

    # Fallback Atom
    for entry in root.findall(f"./{_NS_ATOM}entry"):
        titulo = html_lib.unescape(" ".join(_texto_tag(entry, f"{_NS_ATOM}title").split()))
        url = ""
        for link in entry.findall(f"{_NS_ATOM}link"):
            href = (link.get("href") or "").strip()
            if href and link.get("rel", "alternate") == "alternate":
                url = normalizar_url_noticia(href)
                break
        dt_pub = _parsear_data_rss(_texto_tag(entry, f"{_NS_ATOM}published", f"{_NS_ATOM}updated"))
        data_hora = dt_pub.strftime("%d/%m/%Y às %H:%M") if dt_pub else ""
        corpo = _texto_tag(entry, f"{_NS_ATOM}content", f"{_NS_ATOM}summary")
        if not titulo or not corpo:
            continue
        artigos.append({
            "titulo": titulo,
            "url": url,
            "data_pub": dt_pub,
            "data_hora": data_hora,
            "conteudo_html": corpo,
        })
    return artigos


def _url_pagina_rss(url_base, pagina):
    if pagina <= 1:
        return url_base
    sep = "&" if "?" in url_base else "?"
    return f"{url_base}{sep}paged={pagina}"


def montar_html_feed_rss(titulo_base, artigos, agora_brt):
    """Monta um documento HTML limpo compatível com `html_cleaner`, `deduplicacao` e fatiamento por artigo."""
    ordenados = sorted(
        artigos,
        key=lambda a: (
            a.get("data_pub") or datetime.datetime.min.replace(tzinfo=_FUSO_BRT),
            a.get("titulo", "").lower(),
        ),
    )
    blocos = []
    for art in ordenados:
        url_esc = html_lib.escape(art.get("url") or "", quote=True)
        tit_esc = html_lib.escape(art.get("titulo") or "")
        dt_esc = html_lib.escape(art.get("data_hora") or agora_brt.strftime("%d/%m/%Y às %H:%M"))
        cabecalho_h2 = f'<a href="{url_esc}">{tit_esc}</a>' if url_esc else tit_esc
        attr_url = f' data-url="{url_esc}"' if url_esc else ""
        blocos.append(
            f"<article{attr_url}>\n"
            f"<h2>{cabecalho_h2}</h2>\n"
            f"<p>{dt_esc}</p>\n"
            f"<div>{art.get('conteudo_html') or ''}</div>\n"
            f"</article>"
        )
    return (
        "<html><body>\n"
        f"<h1>{html_lib.escape(titulo_base)}</h1>\n"
        f"<p>Coletado via RSS em {agora_brt:%d/%m/%Y %H:%M}</p>\n"
        "<hr/>\n"
        + "\n<hr/>\n".join(blocos)
        + "\n</body></html>"
    )


def coletar_feed_rss(
    chave,
    titulo_base,
    cfg_rss,
    chaves_vistas=None,
    ultima_data=None,
    get=requests.get,
    agora=None,
):
    """Coleta notícias inéditas de um ou mais feeds RSS paginando automaticamente até o ponto já extraído.

    Retorna uma lista com 0 ou 1 item compatível com `buscar_emails` do pipeline.
    """
    agora_brt = agora or datetime.datetime.now(_FUSO_BRT)
    if agora_brt.tzinfo is not None:
        agora_brt = agora_brt.astimezone(_FUSO_BRT)
    else:
        agora_brt = agora_brt.replace(tzinfo=_FUSO_BRT)

    urls = list(cfg_rss.get("urls") or [])
    if not urls:
        unica = cfg_rss.get("url") or cfg_rss.get("arquivo_url") or ""
        if unica:
            urls = [unica]
    if not urls:
        return []

    max_paginas = max(1, int(cfg_rss.get("max_paginas", 5)))
    janela_dias = max(1, int(cfg_rss.get("janela_dias", 4)))
    limite_janela = agora_brt.date() - datetime.timedelta(days=janela_dias)
    data_corte = max(limite_janela, ultima_data) if ultima_data else (agora_brt.date() - datetime.timedelta(days=min(janela_dias, 2)))

    vistos_execucao = set(chaves_vistas or ())
    coletados = []
    paginas_lidas = 0

    for url_feed in urls:
        urls_neste_feed = set()
        for pagina in range(1, max_paginas + 1):
            alvo = _url_pagina_rss(url_feed, pagina)
            try:
                resp = get(alvo, headers=_HEADERS_RSS, timeout=20)
            except Exception as e:
                if pagina == 1:
                    logger.warning(f"[yellow]  ⚠ [RSS] Falha ao ler '{escape(alvo)}': {escape(str(e))}[/yellow]")
                break
            status = getattr(resp, "status_code", 200)
            if status != 200:
                if pagina == 1:
                    logger.warning(f"[yellow]  ⚠ [RSS] HTTP {status} ao ler '{escape(alvo)}'[/yellow]")
                break

            try:
                conteudo = getattr(resp, "content", None) or getattr(resp, "text", "")
                artigos_pag = parsear_feed_rss(conteudo)
            except Exception as e:
                if pagina == 1:
                    logger.warning(f"[yellow]  ⚠ [RSS] XML inválido em '{escape(alvo)}': {escape(str(e))}[/yellow]")
                break
            if not artigos_pag:
                break

            # Se o site ignora ?paged=N e repete exatamente os itens da página anterior (ex.: Tecnoblog), para aqui
            chaves_pag = [a["url"] or a["titulo"] for a in artigos_pag]
            if all(c in urls_neste_feed for c in chaves_pag):
                break
            urls_neste_feed.update(chaves_pag)
            paginas_lidas += 1

            novas_pag = 0
            status_itens = []  # True = antiga ou já vista, False = nova
            for art in artigos_pag:
                dt_pub = art.get("data_pub")
                eh_antiga = dt_pub is not None and dt_pub.date() < data_corte
                chave_url = normalizar_chave_url(art.get("url", ""))
                chave_td = normalizar_chave_noticia(art["titulo"], art.get("data_hora", ""))
                ja_vista = (chave_url and chave_url in vistos_execucao) or (chave_td in vistos_execucao)

                if eh_antiga or ja_vista:
                    status_itens.append(True)
                    continue

                status_itens.append(False)
                if chave_url:
                    vistos_execucao.add(chave_url)
                vistos_execucao.add(chave_td)
                coletados.append(art)
                novas_pag += 1

            # Para de paginar quando a página não trouxe novidade ou a cauda (3 últimos itens) já chegou no histórico/corte
            cauda = status_itens[-3:] if len(status_itens) >= 3 else status_itens
            if novas_pag == 0 or (cauda and all(cauda)):
                break

    if not coletados:
        logger.info(f"[dim]  ✔ [RSS] Nenhuma notícia nova para '{escape(titulo_base)}' ({len(urls)} feed(s)).[/dim]")
        return []

    html_agregado = montar_html_feed_rss(titulo_base, coletados, agora_brt)
    logger.info(
        f"[bold cyan]🌐 [RSS] {len(coletados)} notícia(s) nova(s) coletada(s) para '{escape(titulo_base)}' "
        f"({len(urls)} feed(s) · {paginas_lidas} página(s)).[/bold cyan]"
    )
    return [{
        "id": None,
        "assunto": f"{chave} ({agora_brt:%d/%m/%Y})",
        "remetente": chave,
        "data": email.utils.format_datetime(agora_brt),
        "conteudo_html": html_agregado,
        "eh_html": True,
        "url": "",
        "fonte_chave": chave,
    }]


def importar_urls_sqlite_legado(mapeamento_dbs, caminho_controle=None, limite_por_db=300):
    """Importa as URLs mais recentes dos bancos `.db` do `canaltech_bot` para `controle_noticias.json`."""
    resumo = {}
    for chave_programa, caminho_db in mapeamento_dbs.items():
        arq = Path(caminho_db)
        if not arq.exists():
            resumo[chave_programa] = 0
            continue
        conn = sqlite3.connect(arq)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT url FROM (SELECT rowid, url FROM sent_articles ORDER BY rowid DESC LIMIT ?) ORDER BY rowid ASC",
                (limite_por_db,),
            )
            urls = [row[0] for row in cur.fetchall() if row and row[0]]
        finally:
            conn.close()
        if urls:
            registrar_noticias_extraidas(
                chave_programa,
                [],
                marcar_inicializado=True,
                caminho_controle=caminho_controle,
                urls_extras=urls,
            )
        resumo[chave_programa] = len(urls)
    return resumo
