from espelho_magico.drive_client import montar_query
from espelho_magico.pipeline import (
    Opcoes,
    Pipeline,
    agrupar_emails,
    deduplicar_secoes,
    eh_extracao_vazia_ou_invalida,
    formatar_data_email,
    inserir_apos_h1,
    maior_edicao_existente,
    nome_fallback,
    sanitizar_markdown_final,
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

