import base64
import json

from espelho_magico.deduplicacao import (
    carregar_historico_noticias,
    deve_deduplicar_noticias,
    extrair_noticias_email,
    filtrar_email_noticias_repetidas,
    normalizar_chave_noticia,
    registrar_noticias_extraidas,
)
from espelho_magico.pipeline import ConfigGrupo, Opcoes, Pipeline


def _html_feed(titulo_feed, cards):
    blocos = []
    for titulo, dt_hora, corpo_extra in cards:
        blocos.append(
            '<div style="font-family: Arial, sans-serif; border-bottom: 1px solid #ccc;">'
            f'<h2><a href="https://exemplo.com/{len(blocos)}">{titulo}</a></h2>'
            f"<p>{dt_hora}</p>"
            f'<div style="font-size: 14px;">{corpo_extra}</div>'
            "</div>"
        )
    return (
        '<html><body><div style="width:100%;max-width:600px;">'
        f"<h1>{titulo_feed}</h1><p>Gerado em 20/11/2026 11:02</p><hr/>"
        + "\n".join(blocos)
        + "</div></body></html>"
    )


def test_deve_deduplicar_noticias_apenas_nos_tres_programas():
    assert deve_deduplicar_noticias("notícias de segurança") is True
    assert deve_deduplicar_noticias("Notícias de Entretenimento") is True
    assert deve_deduplicar_noticias("notícias de games") is True
    assert deve_deduplicar_noticias("the news") is False
    assert deve_deduplicar_noticias("tns sports") is False
    assert deve_deduplicar_noticias("peak") is False


def test_filtra_apenas_titulo_e_data_hora_identicos_preservando_parecidas():
    html_anterior = _html_feed(
        "Notícias de Games",
        [
            ("GTA VI bate recorde de vendas", "19/11/2026 às 18:00", "<p>Texto anterior sobre GTA VI.</p>"),
            ("GTA VI ganha modo foto", "19/11/2026 às 14:00", "<p>Texto modo foto.</p>"),
        ],
    )
    vistos = {item["chave"] for item in extrair_noticias_email({"conteudo_html": html_anterior, "eh_html": True})}

    # Mesma notícia (título + data/hora iguais) deve sair;
    # Mesma pauta ("GTA VI") com outro título, ou mesmo título com outra data/hora, DEVEM ficar!
    html_atual = _html_feed(
        "Notícias de Games",
        [
            ("GTA VI bate recorde de vendas", "19/11/2026 às 18:00", "<p>Texto repetido.</p>"),
            ("GTA VI bate recorde de vendas", "20/11/2026 às 09:00", "<p>Atualização com outro horário.</p>"),
            ("GTA VI chega ao topo do Metacritic", "19/11/2026 às 18:00", "<p>Mesmo horário, outro título.</p>"),
            ("GTA VI ganha trailer inédito", "20/11/2026 às 10:15", "<p>Notícia parecida mas diferente.</p>"),
        ],
    )
    filtrado, removidas, mantidas, total = filtrar_email_noticias_repetidas(
        {"conteudo_html": html_atual, "eh_html": True}, vistos
    )
    assert total == 4
    assert len(removidas) == 1
    assert removidas[0]["titulo"] == "GTA VI bate recorde de vendas"
    assert len(mantidas) == 3
    assert "Texto repetido." not in filtrado["conteudo_html"]
    assert "Atualização com outro horário." in filtrado["conteudo_html"]
    assert "Mesmo horário, outro título." in filtrado["conteudo_html"]
    assert "Notícia parecida mas diferente." in filtrado["conteudo_html"]


def test_filtra_html_com_subtitulos_h2_internos_sem_falso_positivo():
    html_ant = _html_feed(
        "Notícias de Segurança (Tecnoblog)",
        [
            (
                "Como ver suas senhas salvas",
                "19/11/2026 às 16:28",
                "<h2>Índice</h2><ul><li>Item</li></ul><h2>Passo 1</h2><p>Abra o Chrome.</p>",
            )
        ],
    )
    vistos = {item["chave"] for item in extrair_noticias_email({"conteudo_html": html_ant, "eh_html": True})}

    html_novo = _html_feed(
        "Notícias de Segurança (Tecnoblog)",
        [
            (
                "Grupo assume vazamento de dados",
                "20/11/2026 às 04:08",
                "<h2>Incidentes</h2><p>Fornecedores afetados.</p>",
            ),
            (
                "Como ver suas senhas salvas",
                "19/11/2026 às 16:28",
                "<h2>Índice</h2><ul><li>Item</li></ul><h2>Passo 1</h2><p>Abra o Chrome.</p>",
            ),
        ],
    )
    filtrado, removidas, mantidas, total = filtrar_email_noticias_repetidas(
        {"conteudo_html": html_novo, "eh_html": True}, vistos
    )
    assert total == 2
    assert len(removidas) == 1
    assert len(mantidas) == 1
    assert "Grupo assume vazamento de dados" in filtrado["conteudo_html"]
    assert "Fornecedores afetados." in filtrado["conteudo_html"]
    assert "Como ver suas senhas salvas" not in filtrado["conteudo_html"]
    assert "Passo 1" not in filtrado["conteudo_html"]


def test_filtra_fallback_markdown_quando_sem_html():
    md = (
        "# Notícias de Segurança\n\n"
        "## Notícia Velha\n\n19/11/2026 às 15:10\n\n## Subtítulo Interno\n\nCorpo velho.\n\n"
        "## Notícia Nova\n\n20/11/2026 às 08:00\n\nCorpo novo.\n"
    )
    vistos = {normalizar_chave_noticia("Notícia Velha", "19/11/2026 15:10")}
    filtrado, removidas, mantidas, total = filtrar_email_noticias_repetidas(
        {"conteudo_html": md, "eh_html": False}, vistos
    )
    assert total == 2
    assert len(removidas) == 1
    assert len(mantidas) == 1
    assert "Corpo velho." not in filtrado["conteudo_html"]
    assert "Subtítulo Interno" not in filtrado["conteudo_html"]
    assert "Corpo novo." in filtrado["conteudo_html"]


def test_bootstrap_gmail_respeita_ultima_data_e_ids_excluidos(tmp_path):
    (tmp_path / "Hackeando o Problema #212.md").write_text(
        "# Hackeando o Problema #212 — 19 de novembro de 2026\n\n## X\n\nCorpo.\n",
        encoding="utf-8",
    )

    def _msg(msg_id, subject, date_hdr, html):
        b64 = base64.urlsafe_b64encode(html.encode("utf-8")).decode()
        return {
            "id": msg_id,
            "payload": {
                "mimeType": "text/html",
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": "gabriel@gmail.com"},
                    {"name": "Date", "value": date_hdr},
                ],
                "body": {"data": b64},
            },
        }

    html_19 = _html_feed("Notícias de Segurança", [("Falha no Java", "19/11/2026 às 15:10", "<p>Java</p>")])
    html_20 = _html_feed("Notícias de Segurança", [("Vazamento Visa", "20/11/2026 às 04:08", "<p>Visa</p>")])
    msgs = {
        "m19": _msg("m19", "Notícias de Segurança (Tecnoblog) - 19/11", "Thu, 19 Nov 2026 16:50:00 -0300", html_19),
        "m20": _msg("m20", "Notícias de Segurança (Tecnoblog) - 20/11", "Fri, 20 Nov 2026 11:02:00 -0300", html_20),
    }

    class FakeExec:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class FakeMessages:
        def list(self, **_kw):
            return FakeExec({"messages": [{"id": "m20"}, {"id": "m19"}]})

        def get(self, id, **_kw):
            return FakeExec(msgs[id])

    class FakeGmail:
        def users(self):
            return self

        def messages(self):
            return FakeMessages()

    controle = tmp_path / "controle_noticias.json"
    vistos, boot, init_agora = carregar_historico_noticias(
        "notícias de segurança",
        "Hackeando o Problema",
        gmail_service=FakeGmail(),
        ids_excluir=[],
        caminho_controle=controle,
        pasta_resultados=tmp_path,
    )
    assert init_agora is True
    assert normalizar_chave_noticia("Falha no Java", "19/11/2026 às 15:10") in vistos
    # E-mail de 20/11 é mais recente que ultima_data (19/11), portanto NÃO deve ser indexado como já extraído
    assert normalizar_chave_noticia("Vazamento Visa", "20/11/2026 às 04:08") not in vistos
    assert len(boot) == 1


def test_pipeline_remove_repetidas_e_ignora_lote_100_porcento_repetido(tmp_path):
    controle = tmp_path / "controle_noticias.json"
    registrar_noticias_extraidas(
        "notícias de segurança",
        [
            {
                "titulo": "Notícia Antiga",
                "data_hora": "19/11/2026 às 12:00",
                "chave": normalizar_chave_noticia("Notícia Antiga", "19/11/2026 às 12:00"),
            }
        ],
        caminho_controle=controle,
    )

    class ExtratorFalso:
        def __init__(self):
            self.chamadas = []

        def extrair(self, conteudo, _prompt):
            self.chamadas.append(conteudo)
            if len(self.chamadas) == 1:
                return (
                    "# 20 de novembro de 2026\n\n"
                    "## Notícia Inédita\n\n"
                    + ("Texto completo e detalhado sobre o incidente inédito publicado hoje. " * 6)
                )
            return "## Enquete do Dia\n\nQual sua opinião?\n\n- A\n- B\n- C\n- D"

    html_misto = _html_feed(
        "Notícias de Segurança (Tecnoblog)",
        [
            ("Notícia Inédita", "20/11/2026 às 08:00", "<p>" + ("Texto inédito detalhado. " * 8) + "</p>"),
            ("Notícia Antiga", "19/11/2026 às 12:00", "<p>Texto antigo que não deve ir para a IA.</p>"),
        ],
    )

    # 1) Em dry-run, remove a repetida mas NÃO altera controle_noticias.json
    ext_dry = ExtratorFalso()
    pipe_dry = Pipeline(Opcoes(dry_run=True, max_workers=1), extrator=ext_dry, drive_service=None, gmail_service=None)
    pipe_dry.pasta_saida = tmp_path / "_dry"
    pipe_dry.pasta_historico = tmp_path
    pipe_dry.caminho_controle_noticias = controle
    cfg1 = ConfigGrupo(
        chave="notícias de segurança",
        emails=[{
            "id": "e1",
            "assunto": "Notícias de Segurança - 20/11",
            "remetente": "x@x.com",
            "data": "Fri, 20 Nov 2026 11:00:00 -0300",
            "conteudo_html": html_misto,
            "eh_html": True,
        }],
        titulo_base="Hackeando o Problema",
        nome_final="Hackeando o Problema #213.md",
    )
    res_dry = pipe_dry.executar([cfg1])
    assert res_dry[0].status == "dry"
    assert "Notícia Inédita" in ext_dry.chamadas[0]
    assert "Notícia Antiga" not in ext_dry.chamadas[0]
    dados_dry = json.loads(controle.read_text(encoding="utf-8"))
    assert len(dados_dry["notícias de segurança"]["itens"]) == 1  # dry-run não gravou a nova

    # 2) Em execução normal, grava a nova notícia e depois ignora um segundo e-mail só com repetidas sem chamar IA
    ext_real = ExtratorFalso()
    marcados = []

    class FakeGmailModify:
        def users(self):
            return self

        def messages(self):
            return self

        def modify(self, userId, id, body):
            marcados.append((id, body))

            class _Exec:
                def execute(self_inner):
                    return {}

            return _Exec()

    pipe_real = Pipeline(Opcoes(dry_run=False, max_workers=1), extrator=ext_real, drive_service=None, gmail_service=FakeGmailModify())
    pipe_real.pasta_saida = tmp_path
    pipe_real.pasta_historico = tmp_path
    pipe_real.caminho_controle_noticias = controle
    res_real = pipe_real.executar([cfg1])
    assert res_real[0].status == "ok"
    assert marcados == [("e1", {"removeLabelIds": ["UNREAD"]})]

    # Segundo lote com as mesmas 2 notícias -> 100% repetidas -> IA não é chamada e e-mail é marcado como lido
    ext_vazio = ExtratorFalso()
    pipe_dup = Pipeline(Opcoes(dry_run=False, max_workers=1), extrator=ext_vazio, drive_service=None, gmail_service=FakeGmailModify())
    pipe_dup.pasta_saida = tmp_path
    pipe_dup.pasta_historico = tmp_path
    pipe_dup.caminho_controle_noticias = controle
    cfg2 = ConfigGrupo(
        chave="notícias de segurança",
        emails=[{
            "id": "e2",
            "assunto": "Notícias de Segurança - 21/11",
            "remetente": "x@x.com",
            "data": "Sat, 21 Nov 2026 11:00:00 -0300",
            "conteudo_html": html_misto,
            "eh_html": True,
        }],
        titulo_base="Hackeando o Problema",
        nome_final="Hackeando o Problema #214.md",
    )
    res_dup = pipe_dup.executar([cfg2])
    assert res_dup[0].status == "ignorado"
    assert ext_vazio.chamadas == []
    assert not (tmp_path / "Hackeando o Problema #214.md").exists()
    assert ("e2", {"removeLabelIds": ["UNREAD"]}) in marcados


def test_pipeline_deduplica_entre_emails_no_mesmo_lote_unificado(tmp_path):
    controle = tmp_path / "controle_noticias.json"
    registrar_noticias_extraidas("notícias de entretenimento", [], marcar_inicializado=True, caminho_controle=controle)

    html_manha = _html_feed(
        "Notícias de Entretenimento",
        [("Filme A ganha trailer", "20/11/2026 às 08:00", "<p>" + ("Corpo A inedito. " * 10) + "</p>")],
    )
    html_tarde = _html_feed(
        "Notícias de Entretenimento",
        [
            ("Filme A ganha trailer", "20/11/2026 às 08:00", "<p>Corpo A repetido da manhã.</p>"),
            ("Série B é renovada", "20/11/2026 às 14:00", "<p>" + ("Corpo B inedito. " * 10) + "</p>"),
        ],
    )

    class ExtratorFalso:
        def __init__(self):
            self.chamadas = []

        def extrair(self, conteudo, _prompt):
            self.chamadas.append(conteudo)
            if len(self.chamadas) == 1:
                return (
                    "# 20 de novembro de 2026\n\n"
                    "## Filme A ganha trailer\n\n" + ("Corpo A inedito. " * 10) + "\n\n"
                    "## Série B é renovada\n\n" + ("Corpo B inedito. " * 10)
                )
            return "## Enquete do Dia\n\nQual você prefere?\n\n- Filme A\n- Série B\n- Ambos\n- Nenhum"

    ext = ExtratorFalso()
    pipe = Pipeline(Opcoes(dry_run=False, max_workers=1), extrator=ext, drive_service=None, gmail_service=None)
    pipe.pasta_saida = tmp_path
    pipe.pasta_historico = tmp_path
    pipe.caminho_controle_noticias = controle
    cfg = ConfigGrupo(
        chave="notícias de entretenimento",
        emails=[
            {"id": "m1", "assunto": "Notícias de Entretenimento - 20/11", "remetente": "x@x.com", "data": "Fri, 20 Nov 2026 09:00:00 -0300", "conteudo_html": html_manha, "eh_html": True},
            {"id": "m2", "assunto": "Notícias de Entretenimento - 20/11", "remetente": "x@x.com", "data": "Fri, 20 Nov 2026 15:00:00 -0300", "conteudo_html": html_tarde, "eh_html": True},
        ],
        titulo_base="Pipoca Vencida",
        nome_final="Pipoca Vencida #284.md",
        unificar=True,
    )
    res = pipe.executar([cfg])
    assert res[0].status == "ok"
    assert "Corpo A repetido da manhã" not in ext.chamadas[0]
    assert "Corpo A inedito" in ext.chamadas[0]
    assert "Corpo B inedito" in ext.chamadas[0]
    dados = json.loads(controle.read_text(encoding="utf-8"))
    assert len(dados["notícias de entretenimento"]["itens"]) == 2
