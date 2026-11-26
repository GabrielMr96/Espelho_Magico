import datetime

from espelho_magico.drive_client import montar_query
from espelho_magico.pipeline import (
    ConfigGrupo,
    Opcoes,
    Pipeline,
    _post_ja_extraido_localmente,
    agrupar_emails,
    aplicar_cabecalho_edicao,
    buscar_edicoes_faltantes_na_web,
    buscar_email_de_url,
    deduplicar_secoes,
    eh_extracao_vazia_ou_invalida,
    extrair_data_de_texto,
    formatar_data_email,
    inserir_apos_h1,
    maior_edicao_existente,
    nome_fallback,
    sanitizar_markdown_final,
    ultima_data_extraida_local,
)

MAPA = {"tns sports": "Gol Contra", "the news": "Boletim Fora de Ordem", "peak": "Dispatch"}


def test_agrupar_por_assunto_e_remetente():
    emails = [
        {"assunto": "TNS Sports: rodada", "remetente": "x@y.com"},
        {"assunto": "Edição de hoje", "remetente": "Peak <peak@news.com>"},
        {"assunto": "tns sports - extra", "remetente": "x@y.com"},
        {"assunto": "Promoção aleatória", "remetente": "loja@z.com"},
    ]
    grupos = agrupar_emails(emails, MAPA)
    assert len(grupos["tns sports"]) == 2
    assert len(grupos["peak"]) == 1
    assert "promoção aleatória" in grupos


def test_maior_edicao_existente(tmp_path):
    for nome in ("Gol Contra #9.md", "Gol Contra #12.md", "Gol Contra #rascunho.md", "Outro #50.md"):
        (tmp_path / nome).write_text("x", encoding="utf-8")
    assert maior_edicao_existente(tmp_path, "Gol Contra") == 12
    assert maior_edicao_existente(tmp_path, "Inexistente") == 0


def test_nome_fallback_remove_caracteres_invalidos():
    import datetime
    base, nome = nome_fallback('assunto: "teste"/ok?', datetime.datetime(2026, 1, 2, 3, 4, 5))
    assert base == "assunto teste ok" or base == "assunto testeok"
    assert nome.endswith("_20260102_030405.md")


def test_dedup_remove_secao_identica_e_mescla_diferente():
    texto = "\n".join([
        "# Edição",
        "## Política",
        "Texto A.",
        "## Economia",
        "Texto B.",
        "## Política",
        "Texto A.",
        "## Economia",
        "Texto C novo.",
        "",
        "---",
        "",
        "## Esportes",
        "Texto D.",
    ])
    out = deduplicar_secoes(texto)
    assert out.count("## Política") == 1
    assert out.count("Texto A.") == 1
    assert out.count("## Economia") == 1
    # corpo diferente vai para a primeira ocorrência, não para a seção anterior
    eco = out.index("## Economia")
    assert eco < out.index("Texto B.") < out.index("Texto C novo.") < out.index("---") < out.index("## Esportes")


def test_dedup_reinicia_em_resultados_recentes():
    texto = "\n".join([
        "## RESULTADOS RECENTES (10/10/2026)",
        "### Premier League",
        "- A 1 x 0 B",
        "## RESULTADOS RECENTES (11/10/2026)",
        "### Premier League",
        "- C 2 x 2 D",
    ])
    out = deduplicar_secoes(texto)
    assert out.count("### Premier League") == 2


def test_query_drive_escapa_aspas_e_ignora_lixeira():
    q = montar_query("D'Ávila #1", "D'Ávila #1.md", "pasta123")
    assert "name='D\\'Ávila #1'" in q
    assert "trashed=false" in q
    assert "'pasta123' in parents" in q


def test_sanitizar_markdown_final():
    texto = "\n".join([
        "# 12 de Outubro de 2026",
        "",
        "## A IA vai acabar com empregos?",
        "Source: FanDuel.",
        "Texto da matéria. (Aprofunde)",
        "Comentário: caso queira aprofundar, fica a critério.",
        "**“**",
        "",
        "[Descrição da Imagem: Fortune | Getty Images]",
        "[Descrição da Imagem: Gráfico detalhado de vendas de veículos elétricos]",
        "",
        "# 13 de Outubro de 2026",
        "",
        "## Outra Notícia Importante!",
        "Mais texto com link do Twitter pic.twitter.com/abc1234 e https://t.co/xyz789.",
    ])
    out = sanitizar_markdown_final(texto)
    import re
    # Garante exatamente 1 H1 no documento todo
    assert len(re.findall(r"^# ", out, re.M)) == 1
    assert "# 12 de Outubro de 2026" in out
    # O segundo H1 vira H2
    assert "## 13 de Outubro de 2026" in out
    # Pontuação final de cabeçalho removida
    assert "## A IA vai acabar com empregos\n" in out
    assert "## Outra Notícia Importante\n" in out
    # (Aprofunde), Source: FanDuel e links de twitter removidos
    assert "(Aprofunde)" not in out
    assert "caso queira aprofundar, fica a critério." in out
    assert "**“**" not in out
    assert "Source: FanDuel" not in out
    assert "pic.twitter.com" not in out
    assert "https://t.co" not in out
    # Imagem de crédito descartada, imagem com descrição informativa preservada
    assert "Fortune | Getty Images" not in out
    assert "[Descrição da Imagem: Gráfico detalhado de vendas de veículos elétricos]" in out


def test_formatar_data_e_preparar_metadados():
    assert formatar_data_email("Mon, 26 Oct 2026 10:30:10 +0000") == "26 de outubro de 2026"
    email_obj = {
        "assunto": "Brace for impact",
        "data": "Mon, 26 Oct 2026 10:30:10 +0000",
        "conteudo_html": "Texto simples da edição",
        "eh_html": False,
    }
    prep = Pipeline._preparar(email_obj)
    assert "[METADADOS DO E-MAIL — Data do e-mail: 26 de outubro de 2026 | Assunto: Brace for impact]" in prep
    assert "Texto simples da edição" in prep


def test_eh_extracao_vazia_ou_invalida():
    assert eh_extracao_vazia_ou_invalida("")
    assert eh_extracao_vazia_ou_invalida("[SEM_CONTEUDO_EDITORIAL]")
    assert eh_extracao_vazia_ou_invalida("# 26 de outubro de 2026\n\nTexto curto demais.")
    valido = "# 26 de outubro de 2026\n\n## Notícia Completa\n\n" + ("Parágrafo com conteúdo real da notícia. " * 10)
    assert not eh_extracao_vazia_ou_invalida(valido)


def test_dedup_nao_funde_subtitulos_genericos_entre_materias_diferentes():
    texto = "\n".join([
        "# 6 de novembro de 2026",
        "## Hackers atacam freezers na França",
        "### Resumo",
        "- Ataque atingiu bancos de sangue.",
        "### Detalhes do caso",
        "Alarmes soaram a tempo.",
        "## Microsoft Paint tem falha grave",
        "### Resumo",
        "- Falha CVE permite código remoto no Paint.",
        "### Correção liberada",
        "Atualize pela Microsoft Store.",
    ])
    out = deduplicar_secoes(texto)
    idx_hackers = out.index("## Hackers atacam freezers na França")
    idx_paint = out.index("## Microsoft Paint tem falha grave")
    idx_cve = out.index("Falha CVE permite código remoto no Paint.")
    assert idx_hackers < idx_paint < idx_cve
    assert out.count("### Resumo") == 2


def test_inserir_apos_h1_mantem_data_na_primeira_linha():
    corpo = "# 6 de novembro de 2026\n\n## O gol histórico\n\nTexto da matéria."
    futebol_md = "## RESULTADOS RECENTES (05/11/2026)\n\n### Brasileiro Serie A\n- Palmeiras 6 x 1 Remo"
    combinado = sanitizar_markdown_final(inserir_apos_h1(corpo, futebol_md))
    assert combinado.startswith("# 6 de novembro de 2026\n\n## RESULTADOS RECENTES (05/11/2026)")
    assert combinado.index("## RESULTADOS RECENTES") < combinado.index("## O gol histórico")


def test_sanitizar_remove_cercas_markdown_externas():
    bruto = "```markdown\n# 7 de novembro de 2026\n\n## Título limpo?\n\nCorpo da notícia.\n```"
    limpo = sanitizar_markdown_final(bruto)
    assert limpo.startswith("# 7 de novembro de 2026")
    assert "```" not in limpo
    assert "## Título limpo\n" in limpo


def test_formatar_data_ingles_e_montar_prompt_por_prefixo():
    assert formatar_data_email("Sat, 07 Nov 2026 12:00:00 +0000", idioma="en") == "November 7, 2026"
    email_en = {
        "assunto": "This Startup Wants Rockets",
        "data": "Sat, 07 Nov 2026 12:00:00 +0000",
        "conteudo_html": "English body content",
        "eh_html": False,
    }
    prep = Pipeline._preparar(email_en, idioma="en")
    assert "<metadados>" in prep and "<documento_entrada>" in prep
    assert "Email Date: November 7, 2026" in prep
    assert "Output Language: 100% English" in prep

    pipe = Pipeline(Opcoes(dry_run=True), extrator=None, drive_service=None, gmail_service=None)
    prompt_peak = pipe._montar_prompt("peak", unificar=True)
    assert "<diretrizes_especificas_desta_fonte>" in prompt_peak
    assert "100% EM INGLÊS" in prompt_peak
    assert "<modo_unificacao>" in prompt_peak


def test_buscar_email_de_url_infere_prefixo_e_data():
    class RespFalsa:
        text = "<html><head><title>10/11/2026 · the news</title></head><body><h1>o brasileirão virou a la liga?</h1></body></html>"

        def raise_for_status(self):
            pass

    item = buscar_email_de_url(
        "https://thenews.com.br/newsletter/sports/10-11-2026",
        mapeamento={"tns sports": "Gol Contra", "the news": "Boletim Fora de Ordem"},
        get=lambda *a, **kw: RespFalsa(),
    )
    assert item["id"] is None
    assert item["remetente"] == "tns sports"
    assert item["assunto"] == "tns sports: o brasileirão virou a la liga?"
    assert formatar_data_email(item["data"]) == "10 de novembro de 2026"


def test_extrair_data_e_ultima_data_extraida_local(tmp_path):
    assert extrair_data_de_texto("# 9 de novembro de 2026") == datetime.date(2026, 11, 9)
    assert extrair_data_de_texto("# November 10, 2026") == datetime.date(2026, 11, 10)
    assert extrair_data_de_texto("Nov. 5, 2026") == datetime.date(2026, 11, 5)
    assert extrair_data_de_texto("/newsletter/sports/10-11-2026") == datetime.date(2026, 11, 10)

    (tmp_path / "Gol Contra #195.md").write_text("# 6 de novembro de 2026\n\n## Titulo", encoding="utf-8")
    (tmp_path / "Gol Contra #196.md").write_text("# 9 de novembro de 2026\n\n## Titulo", encoding="utf-8")
    assert ultima_data_extraida_local(tmp_path, "Gol Contra") == datetime.date(2026, 11, 9)


def test_buscar_edicoes_faltantes_na_web_recupera_apenas_edicao_nova(tmp_path):
    (tmp_path / "Gol Contra #196.md").write_text("# 9 de novembro de 2026\n\n## Jogo", encoding="utf-8")
    (tmp_path / "Boletim Fora de Ordem #315.md").write_text("# 10 de novembro de 2026\n\n## Dia", encoding="utf-8")

    paginas = {
        "https://thenews.com.br/newsletter/sports": (
            '<html><body>'
            '<a href="/newsletter/sports/10-11-2026">10 de novembro de 2026 · Sports · o brasileirão virou a la liga?</a>'
            '<a href="/newsletter/sports/09-11-2026">09 de novembro de 2026 · Sports · palmeiras x grêmio</a>'
            '</body></html>'
        ),
        "https://thenews.com.br/newsletter/sports/10-11-2026": (
            '<html><body><h1>o brasileirão virou a la liga?</h1><p>Conteúdo da edição</p></body></html>'
        ),
        "https://thenews.com.br/newsletter/the-news": (
            '<html><body>'
            '<a href="/newsletter/the-news/10-11-2026">10 de novembro de 2026 · The News · manchete de hoje</a>'
            '</body></html>'
        ),
    }

    class Resp:
        def __init__(self, text):
            self.text = text
            self.status_code = 200

        def raise_for_status(self):
            pass

    fontes = {
        "tns sports": {"tipo": "thenews", "arquivo_url": "https://thenews.com.br/newsletter/sports"},
        "the news": {"tipo": "thenews", "arquivo_url": "https://thenews.com.br/newsletter/the-news"},
    }
    mapeamento = {"tns sports": "Gol Contra", "the news": "Boletim Fora de Ordem"}

    recuperados = buscar_edicoes_faltantes_na_web(
        emails_atuais=[],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapeamento,
        get=lambda url, **kw: Resp(paginas[url]),
    )
    assert len(recuperados) == 1
    assert recuperados[0]["remetente"] == "tns sports"
    assert "o brasileirão virou a la liga?" in recuperados[0]["assunto"]
    assert formatar_data_email(recuperados[0]["data"]) == "10 de novembro de 2026"


def test_aplicar_cabecalho_edicao_e_compatibilidade_com_data(tmp_path):
    md = "# 10 de novembro de 2026\n\n## Manchete Principal\n\nTexto da notícia."
    com_cabecalho = aplicar_cabecalho_edicao(md, "Controle Quebrado #274.md")
    assert com_cabecalho.splitlines()[0] == "# Controle Quebrado #274 — terça-feira, 10 de novembro de 2026"
    # Idempotente se aplicado novamente
    assert aplicar_cabecalho_edicao(com_cabecalho, "Controle Quebrado #274.md") == com_cabecalho

    # Também inclui dia da semana em inglês
    md_en = "# November 7, 2026\n\n## Big Story\n\nSome content."
    com_cabecalho_en = aplicar_cabecalho_edicao(md_en, "The Out of Order Dispatch #70.md", idioma="en")
    assert com_cabecalho_en.splitlines()[0] == "# The Out of Order Dispatch #70 — Saturday, November 7, 2026"
    assert aplicar_cabecalho_edicao(com_cabecalho_en, "The Out of Order Dispatch #70.md", idioma="en") == com_cabecalho_en

    # Continua sendo lido normalmente por ultima_data_extraida_local
    (tmp_path / "Controle Quebrado #274.md").write_text(com_cabecalho, encoding="utf-8")
    assert ultima_data_extraida_local(tmp_path, "Controle Quebrado") == datetime.date(2026, 11, 10)


def test_pipeline_executar_com_dupla_verificacao(tmp_path):
    class ExtratorFalso:
        def __init__(self):
            self.chamadas = 0

        def extrair(self, conteudo, prompt):
            self.chamadas += 1
            if self.chamadas == 1:
                return (
                    "# 13 de novembro de 2026\n\n"
                    "## Mercado em alta\n\n"
                    "| Ativo | Valor |\n| --- | --- |\n| Ibovespa | 172.000 |\n\n"
                    + ("Texto completo da notícia para passar do mínimo exigido pela validação. " * 6)
                )
            return "## Enquete do Dia\n\nQuem leva o troféu?\n\n- A\n- B\n- C\n- D"

    extrator = ExtratorFalso()
    pipe = Pipeline(Opcoes(dry_run=True, max_workers=1), extrator=extrator, drive_service=None, gmail_service=None)
    pipe.pasta_saida = tmp_path
    cfg = ConfigGrupo(
        chave="tns money",
        emails=[{
            "id": None,
            "assunto": "tns money: teste",
            "remetente": "tns money",
            "data": "Fri, 13 Nov 2026 09:00:00 +0000",
            "conteudo_html": "<p>Ibovespa em 172.000 pontos hoje com texto completo da notícia.</p>",
            "eh_html": True,
        }],
        titulo_base="Fechamento de Caixa",
        nome_final="Fechamento de Caixa #170.md",
    )
    resultados = pipe.executar([cfg])
    assert len(resultados) == 1
    assert resultados[0].status == "dry"
    assert resultados[0].noticias == 1
    gerado = (tmp_path / "Fechamento de Caixa #170.md").read_text(encoding="utf-8")
    assert gerado.startswith("# Fechamento de Caixa #170 — sexta-feira, 13 de novembro de 2026")
    assert "- **Ibovespa:** Valor: 172.000" in gerado
    assert "## Enquete do Dia" in gerado and "- A" in gerado
    assert "|" not in gerado


def test_buscar_edicoes_beehiiv_recupera_manha_e_night_faltante(tmp_path):
    (tmp_path / "Boletim Fora de Ordem #318.md").write_text(
        "# Boletim Fora de Ordem #318 — 13 de novembro de 2026\n\n## Espanha aprova medidas\n\nTexto da manhã.\n",
        encoding="utf-8",
    )

    archive_html = (
        "<html><body>"
        '<a href="/p/14-11-2026"></a>'
        '<a href="/p/14-11-2026">3 hours ago 14/11/2026</a>'
        '<a href="/p/night-350"></a>'
        '<a href="/p/night-350">14 hours ago NIGHT #350 13/11/2026</a>'
        '<a href="/p/13-11-2026"></a>'
        '<a href="/p/13-11-2026">Nov 13, 2026 13/11/2026</a>'
        "</body></html>"
    )
    post_14 = "<html><body><h1>14/11/2026</h1><p>EUA suspendem decolagens e pousos em todo o país.</p></body></html>"
    post_night = (
        "<html><body><h1>NIGHT #350</h1>"
        "<p>Para hoje… Flávio Bolsonaro diz que não apoia a reeleição de Hugo Motta; "
        "United proíbe agentes de AI de comprar passagens; Buser começa a vender voos; e mais. Sexta-feira, 13/11/26</p>"
        "</body></html>"
    )
    paginas = {
        "https://thenewscc.beehiiv.com/archive": archive_html,
        "https://thenewscc.beehiiv.com/p/14-11-2026": post_14,
        "https://thenewscc.beehiiv.com/p/night-350": post_night,
    }

    class Resp:
        def __init__(self, text):
            self.text = text
            self.status_code = 200

        def raise_for_status(self):
            pass

    fontes = {"the news": {"tipo": "beehiiv", "arquivo_url": "https://thenewscc.beehiiv.com/archive"}}
    mapeamento = {"the news": "Boletim Fora de Ordem"}

    # Sem e-mails no Gmail: recupera tanto 14/11 quanto NIGHT #350 de 13/11 (ignora 13/11 matinal)
    recuperados = buscar_edicoes_faltantes_na_web(
        emails_atuais=[],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapeamento,
        get=lambda url, **kw: Resp(paginas[url]),
    )
    assert len(recuperados) == 2
    assert recuperados[0]["assunto"] == "the news: 14/11/2026"
    assert formatar_data_email(recuperados[0]["data"]) == "14 de novembro de 2026"
    assert recuperados[1]["assunto"] == "the news: NIGHT #350"
    assert formatar_data_email(recuperados[1]["data"]) == "13 de novembro de 2026"

    # Se NIGHT #350 já estiver contida no arquivo local de 13/11, não baixa novamente
    (tmp_path / "Boletim Fora de Ordem #318.md").write_text(
        "# Boletim Fora de Ordem #318 — 13 de novembro de 2026\n\n"
        "## Flávio Bolsonaro não apoia Hugo Motta\n\n"
        "United proíbe agentes de comprar passagens e Buser vende voos.\n",
        encoding="utf-8",
    )
    recuperados2 = buscar_edicoes_faltantes_na_web(
        emails_atuais=[],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapeamento,
        get=lambda url, **kw: Resp(paginas[url]),
    )
    assert [r["assunto"] for r in recuperados2] == ["the news: 14/11/2026"]


def test_dedup_remove_paragrafos_repetidos_e_secao_redundante_entre_h2():
    rodape_gta = (
        "A etapa de pré-venda teria superado 15 milhões de unidades comercializadas antecipadamente, "
        "culminando em uma receita de US$ 1,45 bilhão, de acordo com dados compartilhados pelo Sensor Tower."
    )
    texto = "\n\n".join([
        "# 16 de novembro de 2026",
        "## GTA VI muda monetização na Twitch\n\nA Twitch prepara mudanças nas regras para jogos adultos.\n\n" + rodape_gta,
        "## GTA VI pode vender 55 milhões até março\n\nAnalistas projetam 55 milhões de cópias vendidas no ano fiscal.\n\n" + rodape_gta,
        "## 007: Robert Pattinson aparece em teaser\n\n"
        "A Amazon MGM divulgou um teaser oficial revelando Robert Pattinson como James Bond.\n\n"
        "Para garantir o papel de James Bond, Pattinson superou uma disputada etapa final de testes que também reuniu "
        "Nicholas Hoult, Jack Lowden, Callum Turner e Josh O'Connor.\n\n"
        "Aos 40 anos, Pattinson mantém a média etária adotada pelos antecessores no papel, já que Daniel Craig tinha 38 anos "
        "quando foi anunciado e Pierce Brosnan assumiu o posto aos 42.\n\n"
        "Na obra literária de Fleming, Bond é retratado como um homem britânico alto, magro, de cabelos escuros e olhos azuis "
        "que combina charme com uma personalidade fria e letal.",
        "## O nome é Bond: Robert Pattinson é anunciado como novo 007\n\n"
        "Robert Pattinson acaba de ser oficializado pela Amazon MGM como James Bond.\n\n"
        "Para conquistar o posto, Pattinson superou uma etapa final de testes que também reuniu "
        "Nicholas Hoult, Jack Lowden, Callum Turner e Josh O'Connor.\n\n"
        "Aos 40 anos, Pattinson mantém a média etária adotada anteriormente, considerando que Daniel Craig tinha 38 anos "
        "quando foi anunciado e Pierce Brosnan assumiu aos 42.\n\n"
        "Na obra literária de Fleming, Bond é retratado como um homem britânico alto, magro, de cabelos escuros e olhos azuis "
        "que combina charme com uma personalidade fria e letal.",
    ])
    out = deduplicar_secoes(texto)
    # Rodapé repetido removido da segunda matéria de GTA, mantendo o dado inédito de 55 milhões
    assert out.count("15 milhões de unidades") == 1
    assert "Analistas projetam 55 milhões de cópias" in out
    # Seção quase duplicada de 007 descartada, mantendo a primeira
    assert "## 007: Robert Pattinson aparece em teaser" in out
    assert "## O nome é Bond: Robert Pattinson é anunciado como novo 007" not in out


def test_pipeline_ignora_aviso_futebol_sem_jogos(tmp_path, monkeypatch):
    from espelho_magico import futebol

    monkeypatch.setattr(futebol, "coletar_resultados", lambda: ("Nenhum jogo relevante encontrado para 16/11/2026.", None))

    class ExtratorFalso:
        chamadas = 0

        def extrair(self, _c, _p):
            self.chamadas += 1
            if self.chamadas == 1:
                return (
                    "# 17 de novembro de 2026\n\n"
                    "## Matéria esportiva principal\n\n"
                    + ("Texto completo da pauta esportiva do dia com todos os detalhes. " * 6)
                )
            return "## Enquete do Dia\n\nQuem vence?\n\n- A\n- B\n- C\n- D"

    pipe = Pipeline(Opcoes(dry_run=True, max_workers=1), extrator=ExtratorFalso(), drive_service=None, gmail_service=None)
    pipe.pasta_saida = tmp_path
    cfg = ConfigGrupo(
        chave="tns sports",
        emails=[{
            "id": None,
            "assunto": "tns sports: teste",
            "remetente": "tns sports",
            "data": "Tue, 17 Nov 2026 09:00:00 +0000",
            "conteudo_html": "<p>Texto completo da pauta esportiva do dia com todos os detalhes.</p>",
            "eh_html": True,
        }],
        titulo_base="Gol Contra",
        nome_final="Gol Contra #203.md",
    )
    pipe.executar([cfg])
    gerado = (tmp_path / "Gol Contra #203.md").read_text(encoding="utf-8")
    assert "Nenhum jogo" not in gerado
    assert "## Matéria esportiva principal" in gerado


def test_beehiiv_night_nao_da_falso_positivo_por_palavras_genericas_espalhadas():
    html_night = (
        "<html><body>"
        "<p>Para hoje… Senado adia votação do ajuste fiscal; Um ataque hacker contra mais de 170 marcas; O último jogo da seleção em 2026; e mais.</p>"
        "<p>Milhões de caranguejos vermelhos começaram a migração anual na Ilha Christmas, território australiano no Oceano Índico.</p>"
        "<p>O Senado adiou para amanhã a votação dos quatro destaques da Lei Kandir no pacote fiscal com impacto de até R$ 60 bilhões em 2027.</p>"
        "<p>Os sites de Zoom, BBC, CNN e mais de 170 marcas exibiram códigos maliciosos após invasão na plataforma Optimizely.</p>"
        "<p>Vini Jr. marcou três gols nos primeiros 20 minutos na goleada de 7 a 0 do Brasil sobre Singapura em amistoso internacional.</p>"
        "</body></html>"
    )
    texto_matinal_sem_night = (
        "# Boletim Fora de Ordem #322 — 17 de novembro de 2026\n\n"
        "## Risco fiscal e pacote no Congresso\n\n"
        "O ajuste econômico voltou ao debate no Senado antes do último trimestre com corte de gastos.\n\n"
        "## Ciberataque global e seleção de chips\n\n"
        "Um ataque cibernético atribuído a grupo hacker atingiu redes corporativas em resposta ao último relatório.\n\n"
        "## Amistoso de futebol\n\n"
        "A seleção europeia convocou atletas para a próxima janela.\n"
    )
    assert _post_ja_extraido_localmente(html_night, texto_matinal_sem_night) is False

    texto_com_night = (
        texto_matinal_sem_night
        + "\n## Migração na Ilha Christmas\n\n"
        + "Milhões de caranguejos vermelhos começaram a migração anual na Ilha Christmas no Oceano Índico.\n\n"
        + "## Optimizely afetou mais de 170 marcas\n\n"
        + "Os sites de Zoom, BBC e CNN exibiram códigos maliciosos por falha na plataforma Optimizely.\n\n"
        + "## Goleada sobre Singapura\n\n"
        + "Vini Jr. marcou três gols nos primeiros 20 minutos na vitória de 7 a 0 do Brasil sobre Singapura.\n"
    )
    assert _post_ja_extraido_localmente(html_night, texto_com_night) is True


def test_preparar_grupos_ordena_noite_anterior_antes_da_manha_hoje(tmp_path):
    pipe = Pipeline(Opcoes(dry_run=True, unificar="sim"), extrator=None, drive_service=None, gmail_service=None)
    emails = [
        {"id": None, "assunto": "the news: 18/11/2026", "remetente": "the news", "data": "Wed, 18 Nov 2026 09:00:00 +0000", "conteudo_html": "<p>Manhã</p>"},
        {"id": None, "assunto": "the news: NIGHT #352", "remetente": "the news", "data": "Tue, 17 Nov 2026 09:00:00 +0000", "conteudo_html": "<p>Noite</p>"},
    ]
    cfgs = pipe.preparar_grupos(emails)
    assert len(cfgs) == 1
    assert [e["assunto"] for e in cfgs[0].emails] == ["the news: NIGHT #352", "the news: 18/11/2026"]

    # Também funciona para e-mails vindos direto do Gmail em ordem inversa (e mesmo UTC 01h de 18/11 = 22h BRT de 17/11)
    emails_gmail = [
        {"id": "m2", "assunto": "the news: quarta", "remetente": "thenews@x.com", "data": "Wed, 18 Nov 2026 06:00:00 -0300", "conteudo_html": "<p>Manhã</p>"},
        {"id": "m1", "assunto": "the news 🌖 NIGHT #352", "remetente": "thenews@x.com", "data": "Wed, 18 Nov 2026 01:00:00 +0000", "conteudo_html": "<p>Noite</p>"},
    ]
    cfgs_gmail = pipe.preparar_grupos(emails_gmail)
    assert [e["id"] for e in cfgs_gmail[0].emails] == ["m1", "m2"]


def test_consolidar_secoes_mesmo_assunto_concatena_e_absorve_bullets_sem_perder_numeros():
    from espelho_magico.pipeline import consolidar_secoes_mesmo_assunto_detalhado

    md = (
        "# 18 de novembro de 2026\n\n"
        "## Need to Know\n\n"
        "- **→** Nvidia avalia retorno de executivo ao conselho\n"
        "- **→** Crise logística da Apple afeta 40 mil envios de iPhone nas fábricas\n"
        "- **→** Greve portuária afeta 12 navios no porto sul\n"
        "- **→** Nova linha de metrô abre hoje\n\n"
        "## Crise na logística da Apple na Ásia\n\n"
        "A Apple revisou os envios de iPhone após atrasos nas fábricas de fornecedores na Ásia.\n\n"
        "## Giro pelo mundo\n\n"
        "**Nvidia:** O conselho debate o retorno de seu fundador em reunião extraordinária na Califórnia.\n\n"
        "## Atrasos na logística da Apple\n\n"
        "Os fornecedores da Apple confirmaram que os envios de iPhone serão normalizados nas fábricas até sexta-feira.\n\n"
        "## Enquete do Dia\n\nQual tema importa mais?\n\n- Logística\n- Porto\n- Metrô\n- Outro\n"
    )
    consolidado, info = consolidar_secoes_mesmo_assunto_detalhado(md)
    # As duas seções da Apple sobre o mesmo assunto devem concatenar em uma só com subtópico ###
    assert len(info["secoes_concatenadas"]) == 1
    h2s = [l for l in consolidado.splitlines() if l.startswith("## ")]
    assert len(h2s) == 4  # Need to Know, Apple, Giro, Enquete
    assert "atrasos nas fábricas de fornecedores na Ásia" in consolidado
    assert "normalizados nas fábricas até sexta-feira" in consolidado
    assert "### Atrasos na logística da Apple" in consolidado
    # Bullet curto sem número novo já coberto em Giro é absorvido; bullet com número inédito ('40') é preservado
    assert "Nvidia avalia retorno" not in consolidado
    assert "40 mil envios" in consolidado
    assert "12 navios" in consolidado
    assert "Nova linha de metrô abre hoje" in consolidado


def test_apresentacao_terminal_plano_e_resumo():
    from rich.console import Console
    from espelho_magico.pipeline import (
        ConfigGrupo,
        ResultadoGrupo,
        _resumo_datas_grupo,
        _tabela_plano,
        _tabela_resumo,
    )

    emails = [
        {"id": None, "assunto": "Boletim NIGHT", "data": "Tue, 17 Nov 2026 09:00:00 +0000"},
        {"id": "g1", "assunto": "Boletim manhã", "data": "Wed, 18 Nov 2026 06:00:00 -0300"},
    ]
    assert _resumo_datas_grupo(emails) == "17/11 Noite, 18/11"

    cfg = ConfigGrupo(chave="1440", emails=emails, titulo_base="Boletim Fora de Ordem", nome_final="Boletim Fora de Ordem #323.md", unificar=True)
    res = [ResultadoGrupo(nome="Boletim Fora de Ordem #323", status="ok", mensagem="OK", resumo="Enviado", segundos=65.0, noticias=14, verificacao="9 ✔", caracteres=15360)]

    buf = Console(record=True, width=160)
    buf.print(_tabela_plano([cfg]))
    buf.print(_tabela_resumo(res, 65.0))
    texto = buf.export_text()
    assert "Edições · Datas" in texto and "17/11 Noite, 18/11" in texto and "unificado" in texto
    assert "Tamanho" in texto and "15,0 KB" in texto and "14 notícia(s)" in texto


def test_thenews_fallback_url_direta_quando_arquivo_desatualizado(tmp_path):
    hoje = datetime.date.today()
    ontem = hoje - datetime.timedelta(days=1)
    (tmp_path / "Gol Contra #203.md").write_text(
        f"# Gol Contra #203 — {ontem:%d/%m/%Y}\n\n## Ontem\n\nTexto.\n",
        encoding="utf-8",
    )

    # Arquivo principal com cache atrasado mostrando só a edição de ontem
    url_arquivo = "https://thenews.com.br/newsletter/sports"
    url_hoje = f"{url_arquivo}/{hoje:%d-%m-%Y}"
    paginas = {
        url_arquivo: (
            f'<html><body><a href="/newsletter/sports/{ontem:%d-%m-%Y}">{ontem:%d-%m-%Y}</a></body></html>'
        ),
        url_hoje: (
            f"<html><body><h1>Flamengo ganha reforço hoje</h1>"
            f"<p>Por the news sports · {hoje:%d-%m-%Y}</p><p>Conteúdo completo da edição de hoje.</p></body></html>"
        ),
    }

    class Resp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    cabecalhos_vistos = []

    def fake_get(url, headers=None, timeout=20):
        cabecalhos_vistos.append(headers or {})
        if url in paginas:
            return Resp(200, paginas[url])
        return Resp(404)

    fontes = {"tns sports": {"tipo": "thenews", "arquivo_url": url_arquivo}}
    mapeamento = {"tns sports": "Gol Contra"}
    recuperados = buscar_edicoes_faltantes_na_web(
        emails_atuais=[],
        pasta_resultados=tmp_path,
        fontes_web=fontes,
        mapeamento=mapeamento,
        get=fake_get,
    )
    assert len(recuperados) == 1
    assert recuperados[0]["remetente"] == "tns sports"
    assert "Flamengo ganha reforço hoje" in recuperados[0]["assunto"]
    assert any(h.get("Cache-Control") == "no-cache" for h in cabecalhos_vistos)


def test_consolidacao_nao_funde_fincher_com_charlie_cox_e_remove_rodape_seo_curto():
    from espelho_magico.pipeline import consolidar_secoes_mesmo_assunto_detalhado

    md = (
        "# 23 de novembro de 2026\n\n"
        "## David Fincher exalta James Gunn e revela convite para filme do Batman nos anos 90\n\n"
        "David Fincher contou que apresentou uma proposta sombria para o Batman nos anos 90, "
        "enquanto o universo paralelo de Matt Reeves conta com Robert Pattinson no papel central.\n\n"
        "## Charlie Cox revela ter sido barrado nos testes para James Bond por idade\n\n"
        "Charlie Cox contou que foi considerado velho demais para 007; a Amazon MGM escolheu "
        "Robert Pattinson, atual intérprete do Batman, para estrelar Bond 26.\n\n"
        "## GTA VI: Fãs encontram Pennywise no jogo\n\n"
        "Jogadores encontraram uma referência a Pennywise em um elevador cheio de balões.\n\n"
        "GTA VI encerrou um hiato de treze anos da franquia.\n\n"
        "GTA VI está disponível para PS5 e Xbox Series X|S.\n\n"
        "## GTA VI: Como encontrar o carro de Toretto\n\n"
        "O Dodge Charger R/T 1970 pode ser encontrado atrás da casa de Sid Thompson em Grayson.\n\n"
        "GTA VI encerrou um hiato de treze anos da franquia.\n\n"
        "GTA VI está disponível para PlayStation 5 e Xbox Series X/S.\n"
    )
    sanitizado = sanitizar_markdown_final(md, consolidar=False)
    consolidado, info = consolidar_secoes_mesmo_assunto_detalhado(sanitizado)
    assert info["secoes_concatenadas"] == []
    assert "## David Fincher exalta James Gunn" in consolidado
    assert "## Charlie Cox revela ter sido barrado" in consolidado
    # Rodapés curtos repetidos mantidos apenas na primeira ocorrência
    assert consolidado.count("encerrou um hiato de treze anos") == 1
    assert consolidado.lower().count("está disponível para") == 1


def test_pipeline_segunda_passagem_reextrai_parte_rebaixada_e_audita_estrutura(tmp_path, monkeypatch):
    from espelho_magico import config

    monkeypatch.setattr(config, "MAX_CHARS_POR_CHAMADA", 1000)
    monkeypatch.setattr(config, "DUPLA_PASSAGEM_REVISAO", True)

    class ExtratorComDowngrade:
        def __init__(self):
            self.chamadas = []
            self._ultimos = []

        def resetar_modelos_usados(self):
            self._ultimos = []

        def obter_modelos_usados(self):
            return list(self._ultimos)

        def extrair(self, conteudo, prompt, apenas_tier_principal=False):
            self.chamadas.append((apenas_tier_principal, prompt[:60]))
            # 1ª chamada (parte 1): cai em modelo lite e vaza anúncio da NordVPN
            if len(self.chamadas) == 1:
                self._ultimos = ["gemini-flash-lite-latest"]
                return (
                    "# 23 de novembro de 2026\n\n"
                    "## Cupom NordVPN com desconto\n\n"
                    "Assine a NordVPN com 75% de desconto na Black Friday.\n"
                )
            # 2ª chamada (parte 2): modelo principal extrai notícia real com subtítulo separado
            if len(self.chamadas) == 2:
                self._ultimos = ["gemini-3.6-flash"]
                return (
                    "# 23 de novembro de 2026\n\n"
                    "## Galaxy S27 Ultra ganha modo de envio seguro\n\n"
                    "A Samsung testa recurso que limita a bateria de 5.534 mAh a 80% por 14 dias antes do envio à assistência técnica.\n\n"
                    "## Detalhes do envio seguro no aparelho\n\n"
                    "O ajuste preserva as configurações anteriores do usuário quando desativado após o retorno do reparo.\n"
                )
            # 3ª chamada: 2ª passagem da parte 1 (apenas_tier_principal=True) descarta o anúncio!
            if "segunda_passagem_revisao" in prompt:
                assert apenas_tier_principal is True
                self._ultimos = ["gemini-3.6-flash"]
                return "[SEM_CONTEUDO_EDITORIAL]"
            # 4ª chamada: auditoria estrutural compacta em JSON anexa a seção 2 na seção 1
            if "revisor estrutural" in prompt.lower():
                assert apenas_tier_principal is True
                self._ultimos = ["gemini-3.6-flash"]
                return '{"anexar_ao_anterior": [2], "remover_secoes_comerciais": [], "promover_subtitulos": []}'
            # 5ª chamada: criação de Enquete do Dia
            self._ultimos = ["gemini-3.6-flash"]
            return "## Enquete do Dia\n\nVocê confia em enviar celular pelos Correios?\n\n- Sim\n- Nunca\n- Só com seguro\n- Levo pessoalmente"

    extrator = ExtratorComDowngrade()
    pipe = Pipeline(Opcoes(dry_run=True, max_workers=1), extrator=extrator, drive_service=None, gmail_service=None)
    pipe.pasta_saida = tmp_path

    html_grande = (
        "<h2>Cupom NordVPN: como assinar a melhor VPN com até 75% de desconto</h2>"
        "<p>23/11/2026 às 08:30</p>"
        "<p>" + ("Oferta promocional da Black Friday com cupom de desconto especial. " * 10) + "</p>"
        "<h2>Galaxy S27 Ultra ganha modo de envio seguro</h2>"
        "<p>23/11/2026 às 13:47</p>"
        "<p>A Samsung testa recurso que limita a bateria de 5.534 mAh a 80% por 14 dias. "
        + ("Detalhes técnicos da bateria e envio à assistência. " * 10) + "</p>"
    )
    cfg = ConfigGrupo(
        chave="notícias de segurança",
        emails=[{
            "id": None,
            "assunto": "Notícias de Segurança - 23/11",
            "remetente": "feed@local",
            "data": "Mon, 23 Nov 2026 18:00:00 +0000",
            "conteudo_html": html_grande,
            "eh_html": True,
        }],
        titulo_base="Hackeando o Problema",
        nome_final="Hackeando o Problema #215.md",
    )
    resultados = pipe.executar([cfg])
    assert resultados[0].status == "dry"
    gerado = (tmp_path / "Hackeando o Problema #215.md").read_text(encoding="utf-8")
    assert "NordVPN" not in gerado
    assert "## Galaxy S27 Ultra ganha modo de envio seguro" in gerado
    assert "\n### Detalhes do envio seguro no aparelho" in gerado
    assert "\n## Detalhes do envio seguro no aparelho" not in gerado
    assert "## Enquete do Dia" in gerado
    assert not resultados[0].tem_aviso


def test_consolidacao_anexa_na_secao_mais_relevante_e_nao_na_primeira_introducao():
    from espelho_magico.pipeline import consolidar_secoes_mesmo_assunto_detalhado

    md = (
        "# 23 de novembro de 2026\n\n"
        "## O perigo da concordância e falta de conflitos produtivos\n\n"
        "Em reuniões corporativas, profissionais preferem o silêncio para evitar conflitos produtivos desnecessários.\n\n"
        "## Exercício de reflexão com Inteligência Artificial\n\n"
        "Um prompt semanal ajuda a refletir sobre padrões de carreira usando memória da IA.\n\n"
        "## Por que o excesso de cordialidade impede conflitos produtivos na liderança\n\n"
        "Rebecca Newton, psicóloga organizacional, explica como equipes executivas evitam decisões difíceis e apresenta "
        "quatro práticas para estimular conflitos produtivos, confiança interpessoal e discussões saudáveis.\n\n"
        "## Práticas para tornar reuniões e conflitos produtivos melhores\n\n"
        "Rebecca recomenda substituir atualizações por perguntas nas reuniões das equipes executivas, desenvolver "
        "confiança interpessoal e estimular discussões saudáveis sobre decisões difíceis.\n"
    )
    out, info = consolidar_secoes_mesmo_assunto_detalhado(md)
    assert len(info["secoes_concatenadas"]) == 1
    assert "excesso de cordialidade" in info["secoes_concatenadas"][0].lower()
    # A continuação das práticas deve aparecer DEPOIS do exercício de IA e junto de Rebecca Newton
    assert out.index("## Exercício de reflexão") < out.index("## Por que o excesso de cordialidade") < out.index("### Práticas para tornar reuniões")


def test_pares_fechamento_standby_e_pareamento_terca_quinta(tmp_path):
    (tmp_path / "Fechamento de Caixa #178.md").write_text("# Fechamento de Caixa #178 — 23 de novembro de 2026\n", encoding="utf-8")

    def make_pipe(hoje, forcar_pares=False):
        pipe = Pipeline(Opcoes(dry_run=True, interativo=False, forcar_pares=forcar_pares), extrator=None, drive_service=None, gmail_service=None)
        pipe.pasta_historico = tmp_path
        pipe._hoje_ref = hoje
        return pipe

    # Segunda (23/11): tns money sai sozinho imediatamente
    email_seg = {"id": "m1", "assunto": "tns money: bear market", "remetente": "tns money", "data": "Mon, 23 Nov 2026 09:00:00 +0000", "conteudo_html": "<p>x</p>"}
    pipe_seg = make_pipe(datetime.date(2026, 11, 23))
    cfgs = pipe_seg.preparar_grupos([email_seg])
    assert len(cfgs) == 1 and not pipe_seg.em_standby
    assert cfgs[0].chave == "tns money" and not cfgs[0].unificar

    # Terça (24/11) às 08h: só tns money chegou -> entra em stand by aguardando moneydrops
    email_ter_tns = {"id": "m2", "assunto": "tns money: jensen", "remetente": "tns money", "data": "Tue, 24 Nov 2026 09:00:00 +0000", "conteudo_html": "<p>x</p>"}
    pipe_ter = make_pipe(datetime.date(2026, 11, 24))
    cfgs = pipe_ter.preparar_grupos([email_ter_tns])
    assert cfgs == []
    assert len(pipe_ter.em_standby) == 1
    assert pipe_ter.em_standby[0].aguardando == "moneydrops"
    assert pipe_ter.em_standby[0].data_esperada == datetime.date(2026, 11, 24)

    # Terça (24/11) com --forcar-pares -> libera imediatamente
    pipe_force = make_pipe(datetime.date(2026, 11, 24), forcar_pares=True)
    cfgs_force = pipe_force.preparar_grupos([dict(email_ter_tns)])
    assert len(cfgs_force) == 1 and not pipe_force.em_standby

    # Quarta (25/11): stand by de terça expirou (1 dia) -> libera sozinho
    pipe_exp = make_pipe(datetime.date(2026, 11, 25))
    cfgs_exp = pipe_exp.preparar_grupos([dict(email_ter_tns)])
    assert len(cfgs_exp) == 1 and not pipe_exp.em_standby

    # Terça (24/11) com PAR completo (tns money + moneydrops) -> 1 edição unificada com dicas de ambas
    email_ter_md = {"id": None, "assunto": "moneydrops: CEO", "remetente": "moneydrops", "fonte_chave": "moneydrops", "data": "Tue, 24 Nov 2026 12:04:00 +0000", "conteudo_html": "<p>y</p>"}
    pipe_par = make_pipe(datetime.date(2026, 11, 24))
    cfgs_par = pipe_par.preparar_grupos([dict(email_ter_tns), email_ter_md])
    assert len(cfgs_par) == 1 and not pipe_par.em_standby
    assert cfgs_par[0].unificar is True
    assert cfgs_par[0].chaves_fontes == ["tns money", "moneydrops"]
    assert cfgs_par[0].nome_final == "Fechamento de Caixa #179.md"
    prompt = pipe_par._montar_prompt(cfgs_par[0].chave, unificar=True, chaves_fontes=cfgs_par[0].chaves_fontes)
    assert "Pensei no chuveiro" in prompt


def test_pares_ata_opcao_a_better_work_independente_e_dedup_sexta(tmp_path):
    (tmp_path / "Ata de Reunião #128.md").write_text(
        "# Ata de Reunião #128 — quinta-feira, 26 de novembro de 2026\n\n"
        "## OpenAI fecha parceria bilionária com bancos\n\n"
        "A OpenAI anunciou acordo de US$ 10 bilhões com três grandes bancos para automação corporativa.\n\n"
        "## Enquete do Dia\n\nQuem ganha?\n\n- A\n- B\n- C\n- D\n",
        encoding="utf-8",
    )

    def make_pipe(hoje):
        pipe = Pipeline(Opcoes(dry_run=True, interativo=False), extrator=None, drive_service=None, gmail_service=None)
        pipe.pasta_historico = tmp_path
        pipe._hoje_ref = hoje
        return pipe

    # Segunda (23/11): techdrops entra em stand by, enquanto tns better work sai sozinho!
    e_td_seg = {"id": None, "assunto": "techdrops: seg", "remetente": "techdrops", "fonte_chave": "techdrops", "data": "Mon, 23 Nov 2026 09:07:00 +0000", "conteudo_html": "<p>td</p>"}
    e_bw_seg = {"id": "bw1", "assunto": "tns better work: liderança", "remetente": "tns better work", "data": "Mon, 23 Nov 2026 21:18:00 +0000", "conteudo_html": "<p>bw</p>"}
    pipe_seg = make_pipe(datetime.date(2026, 11, 23))
    cfgs_seg = pipe_seg.preparar_grupos([dict(e_td_seg), dict(e_bw_seg)])
    assert len(cfgs_seg) == 1
    assert cfgs_seg[0].chave == "tns better work"
    assert len(pipe_seg.em_standby) == 1
    assert pipe_seg.em_standby[0].fonte_recebida == "techdrops"
    assert pipe_seg.em_standby[0].aguardando == "tns business"
    assert pipe_seg.em_standby[0].data_esperada == datetime.date(2026, 11, 24)

    # Terça (24/11): techdrops (Seg) + tns business (Ter) formam 1 par unificado + tns better work recebe nº sequencial (#129 e #130)
    e_tb_ter = {"id": "tb1", "assunto": "tns business: acordo", "remetente": "tns business", "data": "Tue, 24 Nov 2026 09:00:00 +0000", "conteudo_html": "<p>tb</p>"}
    pipe_ter = make_pipe(datetime.date(2026, 11, 24))
    cfgs_ter = pipe_ter.preparar_grupos([dict(e_td_seg), dict(e_tb_ter), dict(e_bw_seg)])
    assert len(cfgs_ter) == 2 and not pipe_ter.em_standby
    nomes = {c.nome_final for c in cfgs_ter}
    assert nomes == {"Ata de Reunião #129.md", "Ata de Reunião #130.md"}
    cfg_par = next(c for c in cfgs_ter if len(c.chaves_fontes) == 2)
    assert cfg_par.chaves_fontes == ["techdrops", "tns business"]
    assert cfg_par.unificar is True
    prompt_par = pipe_ter._montar_prompt(cfg_par.chave, unificar=True, chaves_fontes=cfg_par.chaves_fontes)
    assert "Hoje eu aprendi" in prompt_par

    # Sexta (27/11): techdrops (Sex) sai sozinho e injeta contexto anti-repetição da edição de quinta (#128, 26/11)
    e_td_sex = {"id": None, "assunto": "techdrops: sex", "remetente": "techdrops", "fonte_chave": "techdrops", "data": "Fri, 27 Nov 2026 09:07:00 +0000", "conteudo_html": "<p>sex</p>"}
    pipe_sex = make_pipe(datetime.date(2026, 11, 27))
    cfgs_sex = pipe_sex.preparar_grupos([e_td_sex])
    assert len(cfgs_sex) == 1 and not pipe_sex.em_standby
    assert "OpenAI fecha parceria bilionária" in cfgs_sex[0].contexto_edicao_anterior
    prompt_sex = pipe_sex._montar_prompt(
        cfgs_sex[0].chave,
        unificar=False,
        chaves_fontes=cfgs_sex[0].chaves_fontes,
        contexto_edicao_anterior=cfgs_sex[0].contexto_edicao_anterior,
    )
    assert "<evitar_repeticao_edicao_anterior>" in prompt_sex
    assert "Hoje eu aprendi" in prompt_sex


def test_web_beehiiv_drops_por_published_time_e_controle_fontes(tmp_path):
    from espelho_magico.pipeline import carregar_controle_fontes, registrar_fontes_processadas

    # Simula Ata #128 de segunda (23/11) sendo tns better work (assunto diferente de techdrops)
    (tmp_path / "Ata de Reunião #128.md").write_text(
        "# Ata de Reunião #128 — segunda-feira, 23 de novembro de 2026\n\n"
        "## Por que o excesso de cordialidade prejudica lideranças\n\n"
        "Rebecca Newton explica a paz passiva em reuniões corporativas.\n",
        encoding="utf-8",
    )
    archive_td = (
        '<html><body>'
        '<a href="/p/o-mundo-em-modo-de-seguranc-a">💧 O mundo em modo de segurança</a>'
        '</body></html>'
    )
    post_td = (
        '<html><head><meta property="article:published_time" content="2026-11-23T09:07:07.000Z"/></head>'
        '<body><h1>O mundo em modo de segurança</h1>'
        '<p>Hoje eu aprendi que o KFC organizou um velório fake para fãs de GTA 6 faltarem ao trabalho.</p>'
        '<p>Palo Alto Networks reportou um dilúvio de vulnerabilidades descobertas por IA 7x mais rápido.</p>'
        '<p>SpaceX levantou nova dívida espacial enquanto concorrentes testam chips em órbita terrestre.</p>'
        '</body></html>'
    )

    class Resp:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

        def raise_for_status(self):
            pass

    paginas = {
        "https://www.techdrops.com.br/archive": archive_td,
        "https://www.techdrops.com.br/p/o-mundo-em-modo-de-seguranc-a": post_td,
    }

    def fake_get(url, headers=None, timeout=20):
        return Resp(paginas[url.split("?")[0]])

    # tns business de terça (24/11) já chegou no Gmail -> busca web deve puxar techdrops de segunda (23/11)!
    email_tb_ter = {
        "id": "tb24",
        "assunto": "tns business: acordo gigante",
        "remetente": "tns business",
        "data": "Tue, 24 Nov 2026 09:00:00 +0000",
        "conteudo_html": "<p>business</p>",
    }
    controle_path = tmp_path / "controle_fontes.json"
    recuperados = buscar_edicoes_faltantes_na_web(
        [email_tb_ter],
        pasta_resultados=tmp_path,
        fontes_web={"techdrops": {"tipo": "beehiiv", "arquivo_url": "https://www.techdrops.com.br/archive"}},
        mapeamento={"tns business": "Ata de Reunião", "techdrops": "Ata de Reunião"},
        get=fake_get,
        caminho_controle_fontes=controle_path,
    )
    assert len(recuperados) == 1
    assert recuperados[0]["fonte_chave"] == "techdrops"
    assert formatar_data_email(recuperados[0]["data"]) == "23 de novembro de 2026"

    # Após registrar em controle_fontes.json, não baixa novamente a mesma URL
    registrar_fontes_processadas(recuperados, "techdrops", caminho=controle_path)
    dados_ctrl = carregar_controle_fontes(controle_path)
    assert dados_ctrl["techdrops"]["ultima_data"] == "2026-11-23"
    recuperados2 = buscar_edicoes_faltantes_na_web(
        [email_tb_ter],
        pasta_resultados=tmp_path,
        fontes_web={"techdrops": {"tipo": "beehiiv", "arquivo_url": "https://www.techdrops.com.br/archive"}},
        mapeamento={"tns business": "Ata de Reunião", "techdrops": "Ata de Reunião"},
        get=fake_get,
        caminho_controle_fontes=controle_path,
    )
    assert recuperados2 == []


def test_integridade_aceita_descarte_de_pauta_repetida_da_edicao_anterior():
    from espelho_magico.validacao import aplicar_dupla_verificacao

    entrada = (
        "## OpenAI fecha parceria bilionária com bancos\n"
        "A OpenAI fechou contrato de US$ 10 bilhões com 3 bancos para 50.000 funcionários em 18 meses, "
        "prevendo ganho de 45% de eficiência até 2028 com investimento adicional de US$ 750 milhões.\n\n"
        "## Nova startup brasileira capta rodada recorde\n"
        "A startup fictícia levantou R$ 120 milhões em série B liderada por dois fundos globais."
    )
    md_extraido = (
        "# 27 de novembro de 2026\n\n"
        "## Nova startup brasileira capta rodada recorde\n\n"
        "A startup fictícia levantou R$ 120 milhões em série B liderada por dois fundos globais.\n\n"
        "## Enquete do Dia\n\nQual é a real?\n\n- A\n- B\n- C\n- D"
    )
    contexto_ant = "Edição anterior: Ata de Reunião #128 (26/11/2026)\n- OpenAI fecha parceria bilionária com bancos: acordo de US$ 10 bilhões."

    prompts_recebidos = []

    def fake_reparar(conteudo, prompt):
        prompts_recebidos.append((conteudo, prompt))
        return "[APENAS_RUIDO_DESCARTADO] matéria da OpenAI já coberta na edição anterior de quinta-feira"

    _, rel = aplicar_dupla_verificacao(
        md_extraido,
        entrada,
        idioma="pt",
        reparar=fake_reparar,
        contexto_edicao_anterior=contexto_ant,
    )
    assert prompts_recebidos
    assert "<edicao_anterior_ja_coberta>" in prompts_recebidos[0][0]
    assert not rel.tem_aviso


def test_boletim_night_sozinha_pergunta_e_respeita_standby(tmp_path):
    (tmp_path / "Boletim Fora de Ordem #331.md").write_text(
        "# Boletim Fora de Ordem #331 — quinta-feira, 26 de novembro de 2026\n",
        encoding="utf-8",
    )
    email_night = {
        "id": None,
        "assunto": "the news: NIGHT #359",
        "remetente": "the news",
        "data": "Thu, 26 Nov 2026 09:00:00 +0000",
        "url": "https://thenewscc.beehiiv.com/p/night-359",
        "conteudo_html": "<p>Noite</p>",
    }
    email_manha = {
        "id": "m27",
        "assunto": "the news: Hollow Knight ganha sequência",
        "remetente": "the news",
        "data": "Fri, 27 Nov 2026 06:00:00 -0300",
        "conteudo_html": "<p>Manhã</p>",
    }

    def make_pipe(hoje, interativo=True, forcar_pares=False, urls=None, resposta=""):
        pipe = Pipeline(
            Opcoes(dry_run=True, interativo=interativo, forcar_pares=forcar_pares, urls=urls or []),
            extrator=None,
            drive_service=None,
            gmail_service=None,
        )
        pipe.pasta_historico = tmp_path
        pipe._hoje_ref = hoje
        pipe._perguntas = []

        def fake_perguntar(msg):
            pipe._perguntas.append(msg)
            return resposta

        pipe._perguntar = fake_perguntar
        return pipe

    # Interativo respondendo Enter/N -> entra em stand by aguardando 27/11
    pipe_aguardar = make_pipe(datetime.date(2026, 11, 26), interativo=True, resposta="")
    cfgs = pipe_aguardar.preparar_grupos([dict(email_night)])
    assert cfgs == []
    assert len(pipe_aguardar._perguntas) == 1
    assert "edição noturna (26/11)" in pipe_aguardar._perguntas[0]
    assert "27/11" in pipe_aguardar._perguntas[0]
    assert len(pipe_aguardar.em_standby) == 1
    assert pipe_aguardar.em_standby[0].titulo_base == "Boletim Fora de Ordem"
    assert pipe_aguardar.em_standby[0].data_esperada == datetime.date(2026, 11, 27)

    # Interativo respondendo 's' -> extrai sozinha imediatamente como #332
    pipe_sim = make_pipe(datetime.date(2026, 11, 26), interativo=True, resposta="s")
    cfgs_sim = pipe_sim.preparar_grupos([dict(email_night)])
    assert len(cfgs_sim) == 1 and not pipe_sim.em_standby
    assert cfgs_sim[0].nome_final == "Boletim Fora de Ordem #332.md"
    assert len(pipe_sim._perguntas) == 1

    # --forcar-pares -> extrai imediatamente sem perguntar
    pipe_forcar = make_pipe(datetime.date(2026, 11, 26), interativo=True, forcar_pares=True)
    cfgs_forcar = pipe_forcar.preparar_grupos([dict(email_night)])
    assert len(cfgs_forcar) == 1 and not pipe_forcar._perguntas

    # --url explícita -> extrai imediatamente sem perguntar
    pipe_url = make_pipe(
        datetime.date(2026, 11, 26),
        interativo=True,
        urls=["https://thenewscc.beehiiv.com/p/night-359"],
    )
    cfgs_url = pipe_url.preparar_grupos([dict(email_night)])
    assert len(cfgs_url) == 1 and not pipe_url._perguntas

    # Não interativo (--sem-interacao): antes de expirar entra em stand by; após 1 dia da manhã esperada libera
    pipe_auto = make_pipe(datetime.date(2026, 11, 26), interativo=False)
    assert pipe_auto.preparar_grupos([dict(email_night)]) == []
    assert len(pipe_auto.em_standby) == 1

    pipe_exp = make_pipe(datetime.date(2026, 11, 28), interativo=False)
    cfgs_exp = pipe_exp.preparar_grupos([dict(email_night)])
    assert len(cfgs_exp) == 1 and not pipe_exp.em_standby

    # Par completo (NIGHT + manhã) e edição matinal com 'Knight' no assunto -> NÃO perguntam sobre NIGHT sozinha
    pipe_par = make_pipe(datetime.date(2026, 11, 27), interativo=True, resposta="n")
    cfgs_par = pipe_par.preparar_grupos([dict(email_night), dict(email_manha)])
    assert len(cfgs_par) == 1 and not pipe_par.em_standby
    assert not any("edição noturna" in p for p in pipe_par._perguntas)

    pipe_manha = make_pipe(datetime.date(2026, 11, 27), interativo=True, resposta="n")
    cfgs_manha = pipe_manha.preparar_grupos([dict(email_manha)])
    assert len(cfgs_manha) == 1 and not pipe_manha.em_standby and not pipe_manha._perguntas













