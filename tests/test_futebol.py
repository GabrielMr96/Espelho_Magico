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
    segunda = datetime.date(2026, 10, 12)
    terca = datetime.date(2026, 10, 13)
    # Sem histórico: segunda traz sexta, sábado e domingo (3 dias); outros dias só ontem
    assert futebol.datas_pendentes(None, segunda) == [
        datetime.date(2026, 10, 9),
        datetime.date(2026, 10, 10),
        datetime.date(2026, 10, 11),
    ]
    assert futebol.datas_pendentes(None, terca) == [datetime.date(2026, 10, 12)]
    # Com garantir_segunda=True e última extração na sexta (09/10), mantém sexta-noite + sáb + dom
    assert futebol.datas_pendentes(datetime.date(2026, 10, 9), segunda) == [
        datetime.date(2026, 10, 9), datetime.date(2026, 10, 10), datetime.date(2026, 10, 11)
    ]
    assert futebol.datas_pendentes(datetime.date(2026, 10, 9), segunda, garantir_segunda=False) == [
        datetime.date(2026, 10, 10), datetime.date(2026, 10, 11)
    ]
    assert futebol.datas_pendentes(datetime.date(2026, 10, 10), segunda) == [datetime.date(2026, 10, 11)]
    assert futebol.datas_pendentes(datetime.date(2026, 10, 11), segunda) == []
    # Limita janela máxima quando há muitos dias sem rodar
    assert len(futebol.datas_pendentes(datetime.date(2026, 10, 1), segunda, max_dias=5)) == 5


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


def test_nome_liga_amigavel_e_formatar_resultados_psg():
    """Garante que Ligue 1 masculina (fra.1) passe e ligas femininas/base/pequenas sejam bloqueadas."""
    evento_psg = {
        "season": {"slug": "regular-season"},
        "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/401885655/league/fra.1"}],
        "competitions": [{
            "altGameNote": "Ligue 1",
            "competitors": [
                {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "PSG", "name": "Paris Saint-Germain"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Montpellier", "name": "Montpellier"}},
            ]
        }],
    }
    evento_fra_fem = {
        "season": {"slug": "regular-season"},
        "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/401885656/league/fra.w.1"}],
        "competitions": [{
            "altGameNote": "Première Ligue",
            "competitors": [
                {"homeAway": "home", "score": "3", "team": {"shortDisplayName": "Lyonnes", "name": "Lyon Women"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "PSG", "name": "Paris Saint-Germain"}},
            ]
        }],
    }
    evento_mls = {
        "season": {"slug": "regular-season"},
        "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/761891/league/usa.1"}],
        "competitions": [{
            "altGameNote": "MLS",
            "competitors": [
                {"homeAway": "home", "score": "2", "team": {"shortDisplayName": "Red Bull NY", "name": "Red Bulls"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Miami", "name": "Inter Miami CF"}},
            ]
        }],
    }
    evento_sula = {
        "season": {"slug": "semifinals"},
        "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/401913957/league/conmebol.sudamericana"}],
        "competitions": [{
            "altGameNote": "CONMEBOL Sudamericana, Semifinals",
            "competitors": [
                {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Vasco", "name": "Vasco da Gama"}},
                {"homeAway": "away", "score": "3", "team": {"shortDisplayName": "Boca", "name": "Boca Juniors"}},
            ]
        }],
    }
    evento_champions = {
        "season": {"slug": "league-phase"},
        "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/401915313/league/uefa.champions"}],
        "competitions": [{
            "altGameNote": "UEFA Champions League, League Phase",
            "competitors": [
                {"homeAway": "home", "score": "4", "team": {"shortDisplayName": "Real Madrid", "name": "Real Madrid"}},
                {"homeAway": "away", "score": "1", "team": {"shortDisplayName": "RB Leipzig", "name": "RB Leipzig"}},
            ]
        }],
    }

    assert futebol.nome_liga_amigavel(evento_psg) == "Ligue 1"
    assert futebol.nome_liga_amigavel(evento_mls) == "Major League Soccer"
    assert futebol.nome_liga_amigavel(evento_sula) == "Copa Sudamericana"
    assert futebol.nome_liga_amigavel(evento_champions) == "UEFA Champions League"

    dados = {"events": [evento_psg, evento_fra_fem, evento_mls, evento_sula, evento_champions]}
    md = futebol.formatar_resultados(dados, datetime.date(2026, 10, 23))

    assert "### Ligue 1\n- PSG 1 x 0 Montpellier" in md
    assert "Lyonnes" not in md
    assert "### Major League Soccer\n- Red Bull NY 2 x 0 Miami" in md
    assert "### Copa Sudamericana\n- Vasco 1 x 3 Boca" in md
    assert "### UEFA Champions League\n- Real Madrid 4 x 1 RB Leipzig" in md
    assert "Major League Soccer\n- PSG" not in md
    assert "### Regular Season" not in md
    assert "### Semifinals" not in md
    assert "### League Phase" not in md


def test_filtra_feminino_base_andamento_e_exige_populares_fora_serie_a(tmp_path):
    dados = {"events": [
        # Série A aceita qualquer confronto
        {
            "season": {"slug": "2026-brasileiro-serie-a"},
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/1/league/bra.1"}],
            "competitions": [{"status": {"type": {"completed": True}}, "competitors": [
                {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Mirassol", "name": "Mirassol"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Juventude", "name": "Juventude"}},
            ]}],
        },
        # Premier League sem time popular cai fora
        {
            "season": {"slug": "2026-27-english-premier-league"},
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/2/league/eng.1"}],
            "competitions": [{"status": {"type": {"completed": True}}, "competitors": [
                {"homeAway": "home", "score": "2", "team": {"shortDisplayName": "Everton", "name": "Everton"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Coventry", "name": "Coventry"}},
            ]}],
        },
        # Feminino (esp.w.1), Sub-21 (fifa.friendly_u21), Amistoso clubes (Arsenal Tula), Liga Uruguaia (Liverpool)
        {
            "season": {"slug": "2026-27-spanish-liga-f"},
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/3/league/esp.w.1"}],
            "competitions": [{"competitors": [
                {"homeAway": "home", "score": "3", "team": {"shortDisplayName": "Real Madrid", "name": "Real Madrid"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Madrid CFF", "name": "Madrid CFF"}},
            ]}],
        },
        {
            "season": {"slug": "2026-u21-international-friendly"},
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/4/league/fifa.friendly_u21"}],
            "competitions": [{"altGameNote": "Men's U-21 Friendly", "competitors": [
                {"homeAway": "home", "score": "2", "team": {"shortDisplayName": "France", "name": "France"}},
                {"homeAway": "away", "score": "1", "team": {"shortDisplayName": "England", "name": "England"}},
            ]}],
        },
        {
            "season": {"slug": "torneo-clausura"},
            "competitions": [{"altGameNote": "Liga AUF Uruguaya", "competitors": [
                {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Danubio", "name": "Danubio"}},
                {"homeAway": "away", "score": "2", "team": {"shortDisplayName": "Liverpool", "name": "Liverpool"}},
            ]}],
        },
        # Jogo ainda em andamento não entra
        {
            "season": {"slug": "2026-brasileiro-serie-a"},
            "competitions": [{"status": {"type": {"completed": False}}, "competitors": [
                {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Flamengo", "name": "Flamengo"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Palmeiras", "name": "Palmeiras"}},
            ]}],
        },
        # Nations League com seleções de elite passa; pequenas ficam fora
        {
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/7/league/uefa.nations"}],
            "competitions": [{"altGameNote": "UEFA Nations League, Group A2", "competitors": [
                {"homeAway": "home", "score": "5", "team": {"shortDisplayName": "Germany", "name": "Germany"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Netherlands", "name": "Netherlands"}},
            ]}],
        },
        {
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/8/league/uefa.nations"}],
            "competitions": [{"altGameNote": "UEFA Nations League, Group D1", "competitors": [
                {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Malta", "name": "Malta"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Gibraltar", "name": "Gibraltar"}},
            ]}],
        },
    ]}
    md = futebol.formatar_resultados(dados, datetime.date(2026, 11, 16))
    assert "- Mirassol 1 x 0 Juventude" in md
    assert "Germany 5 x 0 Netherlands" in md
    for fora in ("Everton", "Madrid CFF", "France", "Danubio", "Flamengo", "Malta"):
        assert fora not in md

    controle = tmp_path / "controle.json"
    futebol.salvar_ultima_data(datetime.date(2026, 11, 13), controle)
    texto, ultima_ok = futebol.coletar_resultados(
        datetime.date(2026, 11, 16),
        lambda d: "Nenhum jogo encontrado." if d == datetime.date(2026, 11, 14) else f"## RESULTADOS RECENTES ({d:%d/%m/%Y})",
        controle,
    )
    assert "Nenhum jogo" not in texto
    assert "15/11/2026" in texto
    assert ultima_ok == datetime.date(2026, 11, 15)


def test_ordem_prioridade_ligas_e_reagrupamento_brt(tmp_path, monkeypatch):
    # Verifica ordenação: Brasileiro Serie A -> Champions -> Premier League -> MLS
    md_ordem = futebol.formatar_resultados(
        {"events": [
            {
                "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/10/league/usa.1"}],
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": "2", "team": {"shortDisplayName": "Miami", "name": "Inter Miami"}},
                    {"homeAway": "away", "score": "1", "team": {"shortDisplayName": "LAFC", "name": "LAFC"}},
                ]}],
            },
            {
                "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/11/league/eng.1"}],
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Arsenal", "name": "Arsenal"}},
                    {"homeAway": "away", "score": "1", "team": {"shortDisplayName": "Chelsea", "name": "Chelsea"}},
                ]}],
            },
            {
                "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/12/league/uefa.champions"}],
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": "3", "team": {"shortDisplayName": "Real Madrid", "name": "Real Madrid"}},
                    {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Milan", "name": "AC Milan"}},
                ]}],
            },
            {
                "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/13/league/bra.1"}],
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": "2", "team": {"shortDisplayName": "Flamengo", "name": "Flamengo"}},
                    {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Vasco", "name": "Vasco"}},
                ]}],
            },
        ]},
        datetime.date(2026, 11, 15),
    )
    pos_bra = md_ordem.index("### Brasileiro Serie A")
    pos_ucl = md_ordem.index("### UEFA Champions League")
    pos_epl = md_ordem.index("### English Premier League")
    pos_mls = md_ordem.index("### Major League Soccer")
    assert pos_bra < pos_ucl < pos_epl < pos_mls

    # Verifica agrupamento BRT e captura de jogo tardio via espiada do dia seguinte sem duplicar ID
    ev_sexta_tardio = {
        "id": "999",
        "date": "2026-11-14T01:30Z",  # 22h30 BRT de sexta 13/11
        "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/999/league/bra.1"}],
        "competitions": [{"status": {"type": {"completed": True}}, "competitors": [
            {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Santos", "name": "Santos"}},
            {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Grêmio", "name": "Grêmio"}},
        ]}],
    }
    ev_domingo_tardio = {
        "id": "1000",
        "date": "2026-11-16T01:00Z",  # 22h00 BRT de domingo 15/11
        "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/1000/league/bra.1"}],
        "competitions": [{"status": {"type": {"completed": True}}, "competitors": [
            {"homeAway": "home", "score": "3", "team": {"shortDisplayName": "Bahia", "name": "Bahia"}},
            {"homeAway": "away", "score": "2", "team": {"shortDisplayName": "Botafogo", "name": "Botafogo"}},
        ]}],
    }

    def fake_fetcher(data):
        if data == datetime.date(2026, 11, 14):
            return {"events": [ev_sexta_tardio]}
        if data == datetime.date(2026, 11, 16):
            return {"events": [ev_domingo_tardio]}
        return {"events": []}

    controle = tmp_path / "controle_brt.json"
    futebol.salvar_ultima_data(datetime.date(2026, 11, 12), controle)
    monkeypatch.setattr(futebol, "_buscar_scoreboard_espn", fake_fetcher)
    md_brt, ultima_ok = futebol.coletar_resultados(datetime.date(2026, 11, 16), caminho_controle=controle)
    assert "## RESULTADOS RECENTES (13/11/2026)\n\n### Brasileiro Serie A\n- Santos 1 x 0 Grêmio" in md_brt
    assert "## RESULTADOS RECENTES (15/11/2026)\n\n### Brasileiro Serie A\n- Bahia 3 x 2 Botafogo" in md_brt
    assert md_brt.count("Santos 1 x 0 Grêmio") == 1
    assert ultima_ok == datetime.date(2026, 11, 15)


def test_concacaf_nations_e_novas_selecoes_destaque():
    dados = {"events": [
        {
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/20/league/fifa.friendly"}],
            "competitions": [{"altGameNote": "Men's International Friendly", "competitors": [
                {"homeAway": "home", "score": "2", "team": {"shortDisplayName": "Japan", "name": "Japan"}},
                {"homeAway": "away", "score": "1", "team": {"shortDisplayName": "Paraguay", "name": "Paraguay"}},
            ]}],
        },
        {
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/21/league/fifa.friendly"}],
            "competitions": [{"altGameNote": "Men's International Friendly", "competitors": [
                {"homeAway": "home", "score": "3", "team": {"shortDisplayName": "Norway", "name": "Norway"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Kazakhstan", "name": "Kazakhstan"}},
            ]}],
        },
        {
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/22/league/concacaf.nations.league"}],
            "competitions": [{"altGameNote": "Quarterfinals", "competitors": [
                {"homeAway": "home", "score": "4", "team": {"shortDisplayName": "USA", "name": "United States"}},
                {"homeAway": "away", "score": "1", "team": {"shortDisplayName": "Jamaica", "name": "Jamaica"}},
            ]}],
        },
        {
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/23/league/uefa.nations"}],
            "competitions": [{"altGameNote": "League A", "competitors": [
                {"homeAway": "home", "score": "2", "team": {"shortDisplayName": "Wales", "name": "Wales"}},
                {"homeAway": "away", "score": "3", "team": {"shortDisplayName": "Portugal", "name": "Portugal"}},
            ]}],
        },
        # Jogo sem seleção destaque continua barrado
        {
            "links": [{"href": "https://www.espn.com/soccer/match/_/gameId/24/league/concacaf.nations.league"}],
            "competitions": [{"altGameNote": "Group Stage", "competitors": [
                {"homeAway": "home", "score": "1", "team": {"shortDisplayName": "Belize", "name": "Belize"}},
                {"homeAway": "away", "score": "0", "team": {"shortDisplayName": "Bonaire", "name": "Bonaire"}},
            ]}],
        },
    ]}
    md = futebol.formatar_resultados(dados, datetime.date(2026, 11, 17))
    assert "### UEFA Nations League\n- Wales 2 x 3 Portugal" in md
    assert "### CONCACAF Nations League\n- USA 4 x 1 Jamaica" in md
    assert "- Japan 2 x 1 Paraguay" in md
    assert "- Norway 3 x 0 Kazakhstan" in md
    assert "Belize" not in md
    assert md.index("### UEFA Nations League") < md.index("### CONCACAF Nations League") < md.index("### Men's International Friendly")

