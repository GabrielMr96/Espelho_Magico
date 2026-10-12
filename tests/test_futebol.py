import datetime

from espelho_magico import futebol


def _evento(slug, casa, fora, placar=("1", "0")):
    return {
        "season": {"slug": slug},
        "competitions": [{
            "competitors": [
                {"homeAway": "home", "score": placar[0], "team": {"shortDisplayName": casa, "name": casa}},
                {"homeAway": "away", "score": placar[1], "team": {"shortDisplayName": fora, "name": fora}},
            ]
        }],
    }


DADOS = {"events": [
    _evento("2026-brasileiro-serie-a", "Bahia", "Vitória", ("2", "1")),
    _evento("2026-27-english-premier-league", "Arsenal", "Chelsea"),
    _evento("2026-argentine-liga-profesional", "Arsenal Sarandí", "Tigre"),
    _evento("2026-colombian-primera-a", "Millonarios", "Nacional"),
    _evento("2026-27-english-championship", "Leeds", "Burnley"),
    _evento("2026-saudi-pro-league", "Al Nassr", "Al Ahli", ("3", "0")),
]}


def test_formatar_resultados_filtra_ligas_e_clubes():
    md = futebol.formatar_resultados(DADOS, datetime.date(2026, 10, 11))
    assert md.startswith("## RESULTADOS RECENTES (11/10/2026)")
    assert "- Bahia 2 x 1 Vitória" in md
    assert "### English Premier League" in md
    assert "- Al Nassr 3 x 0 Al Ahli" in md  # liga secundária, mas clube destaque
    for fora in ("Sarandí", "Millonarios", "Leeds"):
        assert fora not in md


def test_sem_eventos():
    assert "Nenhum jogo" in futebol.formatar_resultados({"events": []}, datetime.date(2026, 1, 1))


def test_datas_pendentes():
    hoje = datetime.date(2026, 10, 12)
    assert futebol.datas_pendentes(None, hoje) == [datetime.date(2026, 10, 11)]
    assert futebol.datas_pendentes(datetime.date(2026, 10, 9), hoje) == [
        datetime.date(2026, 10, 10), datetime.date(2026, 10, 11)
    ]
    assert futebol.datas_pendentes(datetime.date(2026, 10, 11), hoje) == []


def test_coletar_resultados_para_no_primeiro_erro(tmp_path):
    controle = tmp_path / "controle.json"
    futebol.salvar_ultima_data(datetime.date(2026, 10, 8), controle)

    def buscar(data):
        if data == datetime.date(2026, 10, 10):
            raise futebol.ErroESPN("fora do ar")
        return f"## RESULTADOS RECENTES ({data:%d/%m/%Y})"

    texto, ultima_ok = futebol.coletar_resultados(datetime.date(2026, 10, 12), buscar, controle)
    assert ultima_ok == datetime.date(2026, 10, 9)
    assert "09/10/2026" in texto
    assert "Erro" not in texto and "fora do ar" not in texto


def test_coletar_resultados_falha_total_nao_avanca(tmp_path):
    controle = tmp_path / "controle.json"

    def buscar(data):
        raise futebol.ErroESPN("x")

    texto, ultima_ok = futebol.coletar_resultados(datetime.date(2026, 10, 12), buscar, controle)
    assert texto == "" and ultima_ok is None
