import datetime
import io
import re
from pathlib import Path

from rich.console import Console
from rich.text import Text

from espelho_magico.llm import MIN_CHARS_PARA_DIVIDIR
from espelho_magico.pipeline import (
    ConfigGrupo,
    Opcoes,
    Pipeline,
    extrair_data_de_texto,
    sanitizar_markdown_final,
)
from espelho_magico.validacao import (
    AVISO,
    CORRIGIDO,
    OK,
    _contexto_para_enquete,
    _montar_bloco_enquete,
    aplicar_dupla_verificacao,
    contar_noticias,
    converter_tabelas_em_topicos,
    renderizar_relatorio,
    reorganizar_bloco_final,
    remover_links_e_imagens,
)


def _checagem(rel, prefixo):
    return next(c for c in rel.checagens if c.regra.startswith(prefixo))


def _status(rel, prefixo):
    return _checagem(rel, prefixo).status


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
    c_enq = _checagem(rel, "Enquete do Dia")
    assert c_enq.status == CORRIGIDO
    assert "2 enquetes" in c_enq.encontrado
    assert "Primeira pergunta?" in c_enq.corrigido
    assert "Segunda pergunta?" not in final
    assert not rel.tem_aviso


def test_relatorio_detalha_encontrado_e_corrigido_e_notifica_em_tempo_real():
    entrada = (
        "## Resultados financeiros\n\n"
        "A receita cresceu 25%, chegando a 140 milhões, com 380 mil clientes e margem de 18%, "
        "enquanto a dívida caiu 42% para 95 milhões."
    )
    md = (
        "Abertura antes do H1.\n\n"
        "## Enquete de hoje\n\nQual sua opinião?\n\n- Só uma opção\n\n"
        "## Opinião do Leitor\n\nTexto dos leitores sobre a newsletter.\n\n"
        "## Resultado da Enquete de Ontem\n\n60% votaram sim.\n\n"
        "## Resultados financeiros\n\n"
        "A receita cresceu 25% e o lucro é directed pela gestão — veja [nota oficial](https://x.com/a).\n\n"
        "![foto](https://img/a.png)\n\n"
        "| Ativo | Fechamento |\n| --- | --- |\n| Ibovespa | 172.000 |\n| Dólar | R$ 5,50 |\n\n"
        "Oferecimento: Patrocinador X\n\n"
        "# Título [Extra] sobrando\n\nMais texto.\n"
    )
    eventos = []

    def notificar(evento, regra, **dados):
        eventos.append((evento, regra, dados))

    def reparar(conteudo, prompt):
        if "idioma" in prompt.lower():
            return conteudo.replace("é directed", "é dirigido")
        return (
            "# 13 de novembro de 2026\n\n"
            "## Resultados financeiros\n\n"
            "A receita cresceu 25%, chegando a 140 milhões, com 380 mil clientes e margem de 18%, "
            "enquanto a dívida caiu 42% para 95 milhões e o lucro é dirigido pela gestão — veja nota oficial.\n\n"
            "## Mercado financeiro\n\nNova seção restaurada.\n\n"
            "- **Ibovespa:** Fechamento: 172.000\n- **Dólar:** Fechamento: R$ 5,50\n"
        )

    def gerar(_conteudo, _prompt):
        return "## Enquete do Dia\n\nO mercado exagerou?\n\n- Sim\n- Não\n- Talvez\n- Depende"

    _, rel = aplicar_dupla_verificacao(
        md,
        entrada="Data do e-mail: 13 de novembro de 2026\n\n" + entrada,
        idioma="pt",
        gerar=gerar,
        extrair_data=extrair_data_de_texto,
        reparar=reparar,
        notificar=notificar,
    )

    c_cab = _checagem(rel, "Cabeçalho")
    assert c_cab.status == CORRIGIDO
    assert "linha 1 sem '# Data'" in c_cab.encontrado and "título(s) '#' extra(s)" in c_cab.encontrado
    assert "inserido '# 13 de novembro de 2026'" in c_cab.corrigido and "rebaixado(s) para '##'" in c_cab.corrigido

    c_links = _checagem(rel, "Links e imagens")
    assert "link(s) Markdown" in c_links.encontrado and "imagem(ns) Markdown" in c_links.encontrado
    assert "links convertidos em texto puro" in c_links.corrigido

    c_tab = _checagem(rel, "Tabelas")
    assert "Ibovespa, Dólar" in c_tab.encontrado and "2 tópico(s) narrativo(s)" in c_tab.corrigido

    c_ruido = _checagem(rel, "Ruído")
    assert "Oferecimento: Patrocinador X" in c_ruido.encontrado and "removido(s)" in c_ruido.corrigido

    c_id = _checagem(rel, "Idioma")
    assert "'é directed'" in c_id.encontrado and "'directed' → 'dirigido'" in c_id.corrigido

    c_int = _checagem(rel, "Integridade")
    assert c_int.status == CORRIGIDO
    assert "140" in c_int.encontrado and "380" in c_int.encontrado
    assert "100%" in c_int.corrigido and "+5 número(s) restaurado(s)" in c_int.corrigido
    assert "completada(s): 'Resultados financeiros'" in c_int.corrigido

    c_voz = _checagem(rel, "Voz dos Ouvintes")
    assert c_voz.status == CORRIGIDO
    assert "2 bloco(s)" in c_voz.encontrado and "'leitores' → 'ouvintes'" in c_voz.encontrado
    assert "unificado(s) em '## Voz dos Ouvintes'" in c_voz.corrigido

    c_enq = _checagem(rel, "Enquete do Dia")
    assert c_enq.status == CORRIGIDO
    assert "1 opção" in c_enq.encontrado and "O mercado exagerou?" in c_enq.corrigido

    c_ord = _checagem(rel, "Ordem")
    assert c_ord.status == CORRIGIDO
    assert "fora de posição" in c_ord.encontrado and "reordenado" in c_ord.corrigido

    # Eventos em tempo real têm ia_inicio + corrigido para reparos com IA e determinísticos
    ia_inicios = [(r, d) for ev, r, d in eventos if ev == "ia_inicio"]
    assert any(r == "Idioma (sem inglês misturado)" and "é directed" in d["encontrado"] for r, d in ia_inicios)
    assert any(r == "Integridade (números)" and "140" in d["encontrado"] for r, d in ia_inicios)
    assert any(r == "Enquete do Dia" and "1 opção" in d["encontrado"] for r, d in ia_inicios)

    corrigidos = [(r, d) for ev, r, d in eventos if ev == "corrigido"]
    assert any(r == "Integridade (números)" and d["usou_ia"] and "100%" in d["corrigido"] for r, d in corrigidos)
    assert any(r == "Tabelas → tópicos" and not d["usou_ia"] and "Ibovespa" in d["encontrado"] for r, d in corrigidos)

    # Renderização na tabela Rich e em texto puro não quebra com colchetes do Markdown
    buf = io.StringIO()
    Console(file=buf, width=120, no_color=True).print(renderizar_relatorio(rel, "Programa [Teste]"))
    tabela_txt = buf.getvalue()
    assert "Encontrado:" in tabela_txt and "Corrigido:" in tabela_txt
    assert "Encontrado:" in rel.como_texto() and "→ Corrigido:" in rel.como_texto()


def test_promover_h3_detalha_encontrado_e_corrigido():
    md = (
        "# 13 de novembro de 2026\n\n"
        "### Primeira notícia\n\nTexto 1.\n\n"
        "### Segunda notícia\n\nTexto 2.\n\n"
        "## Enquete do Dia\n\nQual?\n\n- A\n- B\n"
    )
    _, rel = aplicar_dupla_verificacao(md, idioma="pt", extrair_data=extrair_data_de_texto)
    c_not = _checagem(rel, "Notícias")
    assert c_not.status == CORRIGIDO
    assert "Primeira notícia" in c_not.encontrado and "Segunda notícia" in c_not.encontrado
    assert "2 subtítulo(s) '###' promovido(s)" in c_not.corrigido


def test_pipeline_loga_detalhes_dupla_verificacao(tmp_path, caplog):
    texto_filler = "Detalhamento editorial completo da matéria para garantir o corpo da edição. " * 4

    class ExtratorFalso:
        def __init__(self):
            self.chamadas = 0

        def extrair(self, _conteudo, _prompt):
            self.chamadas += 1
            if self.chamadas == 1:
                return (
                    "# 15 de novembro de 2026\n\n"
                    f"## Economia\n\nA receita subiu 25%. {texto_filler}\n\n"
                    "| Ativo | Valor |\n| --- | --- |\n| Ibovespa | 172.000 |\n"
                )
            if self.chamadas == 2:
                return (
                    "# 15 de novembro de 2026\n\n"
                    f"## Economia\n\nA receita subiu 25% para 140 milhões com 380 mil clientes, "
                    f"margem de 18%, alta de 42% e meta de 95 milhões. {texto_filler}\n\n"
                    "- **Ibovespa:** Valor: 172.000"
                )
            return "## Enquete do Dia\n\nO resultado convenceu?\n\n- Sim\n- Não\n- Em parte\n- Nunca"

    pipe = Pipeline(Opcoes(dry_run=True, max_workers=1), extrator=ExtratorFalso(), drive_service=None, gmail_service=None)
    pipe.pasta_saida = tmp_path
    cfg = ConfigGrupo(
        chave="the news",
        emails=[{
            "id": None,
            "assunto": "the news: 15/11/2026",
            "remetente": "the news",
            "data": "Sun, 15 Nov 2026 09:00:00 +0000",
            "conteudo_html": (
                "<p>A receita subiu 25% para 140 milhões com 380 mil clientes, "
                "margem de 18%, alta de 42% e meta de 95 milhões. Ibovespa 172.000.</p>"
            ),
            "eh_html": True,
        }],
        titulo_base="Boletim Fora de Ordem",
        nome_final="Boletim Fora de Ordem #320.md",
    )
    with caplog.at_level("INFO", logger="espelho_magico"):
        resultados = pipe.executar([cfg])

    assert resultados[0].status == "dry"
    mensagens = "\n".join(Text.from_markup(r.message).plain for r in caplog.records)
    assert "[Tabelas → tópicos]" in mensagens and "Ibovespa" in mensagens
    assert "[Integridade (números)]" in mensagens and "140" in mensagens and "100%" in mensagens
    assert "[Enquete do Dia]" in mensagens and "O resultado convenceu?" in mensagens


def test_enquete_bilingue_com_pergunta_em_bullet_nao_duplica_e_deduplica_extra():
    md_unico = (
        "# November 14, 2026\n\n"
        "## Travel Agents\n\nTravellers are using travel agents again.\n\n"
        "## Daily Poll / Enquete do Dia\n\n"
        "- How do you prefer to book your vacations?\n"
        "  - Through a professional human travel agent\n"
        "  - Using AI tools and online platforms\n"
        "  - Doing all the planning and booking manually yourself\n"
    )
    chamadas_ia = []
    final, rel = aplicar_dupla_verificacao(
        md_unico,
        idioma="en",
        extrair_data=extrair_data_de_texto,
        gerar=lambda c, p: chamadas_ia.append(p) or "## Daily Poll\n\nDuplicada?\n\n- A\n- B\n- C\n- D",
    )
    assert not chamadas_ia, "não deveria recriar/duplicar enquete reconhecendo Daily Poll / Enquete do Dia"
    assert final.count("## Daily Poll") == 1
    assert "Daily Poll / Enquete do Dia" not in final
    assert "\nHow do you prefer to book your vacations?\n\n- Through a professional human travel agent" in final
    c_enq = _checagem(rel, "Daily Poll")
    assert c_enq.status == CORRIGIDO
    assert "3 opções" in c_enq.corrigido
    assert not rel.tem_aviso

    md_duplicado = (
        md_unico
        + "\n## Daily Poll\n\n"
        + "Should a high school diploma actually be required to lead a country?\n\n"
        + "- Yes, basic civics feels fair.\n"
        + "- No, grads still wreck everything.\n"
        + "- Only if verified against MS Paint.\n"
        + "- Make candidates survive a common sense quiz.\n"
    )
    final_dup, rel_dup = aplicar_dupla_verificacao(
        md_duplicado, idioma="en", extrair_data=extrair_data_de_texto
    )
    assert final_dup.count("## Daily Poll") == 1
    assert "How do you prefer" not in final_dup
    assert "Should a high school diploma actually be required to lead a country?" in final_dup
    assert _checagem(rel_dup, "Daily Poll").status == CORRIGIDO


def test_remove_autopromocao_sorteio_merchan_sem_falso_positivo_integridade():
    entrada = (
        "# 15 de novembro de 2026\n\n"
        "Hoje também é o último dia para concorrer a um **iPhone 18 Pro** indicando o *the news* e registrando seu Streak.\n\n"
        "## Estudo FGV EAESP (ARQ)\n\n"
        "Um estudo da FGV EAESP testou 80 janelas de dez anos desde 2010 e mostrou vantagem de 9,2%.\n"
        "- **Taxas e ativos no ARQ:** conversão de 0,5% (R$ 600 em dez anos) e corretagem de 0,2% em 10 mil ativos.\n"
        "- **Investback de lançamento:** apenas nas próximas 48 horas, o ARQ devolve US$ 10 dos primeiros US$ 100.\n\n"
        "## Apple anuncia novidades\n\n"
        "A Apple anunciou o iPhone 18 Pro por US$ 1.199 com chip A20 de 2 nanômetros.\n"
        "- Destaque editorial: **As 25 melhores cidades do mundo em 2026**\n\n"
        "Next week, a meditation on whether Canada has its own Strait of Hormuz.\n\n"
        "## Peak Premium & Advertising\n\n"
        "- **Try Peak Premium:** Unlimited access for $1.75 per week for 60 days.\n"
        "- **Advertise with The Peak:** Reach over 200,000 professionals.\n"
        "- **Head Office:** 68 Claremont Street, Toronto, Ontario M6J 2M5, Canada.\n\n"
        "## Última chamada: iPhone 18 Pro\n\n"
        "Último dia da campanha para concorrer a um iPhone 18 Pro indicando amigos para receber o the news.\n\n"
        "## Peak Picks\n\n"
        "- Mexico just made $5.4 million selling Ryan Wedding's motorcycle collection.\n\n"
        "## Voz dos Ouvintes\n\n"
        "- (Imparcial) Sou assinante desde o início do programa.\n\n"
        "## Enquete do Dia\n\nQual notícia marcou?\n\n- Estudo FGV\n- Apple\n- Mercado\n- Nenhuma\n"
    )
    final, rel = aplicar_dupla_verificacao(
        entrada, entrada=entrada, idioma="pt", extrair_data=extrair_data_de_texto
    )
    assert "## Estudo FGV EAESP\n" in final
    assert "(ARQ)" not in final
    assert "iPhone 18 Pro indicando" not in final
    assert "Streak" not in final
    assert "Investback" not in final
    assert "Taxas e ativos no ARQ" not in final
    assert "Destaque editorial" not in final
    assert "Next week" not in final
    assert "Peak Premium" not in final
    assert "Claremont Street" not in final
    assert "Última chamada" not in final
    assert "## Peak Picks" not in final
    assert "## Quick Picks\n" in final
    assert "Sou ouvinte desde o início do programa." in final
    # Notícia legítima com número da Apple permanece intacta
    assert "A Apple anunciou o iPhone 18 Pro por US$ 1.199" in final
    assert _status(rel, "Ruído") == CORRIGIDO
    assert _status(rel, "Integridade") == OK
    assert not rel.tem_aviso


def test_remove_denius_games_e_extra_clickbait_puro():
    md = (
        "# 17 de novembro de 2026\n\n"
        "## Economia em foco\n\n"
        "O dólar caiu para R$ 5,28 e a bolsa subiu.\n\n"
        "## Extra\n\n"
        "- **PARA VER:** Os carros mais bizarros.\n"
        "- **DESCUBRA:** Como era a casa.\n"
        "- **PARA CLICAR:** A reação curiosa.\n"
        "- **NESTE DIA:** A carta histórica.\n\n"
        "## Dênius Games – O jogo das corujas noturnas chegou\n\n"
        "## Enquete do Dia\n\nQual destaque?\n\n- Economia\n- Tecnologia\n- Saúde\n- Nenhum\n"
    )
    final, rel = aplicar_dupla_verificacao(md, idioma="pt", extrair_data=extrair_data_de_texto)
    assert "## Extra" not in final
    assert "PARA VER:" not in final
    assert "DESCUBRA:" not in final
    assert "Dênius Games" not in final
    assert "## Economia em foco" in final
    assert _status(rel, "Ruído") == CORRIGIDO
    assert not rel.tem_aviso


def test_enquete_contexto_abaixo_limite_divisao_e_bloco_nao_mistura_perguntas():
    secoes = "\n\n".join(
        f"## Notícia {i}\n\n" + ("Texto detalhado da pauta com acontecimentos relevantes. " * 25)
        for i in range(1, 20)
    )
    md_grande = f"# 17 de novembro de 2026\n\n{secoes}\n"
    ctx = _contexto_para_enquete(md_grande)
    assert len(ctx) < MIN_CHARS_PARA_DIVIDIR

    resposta_mista = (
        "## Enquete do Dia\n\n"
        "Você andaria no carro autônomo?\n\n"
        "- Só se tiver volante de reserva\n"
        "- Vou no banco de trás\n"
        "- Prefiro dirigir\n"
        "- Só depois que outros testarem\n\n"
        "## Enquete do Dia\n\n"
        "Você usaria pente elétrico contra calvície?\n\n"
        "- Qualquer coisa é melhor do que aceitar a calvície\n"
        "- Prefiro boné\n"
    )
    bloco = _montar_bloco_enquete(resposta_mista, "pt")
    assert bloco is not None
    assert "Você andaria no carro autônomo?" in bloco
    assert "Você usaria pente elétrico" not in bloco
    assert "calvície" not in bloco
    assert bloco.count("\n- ") == 4


def test_remove_minerva_cta_e_caixa_avaliacao_edicao():
    md = (
        "# 17 de novembro de 2026\n\n"
        "## Nutrição na corrida\n\n"
        "> 🏃‍➡️ **Na prática:** o que fica no prato importa mais. Nesta matéria, a **My Minerva Foods** explicou que são as **proteínas** que reparam o músculo.\n\n"
        "- Quer montar um prato? A **My Minerva Foods**, hub gratuito de ciência e nutrição, te mostra como. 👉 Veja aqui o conteúdo completo.\n\n"
        "## Voz dos Ouvintes\n\n"
        "- (32%): Excelente resumo!\n\n"
        "O que você achou da edição de hoje?\n\n"
        "- 🥇 Gostei  \n"
        "- 🥈 Bem mais ou menos  \n"
        "- 🥉 Não gostei\n\n"
        "## Enquete do Dia\n\nQual?\n\n- A\n- B\n- C\n- D\n"
    )
    final, rel = aplicar_dupla_verificacao(md, idioma="pt", extrair_data=extrair_data_de_texto)
    assert "My Minerva Foods" not in final
    assert "Veja aqui" not in final
    assert "O que você achou da edição de hoje" not in final
    assert "Gostei" not in final
    assert "Proteínas" in final or "proteínas" in final
    assert "Excelente resumo!" in final
    assert _status(rel, "Ruído") == CORRIGIDO
    assert not rel.tem_aviso


def test_integridade_ignora_espaco_inseparavel_endereco_e_dropped_by():
    entrada = (
        "# 17 de novembro de 2026\n\n"
        "EDIÇÃO #259 • 17/11/2026 • THEDROPS.COM.BR\n\n"
        "## IA na pesquisa\n\n"
        "OpenAI ofereceu acesso para até 200.000 pesquisadores, cortou o tempo de 475 para 24 minutos e marcou 1.686 pontos.\n\n"
        "##### CAMPANHA FOMO ZERO\n\n"
        "# A IA trabalha 24/7!\n\n"
        "Gaste US$ 3 trilhões e entre no Kit Anti FOMO Club.\n\n"
        "##### DROPPED BY GEOAMBIENTE\n\n"
        "# Sua IA conversa\n\n"
        "Dia 19/11, às 10h. Garanta sua vaga.\n\n"
        "*Quer fazer sua marca conversar com +300 mil líderes? Anuncie.*\n"
        "Rod. Francisco Magno Vieira, 4397 - Sl 202 - Campeche\n"
        "Florianópolis, SC 88065-000, Brazil\n"
    )
    md = (
        "# 17 de novembro de 2026\n\n"
        "## IA na pesquisa\n\n"
        "OpenAI ofereceu acesso para até 200\u202f000 pesquisadores, cortou o tempo de 475 para 24 minutos e marcou 1.686 pontos.\n\n"
        "## Campanha FOMO Zero\n\n"
        "Gaste US$ 3 trilhões e entre no Kit Anti FOMO Club.\n\n"
        "## Enquete do Dia\n\nQual impacto?\n\n- Alto\n- Médio\n- Baixo\n- Nenhum\n"
    )
    final, rel = aplicar_dupla_verificacao(md, entrada=entrada, idioma="pt", extrair_data=extrair_data_de_texto)
    assert "Campanha FOMO Zero" not in final
    assert "200\u202f000" in final
    assert _status(rel, "Ruído") == CORRIGIDO
    assert _status(rel, "Integridade") == OK
    assert not rel.tem_aviso


def test_completa_imparcial_voz_remove_ovicio_form_e_higieniza_risos_moeda():
    bruto = (
        "# 18 de novembro de 2026\n\n"
        "## Curiosidade\n\n"
        "Qual ator ganha menos?\n\n"
        "**Resposta:** A resposta oficial não consta nesta parte da edição.\n\n"
        "## Motores V8\n\n"
        "### Estagi, você tá falando grego\n\n"
        "Ronco agressivo (e agradável aos nossos ouvidos. risos.). Veja a carta na íntegra aqui.\n\n"
        "## Frango e Meta\n\n"
        "*Não é pegadinha, risos.* A Upside lançou hoje e a Meta pagou US$ US$ 25 bilhões. (***Aprofunde***)\n\n"
        "O texto mostra como o modelo funciona.\n\n"
        "❝\n\n"
        "> — (@)\n\n"
        "🔋 Veja mais nesta entrevista do the news.\n\n"
        "## Gatos no Brasil\n\n"
        "Pelo contrário, segundo o IBGE, 19,3% dos lares têm gatos, risos. E seguem felizes.\n\n"
        "## The Game Awards\n\n"
        "Os vencedores serão anunciados **no dia 10 de dezembro**, e pela primeira vez a equipe do **O Vício** faz parte do júri.\n\n"
        "Chegou o dia de conhecer os indicados do **TGA 2026**. Desta vez, o portal O Vício fez parte da escolha dos indicados. Confira a lista revelada em live:\n\n"
        "Premia um profissional por atuação de voz. Por favor, liste o nome específico do ator ou personagem, e o jogo.\n\n"
        "## Voz dos Ouvintes\n\n"
        "Resultado da enquete da edição de 17/11/26 (votada por 2.128 ouvintes): **Esquerda:** 5% | **Direita:** 5% | **Imparcial:** (não informado)\n\n"
        "## Enquete do Dia\n\nQual tema venceu?\n\n- Motores\n- Economia\n- IA\n- Games\n"
    )
    sanitizado = sanitizar_markdown_final(bruto)
    assert "US$ US$" not in sanitizado and "US$ 25 bilhões" in sanitizado
    assert "Aprofunde" not in sanitizado
    assert "❝" not in sanitizado and "> — (@)" not in sanitizado
    assert "risos" not in sanitizado.lower()
    assert "### Entenda o contexto" in sanitizado
    assert "Estagi" not in sanitizado
    assert "Pelo contrário" not in sanitizado and "Segundo o IBGE, 19,3%" in sanitizado
    assert "O texto mostra como" not in sanitizado and "A análise mostra como" in sanitizado

    final, rel = aplicar_dupla_verificacao(sanitizado, idioma="pt", extrair_data=extrair_data_de_texto)
    assert "Imparcial: 90% | **Esquerda:** 5% | **Direita:** 5%" in final
    assert "(não informado)" not in final
    assert "Veja a carta na íntegra aqui" not in final
    assert "Veja mais nesta entrevista do the news" not in final
    assert "O Vício" not in final
    assert "Confira a lista revelada em live" not in final
    assert "Por favor, liste o nome específico" not in final
    assert "não consta nesta parte da edição" not in final
    assert "Premia um profissional por atuação de voz." in final
    assert _status(rel, "Ruído") == CORRIGIDO
    assert _status(rel, "Voz dos Ouvintes") == CORRIGIDO
    assert not rel.tem_aviso

    raiz = Path(__file__).resolve().parent.parent / "prompts_notebooklm"
    for nome in ("podcast_pt_br_universal.md", "podcast_en_the_out_of_order_dispatch.md"):
        conteudo = (raiz / nome).read_text(encoding="utf-8")
        bloco = re.search(r"```text\s*(.*?)\s*```", conteudo, re.S)
        assert bloco is not None
        assert len(bloco.group(1)) < 5000


def test_cobertura_pautas_detecta_e_recupera_noticia_sem_numeros_omitida():
    from espelho_magico.validacao import AVISO, checar_cobertura_pautas, checar_integridade

    entrada = (
        "Os estivadores e operadores portuários de Roterdã entraram em greve por tempo indeterminado após o "
        "impasse nas negociações salariais com as grandes armadoras europeias e suspenderam os embarques marítimos.\n\n"
        "A agência espacial europeia divulgou novas imagens detalhadas das tempestades magnéticas no hemisfério "
        "norte obtidas por satélites solares em órbita polar durante a madrugada.\n\n"
        "Os bancos regionais ampliaram as linhas de crédito emergencial voltadas ao setor agrícola depois da "
        "prolongada estiagem nas lavouras de café do sudeste mineiro.\n\n"
        "O ministério da saúde anunciou a expansão nacional dos atendimentos remotos de telessaúde nas "
        "unidades básicas para reduzir filas de especialidades médicas em municípios isolados."
    )
    md_incompleto = (
        "# 18 de novembro de 2026\n\n"
        "## Greve no porto de Roterdã\n\n"
        "Os operadores portuários de Roterdã suspenderam embarques marítimos após impasse salarial.\n\n"
        "## Enquete do Dia\n\nQual assunto mais chamou atenção?\n\n- Porto\n- Espaço\n- Agro\n- Saúde\n"
    )

    pct_p, verif, omitidas = checar_cobertura_pautas(entrada, md_incompleto)
    assert len(verif) == 4 and len(omitidas) == 3 and pct_p == 25

    st, det = checar_integridade(entrada, md_incompleto)
    assert st == AVISO and "pautas cobertas" in det

    def fake_reparar(conteudo, instrucao):
        assert "<trechos_fonte>" in conteudo
        assert "satélites solares" in conteudo
        return (
            "# 18 de novembro de 2026\n\n"
            "## Greve no porto de Roterdã\n\n"
            "Os operadores portuários de Roterdã suspenderam embarques marítimos após impasse salarial.\n\n"
            "## Tempestades solares e agro\n\n"
            "A agência espacial europeia captou imagens de tempestades magnéticas com satélites solares em órbita polar.\n\n"
            "### Crédito agrícola para o café\n\n"
            "Bancos regionais ampliaram crédito ao setor agrícola após estiagem nas lavouras de café mineiro.\n\n"
            "## Telessaúde nos municípios\n\n"
            "O ministério da saúde anunciou expansão nacional de telessaúde nas unidades básicas.\n\n"
            "## Enquete do Dia\n\nQual assunto mais chamou atenção?\n\n- Porto\n- Espaço\n- Agro\n- Saúde\n"
        )

    final, rel = aplicar_dupla_verificacao(md_incompleto, entrada=entrada, idioma="pt", reparar=fake_reparar)
    assert _status(rel, "Integridade (números)") == CORRIGIDO
    assert "telessaúde" in final.lower()
    # Seções concatenadas (tempestades + café sob o mesmo ##) continuam 100% cobertas sem falso positivo
    pct_final, _, omitidas_final = checar_cobertura_pautas(entrada, final)
    assert pct_final == 100 and not omitidas_final



