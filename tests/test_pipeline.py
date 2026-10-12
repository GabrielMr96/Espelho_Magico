from espelho_magico.drive_client import montar_query
from espelho_magico.pipeline import agrupar_emails, deduplicar_secoes, maior_edicao_existente, nome_fallback

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
