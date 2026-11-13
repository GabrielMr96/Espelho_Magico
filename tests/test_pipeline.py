import datetime

from espelho_magico.drive_client import montar_query
from espelho_magico.pipeline import (
    ConfigGrupo,
    Opcoes,
    Pipeline,
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
    assert com_cabecalho.splitlines()[0] == "# Controle Quebrado #274 — 10 de novembro de 2026"
    # Idempotente se aplicado novamente
    assert aplicar_cabecalho_edicao(com_cabecalho, "Controle Quebrado #274.md") == com_cabecalho

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
    assert gerado.startswith("# Fechamento de Caixa #170 — 13 de novembro de 2026")
    assert "- **Ibovespa:** Valor: 172.000" in gerado
    assert "## Enquete do Dia" in gerado and "- A" in gerado
    assert "|" not in gerado





