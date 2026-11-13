import datetime

from espelho_magico.pipeline import extrair_data_de_texto
from espelho_magico.validacao import (
    AVISO,
    CORRIGIDO,
    OK,
    aplicar_dupla_verificacao,
    contar_noticias,
    converter_tabelas_em_topicos,
    reorganizar_bloco_final,
    remover_links_e_imagens,
)


def _status(rel, prefixo):
    return next(c.status for c in rel.checagens if c.regra.startswith(prefixo))


def test_converte_tabela_em_topicos_sem_perder_dados():
    md = (
        "## Fechamento de Mercado\n\n"
        "| Ativo | Fechamento | 1 dia | YTD |\n| --- | --- | --- | --- |\n"
        "| Ibovespa | 172.236,05 | +2,22% | +6,90% |\n| Dólar | R$ 5,51 | +0,22% | -0,17% |\n\n"
        "Texto depois."
    )
    saida, n = converter_tabelas_em_topicos(md, "pt")
    assert n == 1
    assert "|" not in saida
    assert "- **Ibovespa:** Fechamento: 172.236,05 · no dia: +2,22% · no ano: +6,90%" in saida
    assert "- **Dólar:** Fechamento: R$ 5,51 · no dia: +0,22% · no ano: -0,17%" in saida
    assert saida.rstrip().endswith("Texto depois.")


def test_remove_links_e_imagens():
    md = "## Nota\n\nVeja [o trailer](https://x.com/a) aqui.\n\n![grafico](https://img/1.png)\n\nMais em https://site.com/abc"
    saida, n = remover_links_e_imagens(md)
    assert n == 3
    assert "https" not in saida and "![" not in saida
    assert "Veja o trailer aqui." in saida


def test_reorganiza_voz_e_enquete_para_o_final_e_adapta_leitores():
    md = (
        "# 10 de novembro de 2026\n\n"
        "## Enquete do Dia\n\nO futebol ficou chato?\n\n- Sim\n- Não\n\n"
        "## Resultado da Enquete de Ontem\n\n- 32% Palmeiras: \"comentário de leitor no The News\"\n\n"
        "## Notícia Principal\n\nTexto da notícia.\n\n"
        "## Opinião do Leitor\n\nEnquete votada por 2.230 leitores sobre a newsletter.\n"
    )
    saida, info = reorganizar_bloco_final(md, "pt")
    titulos = [l for l in saida.split("\n") if l.startswith("## ")]
    assert titulos == ["## Notícia Principal", "## Voz dos Ouvintes", "## Enquete do Dia"]
    assert "### Resultado da Enquete de Ontem" in saida and "### Opinião do Ouvinte" in saida
    assert "2.230 ouvintes" in saida and "comentário de ouvinte no programa" in saida
    assert "sobre a edição." in saida
    assert info["mudou_ordem"] and info["opcoes_enquete"] == 2


def test_dupla_verificacao_cria_enquete_com_ia_quando_fonte_nao_tem():
    md = "# 10 de novembro de 2026\n\n## GTA VI ganha festival\n\nFestival em Miami.\n\n## Until Dawn 2 sem coop\n\nSó singleplayer."
    chamadas = []

    def gerar(conteudo, prompt):
        chamadas.append(prompt)
        return "## Enquete do Dia\n\nQual é o maior absurdo da semana?\n\n- Festival de GTA\n- Until Dawn solo\n- Os dois\n- Nenhum"

    saida, rel = aplicar_dupla_verificacao(md, entrada=md, idioma="pt", gerar=gerar, extrair_data=extrair_data_de_texto)
    assert chamadas, "deveria pedir a enquete à IA"
    assert saida.split("\n## ")[-1].startswith("Enquete do Dia")
    assert "- Festival de GTA" in saida
    assert _status(rel, "Enquete do Dia") == CORRIGIDO
    assert _status(rel, "Cabeçalho") == OK
    assert contar_noticias(saida) == 2


def test_dupla_verificacao_usa_fallback_se_ia_falhar_e_respeita_ingles():
    md = "# November 10, 2026\n\n## Markets\n\n| Asset | Value |\n| --- | --- |\n| TSX | 35,789 |\n\n## Nvidia Probe\n\nText."

    def gerar(conteudo, prompt):
        raise RuntimeError("503")

    saida, rel = aplicar_dupla_verificacao(md, entrada=md, idioma="en", gerar=gerar, extrair_data=extrair_data_de_texto)
    assert "## Daily Poll" in saida and "- Markets" in saida and "- Nvidia Probe" in saida
    assert "- **TSX:** Value: 35,789" in saida
    assert _status(rel, "Tabelas") == CORRIGIDO
    assert _status(rel, "Idioma") == OK


def test_integridade_avisa_quando_numeros_somem():
    entrada = "Dados: 10% 20% 30% 40% 50% 60% 70% 80% 90% 15% 25%"
    md = "# 10 de novembro de 2026\n\n## Resumo\n\nDados: 10% 20%\n\n## Enquete do Dia\n\nPergunta?\n\n- A\n- B"
    _, rel = aplicar_dupla_verificacao(md, entrada=entrada, idioma="pt", extrair_data=extrair_data_de_texto)
    assert _status(rel, "Integridade") == AVISO
    assert extrair_data_de_texto("# Gol Contra #198 — 10 de novembro de 2026") == datetime.date(2026, 11, 10)


def test_ruido_e_code_switching_evitam_falsos_positivos():
    md_limpo = (
        "# 13 de novembro de 2026\n\n"
        "## Projeto no Congresso\n\n"
        "O projeto apresentado por deputados prevê que o serviço é streaming e o mercado é marketing puro.\n\n"
        "## Enquete do Dia\n\nQual sua opinião?\n\n- A\n- B\n- C\n"
    )
    _, rel_limpo = aplicar_dupla_verificacao(md_limpo, idioma="pt", extrair_data=extrair_data_de_texto)
    assert _status(rel_limpo, "Ruído") == OK
    assert _status(rel_limpo, "Idioma") == OK

    md_sujo = (
        "# 13 de novembro de 2026\n\n"
        "## Filme Novo\n\n"
        "O filme é directed por alguém.\n\n"
        "Oferecimento: Marca X\n\n"
        "## Enquete do Dia\n\nQual sua opinião?\n\n- A\n- B\n"
    )
    corrigido, rel_sujo = aplicar_dupla_verificacao(md_sujo, idioma="pt", extrair_data=extrair_data_de_texto)
    assert _status(rel_sujo, "Ruído") == CORRIGIDO
    assert "Oferecimento" not in corrigido
    assert _status(rel_sujo, "Idioma") == AVISO

    corrigido_ia, rel_ia = aplicar_dupla_verificacao(
        md_sujo,
        idioma="pt",
        extrair_data=extrair_data_de_texto,
        reparar=lambda txt, _p: txt.replace("é directed", "é dirigido"),
    )
    assert _status(rel_ia, "Idioma") == CORRIGIDO
    assert "é dirigido" in corrigido_ia


def test_integridade_ignora_metadados_timestamps_leia_tambem_e_extenso():
    entrada = (
        "<metadados>\n[METADADOS DO E-MAIL — Data do e-mail: 13 de novembro de 2026 | Assunto: Boletim - 13/11]\n</metadados>\n\n"
        "# Notícias\n\nGerado em 13/11/2026 16:50\n\n"
        "## Estúdio vende milhões\n\n"
        "13/11/2026 às 16:33\n\n1–2 minutos\n\n"
        "A empresa vendeu 10,31 milhões de cópias em 12 meses, alta de 15% frente a 2025.\n\n"
        "### Leia também sobre o jogo:\n\n"
        "- Jogo é retirado de 1.105 lojas após polêmica\n"
        "- Expansão custa R$ 299 no lançamento\n"
    )
    md = (
        "# 13 de novembro de 2026\n\n"
        "## Estúdio vende milhões\n\n"
        "A empresa vendeu 10,31 milhões de cópias em doze meses, alta de 15% frente a 2025.\n\n"
        "## Enquete do Dia\n\nFaz sentido?\n\n- Sim\n- Não\n"
    )
    _, rel = aplicar_dupla_verificacao(md, entrada=entrada, idioma="pt", extrair_data=extrair_data_de_texto)
    assert _status(rel, "Integridade") == OK
    assert not rel.tem_aviso


def test_integridade_recupera_omissoes_com_ia():
    entrada = (
        "## Resultados financeiros\n\n"
        "A receita cresceu 25%, chegando a 140 milhões, com 380 mil clientes e margem de 18%, "
        "enquanto a dívida caiu 42% para 95 milhões."
    )
    md = (
        "# 13 de novembro de 2026\n\n"
        "## Resultados financeiros\n\n"
        "A receita cresceu 25% neste trimestre.\n\n"
        "## Enquete do Dia\n\nBom resultado?\n\n- Sim\n- Não\n"
    )

    def reparar_fatos(_txt, _prompt):
        return (
            "# 13 de novembro de 2026\n\n"
            "## Resultados financeiros\n\n"
            "A receita cresceu 25%, chegando a 140 milhões, com 380 mil clientes e margem de 18%, "
            "enquanto a dívida caiu 42% para 95 milhões.\n\n"
            "## Enquete do Dia\n\nBom resultado?\n\n- Sim\n- Não\n"
        )

    final, rel = aplicar_dupla_verificacao(
        md, entrada=entrada, idioma="pt", extrair_data=extrair_data_de_texto, reparar=reparar_fatos,
    )
    assert _status(rel, "Integridade") == CORRIGIDO
    assert "380 mil" in final
    assert not rel.tem_aviso


def test_corrige_cabecalho_h1_multiplo_e_enquete_incompleta():
    entrada = "Data do e-mail: 13 de novembro de 2026\n\nTexto qualquer"
    md = (
        "Introdução antes do cabeçalho.\n\n"
        "# Título sobrando\n\nMais texto.\n\n"
        "## Enquete do Dia\n\nPergunta 1?\n\n- Única opção\n"
    )
    final, rel = aplicar_dupla_verificacao(
        md,
        entrada=entrada,
        idioma="pt",
        extrair_data=extrair_data_de_texto,
        gerar=lambda _c, _p: "## Enquete do Dia\n\nQual sua escolha?\n\n- A\n- B\n- C",
    )
    assert final.startswith("# 13 de novembro de 2026\n")
    assert _status(rel, "Cabeçalho") == CORRIGIDO
    assert "## Título sobrando" in final
    assert _status(rel, "Notícias") == OK
    assert _status(rel, "Enquete do Dia") == CORRIGIDO
    assert not rel.tem_aviso


def test_promove_subtitulos_h3_quando_faltam_h2():
    md = (
        "# 13 de novembro de 2026\n\n"
        "### Primeira notícia\n\nTexto 1.\n\n"
        "### Segunda notícia\n\nTexto 2.\n\n"
        "## Enquete do Dia\n\nQual?\n\n- A\n- B\n"
    )
    final, rel = aplicar_dupla_verificacao(md, idioma="pt", extrair_data=extrair_data_de_texto)
    assert "## Primeira notícia" in final
    assert "## Segunda notícia" in final
    assert _status(rel, "Notícias") == CORRIGIDO
    assert not rel.tem_aviso


def test_unifica_multiplas_enquetes_no_mesmo_bloco():
    md = (
        "# 13 de novembro de 2026\n\n"
        "## Notícia única\n\nDetalhes da notícia.\n\n"
        "## Enquete do Dia\n\nPrimeira pergunta?\n\n- Sim\n- Não\n\n"
        "Segunda pergunta?\n\n- Talvez\n- Nunca\n"
    )
    final, rel = aplicar_dupla_verificacao(md, idioma="pt", extrair_data=extrair_data_de_texto)
    assert _status(rel, "Enquete do Dia") == CORRIGIDO
    assert "Segunda pergunta?" not in final
    assert not rel.tem_aviso

