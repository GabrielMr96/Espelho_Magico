import datetime
import json
import sqlite3

from espelho_magico.deduplicacao import (
    carregar_historico_noticias,
    filtrar_email_noticias_repetidas,
    normalizar_chave_noticia,
    normalizar_chave_url,
    normalizar_url_noticia,
    registrar_noticias_extraidas,
)
from espelho_magico.pipeline import ConfigGrupo, Opcoes, Pipeline, buscar_edicoes_faltantes_na_web
from espelho_magico.rss_client import (
    coletar_feed_rss,
    importar_urls_sqlite_legado,
    janela_horaria_liberada,
    parsear_feed_rss,
    parsear_hora_minima,
)

_FUSO_BRT = datetime.timezone(datetime.timedelta(hours=-3))


def _rss_xml(itens):
    blocos = []
    for titulo, link, pub_date, corpo in itens:
        blocos.append(
            "<item>"
            f"<title>{titulo}</title>"
            f"<link>{link}</link>"
            f"<pubDate>{pub_date}</pubDate>"
            f"<content:encoded><![CDATA[{corpo}]]></content:encoded>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel><title>Feed Teste</title>"
        + "".join(blocos)
        + "</channel></rss>"
    )


class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code


def test_normalizacao_url_e_dedup_dupla_por_url_mesmo_com_titulo_editado():
    u1 = normalizar_url_noticia("https://OVicio.com.br/gta-vi-recorde/?utm_source=rss&fbclid=123#topo")
    u2 = normalizar_url_noticia("https://ovicio.com.br/gta-vi-recorde")
    assert u1 == u2 == "https://ovicio.com.br/gta-vi-recorde"

    vistos = {normalizar_chave_url(u1)}
    html = (
        "<html><body>"
        '<article data-url="https://ovicio.com.br/gta-vi-recorde/">'
        '<h2><a href="https://ovicio.com.br/gta-vi-recorde/">GTA VI bate recorde histórico (Título Editado)</a></h2>'
        "<p>26/11/2026 às 19:00</p>"
        "<div><p>Mesmo artigo com título corrigido pelo editor.</p></div>"
        "</article>"
        '<article data-url="https://ovicio.com.br/zelda-novo/">'
        '<h2><a href="https://ovicio.com.br/zelda-novo/">Novo Zelda anunciado</a></h2>'
        "<p>26/11/2026 às 19:10</p>"
        "<div><p>Artigo inédito.</p></div>"
        "</article>"
        "</body></html>"
    )
    filtrado, removidas, mantidas, total = filtrar_email_noticias_repetidas(
        {"conteudo_html": html, "eh_html": True}, vistos
    )
    assert total == 2
    assert len(removidas) == 1
    assert len(mantidas) == 1
    assert mantidas[0]["url"] == "https://ovicio.com.br/zelda-novo"
    assert "Título Editado" not in filtrado["conteudo_html"]
    assert "Novo Zelda anunciado" in filtrado["conteudo_html"]


def test_janela_horaria_liberada():
    assert parsear_hora_minima("16:50") == (16, 50)
    assert parsear_hora_minima("") is None
    assert janela_horaria_liberada("16:50", datetime.datetime(2026, 11, 26, 16, 49, tzinfo=_FUSO_BRT)) is False
    assert janela_horaria_liberada("16:50", datetime.datetime(2026, 11, 26, 16, 50, tzinfo=_FUSO_BRT)) is True
    assert janela_horaria_liberada("16:50", datetime.datetime(2026, 11, 26, 20, 0, tzinfo=_FUSO_BRT)) is True


def test_parsear_feed_rss_converte_para_brt_e_usa_content_encoded():
    xml = _rss_xml([
        (
            "Vingadores &amp; X-Men",
            "https://ovicio.com.br/vingadores-xmen/",
            "Thu, 26 Nov 2026 20:30:00 +0000",
            "<p>Corpo completo da matéria.</p>",
        )
    ])
    artigos = parsear_feed_rss(xml)
    assert len(artigos) == 1
    assert artigos[0]["titulo"] == "Vingadores & X-Men"
    assert artigos[0]["url"] == "https://ovicio.com.br/vingadores-xmen"
    assert artigos[0]["data_hora"] == "26/11/2026 às 17:30"
    assert "Corpo completo" in artigos[0]["conteudo_html"]


def test_coletar_feed_rss_pagina_automaticamente_e_deduplica_entre_feeds():
    agora = datetime.datetime(2026, 11, 27, 17, 0, tzinfo=_FUSO_BRT)
    feed_filmes = "https://ovicio.com.br/category/filmes/feed/"
    feed_series = "https://ovicio.com.br/category/series/feed/"

    # Página 1 de filmes: 4 inéditas; Página 2: 1 inédita + 3 já extraídas (para na página 2, sem chamar página 3!)
    pag1_filmes = _rss_xml([
        ("Filme 4", "https://ovicio.com.br/filme-4/", "Fri, 27 Nov 2026 19:00:00 +0000", "<p>Texto 4</p>"),
        ("Filme 3", "https://ovicio.com.br/filme-3/", "Fri, 27 Nov 2026 18:00:00 +0000", "<p>Texto 3</p>"),
        ("Filme 2", "https://ovicio.com.br/filme-2/", "Fri, 27 Nov 2026 17:00:00 +0000", "<p>Texto 2</p>"),
        ("Cross Filmes e Séries", "https://ovicio.com.br/cross/", "Fri, 27 Nov 2026 16:00:00 +0000", "<p>Cross</p>"),
    ])
    pag2_filmes = _rss_xml([
        ("Filme 1", "https://ovicio.com.br/filme-1/", "Fri, 27 Nov 2026 15:00:00 +0000", "<p>Texto 1</p>"),
        ("Antigo 3", "https://ovicio.com.br/antigo-3/", "Thu, 26 Nov 2026 18:00:00 +0000", "<p>Velho</p>"),
        ("Antigo 2", "https://ovicio.com.br/antigo-2/", "Thu, 26 Nov 2026 17:00:00 +0000", "<p>Velho</p>"),
        ("Antigo 1", "https://ovicio.com.br/antigo-1/", "Thu, 26 Nov 2026 16:00:00 +0000", "<p>Velho</p>"),
    ])
    # Séries traz 1 post repetido de Filmes (mesma URL) + 1 série inédita + 3 antigas (de 20/11, fora da janela)
    pag1_series = _rss_xml([
        ("Cross Filmes e Séries", "https://ovicio.com.br/cross/", "Fri, 27 Nov 2026 16:00:00 +0000", "<p>Cross</p>"),
        ("Série Nova", "https://ovicio.com.br/serie-nova/", "Fri, 27 Nov 2026 14:00:00 +0000", "<p>Série inédita</p>"),
        ("Série Velha 1", "https://ovicio.com.br/sv-1/", "Fri, 20 Nov 2026 14:00:00 +0000", "<p>Velha</p>"),
        ("Série Velha 2", "https://ovicio.com.br/sv-2/", "Fri, 20 Nov 2026 13:00:00 +0000", "<p>Velha</p>"),
        ("Série Velha 3", "https://ovicio.com.br/sv-3/", "Fri, 20 Nov 2026 12:00:00 +0000", "<p>Velha</p>"),
    ])
    paginas = {
        feed_filmes: pag1_filmes,
        f"{feed_filmes}?paged=2": pag2_filmes,
        feed_series: pag1_series,
    }
    chamadas = []

    def fake_get(url, headers=None, timeout=20):
        chamadas.append(url)
        if url in paginas:
            return _Resp(paginas[url])
        return _Resp("", status_code=404)

    chaves_vistas = {
        normalizar_chave_url("https://ovicio.com.br/antigo-3/"),
        normalizar_chave_url("https://ovicio.com.br/antigo-2/"),
        normalizar_chave_url("https://ovicio.com.br/antigo-1/"),
    }
    itens = coletar_feed_rss(
        "notícias de entretenimento",
        "Pipoca Vencida",
        {"urls": [feed_filmes, feed_series], "max_paginas": 5, "janela_dias": 4},
        chaves_vistas=chaves_vistas,
        ultima_data=datetime.date(2026, 11, 26),
        get=fake_get,
        agora=agora,
    )
    assert len(itens) == 1
    # Não deve ter chamado ?paged=3 em filmes nem ?paged=2 em séries (parada precoce pela cauda)
    assert f"{feed_filmes}?paged=3" not in chamadas
    assert f"{feed_series}?paged=2" not in chamadas
    html = itens[0]["conteudo_html"]
    # 5 filmes + 1 série inédita = 6 artigos (sem duplicar Cross e sem antigos)
    assert html.count("<article ") == 6
    assert "Antigo 3" not in html and "Série Velha" not in html
    assert html.count("Cross Filmes e Séries") == 1
    # Ordem cronológica crescente: Série Nova (11h BRT) antes de Filme 4 (16h BRT)
    assert html.index("Série Nova") < html.index("Filme 1") < html.index("Filme 4")


def test_coletar_feed_rss_para_quando_site_ignora_paginacao():
    agora = datetime.datetime(2026, 11, 27, 17, 0, tzinfo=_FUSO_BRT)
    url_tb = "https://tecnoblog.net/tema/seguranca-e-privacidade/feed/"
    mesma_pag = _rss_xml([
        ("Falha Crítica", "https://tecnoblog.net/noticias/falha/", "Fri, 27 Nov 2026 15:00:00 +0000", "<p>Detalhes</p>"),
    ])
    chamadas = []

    def fake_get(url, headers=None, timeout=20):
        chamadas.append(url)
        return _Resp(mesma_pag)

    itens = coletar_feed_rss(
        "notícias de segurança",
        "Hackeando o Problema",
        {"urls": [url_tb], "max_paginas": 4, "janela_dias": 4},
        chaves_vistas=set(),
        ultima_data=datetime.date(2026, 11, 26),
        get=fake_get,
        agora=agora,
    )
    assert len(itens) == 1
    # Página 2 retornou exatamente os mesmos itens da página 1 -> parou sem tentar página 3/4
    assert len(chamadas) == 2
    assert itens[0]["conteudo_html"].count("<article ") == 1


def test_buscar_web_rss_respeita_horario_1650_trava_diaria_e_bypass(tmp_path):
    feed_games = "https://ovicio.com.br/category/games/feed/"
    xml_games = _rss_xml([
        ("GTA VI bate recorde", "https://ovicio.com.br/gta-recorde/", "Fri, 27 Nov 2026 18:00:00 +0000", "<p>Conteúdo completo sobre GTA VI.</p>"),
    ])
    chamadas = []

    def fake_get(url, headers=None, timeout=20):
        chamadas.append(url)
        return _Resp(xml_games)

    fontes = {
        "notícias de games": {
            "tipo": "rss",
            "urls": [feed_games],
            "hora_minima": "16:50",
            "max_paginas": 3,
            "janela_dias": 4,
            "max_por_dia": 1,
        }
    }
    mapa = {"notícias de games": "Controle Quebrado"}
    ctrl_fontes = tmp_path / "controle_fontes.json"
    ctrl_noticias = tmp_path / "controle_noticias.json"
    ctrl_fontes.write_text(
        json.dumps({"notícias de games": {"ultima_data": "2026-11-26", "urls": []}}),
        encoding="utf-8",
    )

    # 1) Antes das 16:50 (14:00 BRT) -> não consulta o feed
    res_cedo = buscar_edicoes_faltantes_na_web(
        [],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapa,
        get=fake_get,
        caminho_controle_fontes=ctrl_fontes,
        caminho_controle_noticias=ctrl_noticias,
        agora=datetime.datetime(2026, 11, 27, 14, 0, tzinfo=_FUSO_BRT),
    )
    assert res_cedo == []
    assert chamadas == []

    # 2) Com --grupo games às 14:00 -> bypassa a trava de horário e coleta!
    res_grupo = buscar_edicoes_faltantes_na_web(
        [],
        pasta_resultados=tmp_path,
        filtros_grupo=["games"],
        fontes_web=fontes,
        mapeamento=mapa,
        get=fake_get,
        caminho_controle_fontes=ctrl_fontes,
        caminho_controle_noticias=ctrl_noticias,
        agora=datetime.datetime(2026, 11, 27, 14, 0, tzinfo=_FUSO_BRT),
    )
    assert len(res_grupo) == 1
    assert "GTA VI bate recorde" in res_grupo[0]["conteudo_html"]

    # 3) A partir das 16:50 sem filtro -> coleta normalmente
    res_tarde = buscar_edicoes_faltantes_na_web(
        [],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapa,
        get=fake_get,
        caminho_controle_fontes=ctrl_fontes,
        caminho_controle_noticias=ctrl_noticias,
        agora=datetime.datetime(2026, 11, 27, 16, 50, tzinfo=_FUSO_BRT),
    )
    assert len(res_tarde) == 1

    # 4) Já extraído hoje (ultima_data == 2026-11-27) às 19:30 -> trava de 1 edição/dia segura para amanhã
    ctrl_fontes.write_text(
        json.dumps({"notícias de games": {"ultima_data": "2026-11-27", "urls": []}}),
        encoding="utf-8",
    )
    chamadas.clear()
    res_noite = buscar_edicoes_faltantes_na_web(
        [],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapa,
        get=fake_get,
        caminho_controle_fontes=ctrl_fontes,
        caminho_controle_noticias=ctrl_noticias,
        agora=datetime.datetime(2026, 11, 27, 19, 30, tzinfo=_FUSO_BRT),
    )
    assert res_noite == []
    assert chamadas == []

    # 5) Com --forcar-pares às 19:30 -> ignora trava diária e coleta!
    res_forcar = buscar_edicoes_faltantes_na_web(
        [],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapa,
        get=fake_get,
        caminho_controle_fontes=ctrl_fontes,
        caminho_controle_noticias=ctrl_noticias,
        forcar_pares=True,
        agora=datetime.datetime(2026, 11, 27, 19, 30, tzinfo=_FUSO_BRT),
    )
    assert len(res_forcar) == 1


def test_importar_urls_sqlite_legado_e_persistencia_no_pipeline(tmp_path):
    db_path = tmp_path / "news_games.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sent_articles (url TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO sent_articles (url) VALUES (?)",
        [("https://ovicio.com.br/velha-1/",), ("https://ovicio.com.br/velha-2/",)],
    )
    conn.commit()
    conn.close()

    ctrl_noticias = tmp_path / "controle_noticias.json"
    ctrl_fontes = tmp_path / "controle_fontes.json"
    resumo = importar_urls_sqlite_legado({"notícias de games": db_path}, caminho_controle=ctrl_noticias)
    assert resumo == {"notícias de games": 2}

    vistos, _, _ = carregar_historico_noticias("notícias de games", "Controle Quebrado", caminho_controle=ctrl_noticias)
    assert normalizar_chave_url("https://ovicio.com.br/velha-2") in vistos

    # Fluxo fim a fim no Pipeline com item RSS -> grava URL + chave no controle_noticias e data no controle_fontes
    feed_games = "https://ovicio.com.br/category/games/feed/"
    xml = _rss_xml([
        ("Velha 2", "https://ovicio.com.br/velha-2/", "Fri, 27 Nov 2026 17:00:00 +0000", "<p>Já vista no SQLite.</p>"),
        ("Nova Inédita", "https://ovicio.com.br/nova-inedita/", "Fri, 27 Nov 2026 18:00:00 +0000", "<p>" + ("Texto detalhado inédito de games. " * 10) + "</p>"),
    ])
    agora = datetime.datetime(2026, 11, 27, 17, 0, tzinfo=_FUSO_BRT)
    itens = coletar_feed_rss(
        "notícias de games",
        "Controle Quebrado",
        {"urls": [feed_games], "max_paginas": 2, "janela_dias": 4},
        chaves_vistas=vistos,
        ultima_data=datetime.date(2026, 11, 26),
        get=lambda url, **kw: _Resp(xml),
        agora=agora,
    )
    assert len(itens) == 1
    assert "Velha 2" not in itens[0]["conteudo_html"]
    assert "Nova Inédita" in itens[0]["conteudo_html"]

    class ExtratorFalso:
        def __init__(self):
            self.n = 0

        def extrair(self, _c, _p):
            self.n += 1
            if self.n == 1:
                return "# 27 de novembro de 2026\n\n## Nova Inédita\n\n" + ("Texto detalhado inédito de games. " * 10)
            return "## Enquete do Dia\n\nQual jogo?\n\n- A\n- B\n- C\n- D"

    pipe = Pipeline(Opcoes(dry_run=False, max_workers=1), extrator=ExtratorFalso(), drive_service=None, gmail_service=None)
    pipe.pasta_saida = tmp_path
    pipe.pasta_historico = tmp_path
    pipe.caminho_controle_noticias = ctrl_noticias
    pipe.caminho_controle_fontes = ctrl_fontes
    cfg = ConfigGrupo(
        chave="notícias de games",
        emails=itens,
        titulo_base="Controle Quebrado",
        nome_final="Controle Quebrado #289.md",
    )
    res = pipe.executar([cfg])
    assert res[0].status == "ok"

    dados_not = json.loads(ctrl_noticias.read_text(encoding="utf-8"))
    assert "https://ovicio.com.br/nova-inedita" in dados_not["notícias de games"]["urls"]
    dados_fnt = json.loads(ctrl_fontes.read_text(encoding="utf-8"))
    assert dados_fnt["notícias de games"]["ultima_data"] == "2026-11-27"
