"""Placares de futebol via API pública da ESPN (usado no Gol Contra)."""
import datetime
import json
import re

import requests

from . import config
from .log import escape, logger

DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
URL_ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?dates={data}&limit=500"
_FUSO_BRT = datetime.timezone(datetime.timedelta(hours=-3))
_ORDEM_LIGAS = (
    "brasileiro serie a", "copa do brasil", "copa libertadores", "copa sudamericana",
    "fifa world cup", "conmebol copa america", "copa america", "uefa european championship",
    "fifa club world cup", "uefa champions league", "uefa europa league",
    "uefa nations league", "world cup qualifying", "eliminatorias", "eliminatórias",
    "mens international friendly", "english premier league", "fa cup", "efl cup",
    "laliga", "spanish laliga", "copa del rey", "italian serie a", "coppa italia",
    "german bundesliga", "dfb pokal", "ligue 1", "french ligue 1",
    "portuguese primeira liga", "saudi pro league", "major league soccer",
)


class ErroESPN(Exception):
    pass


_RE_NAO_PROFISSIONAL = re.compile(
    r"(?:\b(?:women'?s?|womens|woman|vrouwen|femenil|femenina|feminina|feminino|"
    r"feminin|f[eé]minin|femen[ií]|frauen|dames|femminile|liga\s+f|"
    r"premi[eè]re\s+ligue|arkema|nwsl|wsl|shebelieves|"
    r"u[-\s]?(?:2[0-3]|1[4-9])|sub[-\s]?(?:2[0-3]|1[4-9])|under[-\s]?(?:2[0-3]|1[4-9])|"
    r"youth|juniores|juvenil|copinha|ncaa|ncaam|ncaaw|college|university|"
    r"olympics?|olimp[ií]adas|reserves?|premier\s+league\s+2|mls\s+next|ii)\b|\(\s*w\s*\))",
    re.I,
)
_RE_CODIGO_NAO_PRO = re.compile(
    r"(?:\.w(?:\.|$)|(?:_|\.)u(?:2[0-3]|1[4-9])|wchampions|wwc|nwsl|uwcl|youth|ncaa|olymp)",
    re.I,
)


def _norm_liga(texto):
    return (texto or "").lower().replace("’", "").replace("'", "").strip()


def _liga_no_conjunto(liga_lower, alvos):
    norm = _norm_liga(liga_lower)
    for a in alvos:
        alvo = _norm_liga(a)
        if norm == alvo or norm.startswith((f"{alvo},", f"{alvo} -", f"{alvo}:")):
            return True
    return False


def _eh_jogo_profissional(evento, code, liga_lower, nomes_times):
    """Rejeita campeonatos ou equipes femininas, de base (<= Sub-23), universitárias ou reservas."""
    if code and _RE_CODIGO_NAO_PRO.search(code):
        return False
    comp = (evento.get("competitions") or [{}])[0]
    trechos = [
        liga_lower,
        (comp.get("altGameNote") or "").lower(),
        ((evento.get("season") or {}).get("slug") or "").lower(),
        *nomes_times,
    ]
    return not any(_RE_NAO_PROFISSIONAL.search(t) for t in trechos if t)


def _jogo_concluido(evento, competicao):
    status = (competicao.get("status") or evento.get("status") or {}).get("type") or {}
    if "completed" in status:
        return bool(status["completed"])
    return True


def eh_time_destaque(nome, clubes_destaque):
    """Casa nomes de clubes evitando falsos positivos (Arsenal Sarandí/Tula, Inter Bogotá, Botafogo-SP...)."""
    nome_limpo = (nome or "").lower()
    for c in clubes_destaque:
        clube = c.lower()
        if clube == "inter miami":
            if "inter miami" in nome_limpo or ("miami" in nome_limpo and "inter" in nome_limpo):
                return True
            continue
        if clube in ("inter milan", "internacional") and any(
            x in nome_limpo for x in ("bogota", "bogotá", "tecla", "st ", "turku", "limeira")
        ):
            continue
        if clube == "botafogo" and any(x in nome_limpo for x in ("botafogo-sp", "botafogo sp", "botafogo-pb", "botafogo pb")):
            continue
        if any(x in nome_limpo for x in ("sarandi", "sarandí", "Arsenal Tula".lower())):
            if clube == "arsenal":
                continue
        if clube == "santos" and "laguna" in nome_limpo:
            continue
        if clube == "al hilal" and "omdurman" in nome_limpo:
            continue
        if nome_limpo == clube or f" {clube} " in f" {nome_limpo} ":
            return True
    return False


GENERIC_STAGES = {
    "regular season", "league phase", "league stage", "group stage",
    "semifinals", "semi-finals", "round of 16", "final stage",
    "first round", "second round", "second preliminary round",
    "apertura", "clausura", "fall season", "outros", "fase de grupos",
}

LEAGUE_CODE_MAP = {
    "eng.1": "English Premier League",
    "eng.fa": "FA Cup",
    "eng.league_cup": "EFL Cup",
    "esp.1": "Laliga",
    "esp.copa_del_rey": "Copa del Rey",
    "ger.1": "German Bundesliga",
    "ger.dfb_pokal": "DFB Pokal",
    "ita.1": "Italian Serie A",
    "ita.coppa_italia": "Coppa Italia",
    "fra.1": "Ligue 1",
    "bra.1": "Brasileiro Serie A",
    "bra.2": "Brasileiro Serie B",
    "bra.copa_do_brasil": "Copa do Brasil",
    "por.1": "Portuguese Primeira Liga",
    "sau.1": "Saudi Pro League",
    "usa.1": "Major League Soccer",
    "uefa.champions": "UEFA Champions League",
    "uefa.europa": "UEFA Europa League",
    "uefa.europa.conf": "UEFA Conference League",
    "uefa.nations": "UEFA Nations League",
    "uefa.euro": "UEFA European Championship",
    "conmebol.libertadores": "Copa Libertadores",
    "conmebol.sudamericana": "Copa Sudamericana",
    "conmebol.america": "Copa America",
    "fifa.world": "FIFA World Cup",
    "fifa.cwc": "FIFA Club World Cup",
    "afc.champions": "AFC Champions League Elite",
    "afc.champions_elite": "AFC Champions League Elite",
}


def extrair_codigo_liga(evento):
    """Extrai o código da liga presente nos links do evento ESPN (/league/<slug>)."""
    for link in evento.get("links") or []:
        href = link.get("href") or ""
        m = re.search(r"/league/([a-zA-Z0-9\._\-]+)", href)
        if m:
            return m.group(1).lower()
    return None


def nome_liga_amigavel(evento):
    """Identifica o nome amigável da liga a partir do código do link, altGameNote ou slug da season."""
    code = extrair_codigo_liga(evento)
    if code and code in LEAGUE_CODE_MAP:
        return LEAGUE_CODE_MAP[code]

    comp = (evento.get("competitions") or [{}])[0]
    alt = comp.get("altGameNote")
    if alt:
        alt_liga = alt.split(",")[0].strip()
        if alt_liga.upper() == "MLS":
            return "Major League Soccer"
        if "libertadores" in alt_liga.lower():
            return "Copa Libertadores"
        if "sudamericana" in alt_liga.lower():
            return "Copa Sudamericana"
        if "champions league" in alt_liga.lower() and "uefa" in alt_liga.lower():
            return "UEFA Champions League"
        if "europa league" in alt_liga.lower() and "uefa" in alt_liga.lower():
            return "UEFA Europa League"
        if alt_liga.lower() not in GENERIC_STAGES:
            return alt_liga

    slug = (evento.get("season") or {}).get("slug") or ""
    nome = slug.replace("-", " ").title()
    nome_limpo = re.sub(r"^\d{4}(\s+\d{2})?\s+", "", nome).strip()
    if nome_limpo and nome_limpo.lower() not in GENERIC_STAGES:
        return nome_limpo

    if code:
        return code.upper()
    return "Outros"


def formatar_resultados(dados, data, filtros=None):
    """Transforma o JSON do scoreboard da ESPN em Markdown com os jogos profissionais e populares."""
    filtros = filtros or config.FUTEBOL
    ligas_todos = filtros.get("ligas_todos_jogos", ["brasileiro serie a"])
    ligas_populares = filtros.get("ligas_populares") or filtros.get("ligas_permitidas", [])
    ligas_selecoes = filtros.get("ligas_selecoes", [])
    termos_ignorados = filtros.get("termos_ligas_ignoradas", [])
    ligas_excluidas = filtros.get("ligas_excluidas", [])
    clubes = set(filtros.get("clubes_destaque", []))
    selecoes = set(filtros.get("selecoes_destaque", []))
    data_br = data.strftime("%d/%m/%Y")

    eventos = dados.get("events") or []
    if not eventos:
        return f"Nenhum jogo relevante encontrado para {data_br}."

    jogos_por_liga = {}
    for evento in eventos:
        competicoes = evento.get("competitions") or []
        if not competicoes or not _jogo_concluido(evento, competicoes[0]):
            continue
        code = extrair_codigo_liga(evento)
        liga = nome_liga_amigavel(evento)
        liga_lower = liga.lower()

        if any(t in liga_lower for t in termos_ignorados) or any(exc in liga_lower for exc in ligas_excluidas):
            continue

        times = competicoes[0].get("competitors") or []
        if len(times) < 2:
            continue
        casa = next((t for t in times if t.get("homeAway") == "home"), times[0])
        fora = next((t for t in times if t.get("homeAway") == "away"), times[1])

        nome_casa = casa["team"].get("shortDisplayName") or casa["team"].get("name", "?")
        nome_fora = fora["team"].get("shortDisplayName") or fora["team"].get("name", "?")
        nomes = [
            nome_casa.lower(), nome_fora.lower(),
            casa["team"].get("name", "").lower(), fora["team"].get("name", "").lower(),
            casa["team"].get("displayName", "").lower(), fora["team"].get("displayName", "").lower(),
        ]
        if not _eh_jogo_profissional(evento, code, liga_lower, nomes):
            continue

        eh_todos = _liga_no_conjunto(liga_lower, ligas_todos)
        eh_popular = _liga_no_conjunto(liga_lower, ligas_populares)
        eh_selecao = _liga_no_conjunto(liga_lower, ligas_selecoes)

        if eh_todos:
            permitido = True
        elif eh_popular:
            permitido = any(eh_time_destaque(n, clubes) for n in nomes)
        elif eh_selecao:
            permitido = any(eh_time_destaque(n, selecoes) for n in nomes)
        else:
            permitido = False

        if not permitido:
            continue

        score_casa = str(casa.get("score", "") or "").strip()
        score_fora = str(fora.get("score", "") or "").strip()
        if not score_casa or not score_fora:
            continue

        jogos_por_liga.setdefault(liga, []).append(
            f"{nome_casa} {score_casa} x {score_fora} {nome_fora}"
        )

    if not jogos_por_liga:
        return f"Nenhum jogo dos principais campeonatos encontrado para {data_br}."

    linhas = [f"## RESULTADOS RECENTES ({data_br})"]
    for liga in sorted(jogos_por_liga, key=_ordem_liga):
        linhas.append(f"\n### {liga}")
        linhas.extend(f"- {j}" for j in jogos_por_liga[liga])
    return "\n".join(linhas) + "\n"


def _ordem_liga(liga):
    """Ordena campeonatos por relevância para o ouvinte brasileiro (Série A -> continental -> Europa -> resto)."""
    norm = _norm_liga(liga)
    for idx, alvo in enumerate(_ORDEM_LIGAS):
        if norm == alvo or norm.startswith((f"{alvo},", f"{alvo} -", f"{alvo}:")):
            return (0, idx, norm)
    return (1, 0, norm)


def _data_brt_evento(evento, fallback_data):
    """Converte a data ISO UTC da ESPN para a data do calendário em Brasília (UTC-3)."""
    bruto = (evento.get("date") or "").strip()
    if not bruto:
        return fallback_data
    try:
        dt = datetime.datetime.fromisoformat(bruto.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_FUSO_BRT).date()
    except ValueError:
        return fallback_data


def _buscar_scoreboard_espn(data):
    """Busca o JSON bruto do scoreboard da ESPN para uma data. Lança ErroESPN se falhar."""
    logger.info(f"[cyan]➜ Buscando placares do dia {data:%d/%m/%Y} ({DIAS_SEMANA[data.weekday()]}) na ESPN...[/cyan]")
    try:
        resposta = requests.get(URL_ESPN.format(data=data.strftime("%Y%m%d")), timeout=10)
        resposta.raise_for_status()
        return resposta.json()
    except Exception as e:
        raise ErroESPN(f"Erro ao buscar API ESPN para {data:%d/%m/%Y}: {e}") from e


def buscar_resultados_futebol(data):
    """Busca e formata os resultados de uma data. Lança ErroESPN se a API falhar."""
    return formatar_resultados(_buscar_scoreboard_espn(data), data)


# --- Controle de datas já extraídas --------------------------------------------

def ler_ultima_data(caminho=None):
    caminho = caminho or config.CONTROLE_GOL_PATH
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            valor = json.load(f).get("ultima_data_extraida")
        return datetime.datetime.strptime(valor, "%Y-%m-%d").date() if valor else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"[yellow]⚠ Erro ao ler {caminho}: {escape(str(e))}[/yellow]")
        return None


def salvar_ultima_data(data, caminho=None):
    caminho = caminho or config.CONTROLE_GOL_PATH
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump({"ultima_data_extraida": data.strftime("%Y-%m-%d")}, f)


def datas_pendentes(ultima_data, hoje, dias_segunda=None, max_dias=None, garantir_segunda=None):
    """Datas de (ultima_data + 1) até ontem (limitadas por max_dias).

    Na segunda-feira sem histórico — ou com `garantir_janela_segunda` ativo e histórico
    de sexta-feira ou anterior — garante os 3 dias do fim de semana (sexta, sábado e domingo).
    """
    if dias_segunda is None:
        dias_segunda = config.FUTEBOL.get("dias_janela_segunda", 3)
    if max_dias is None:
        max_dias = config.FUTEBOL.get("dias_max_pendentes", 5)
    if garantir_segunda is None:
        garantir_segunda = config.FUTEBOL.get("garantir_janela_segunda", True)

    ontem = hoje - datetime.timedelta(days=1)
    janela_seg = hoje - datetime.timedelta(days=dias_segunda)
    if ultima_data is None:
        inicio = janela_seg if hoje.weekday() == 0 else ontem
    else:
        inicio = ultima_data + datetime.timedelta(days=1)
        if garantir_segunda and hoje.weekday() == 0 and ultima_data <= janela_seg:
            inicio = min(inicio, janela_seg)
        if max_dias and (hoje - inicio).days > max_dias:
            inicio = hoje - datetime.timedelta(days=max_dias)
    datas = []
    atual = inicio
    while atual <= ontem:
        datas.append(atual)
        atual += datetime.timedelta(days=1)
    return datas


def _coletar_espn_brt(pendentes, fetcher=None):
    """Busca scoreboards da ESPN para os dias pendentes + fronteira seguinte e reagrupa por data BRT."""
    fetcher = fetcher or _buscar_scoreboard_espn
    por_data = {d: [] for d in pendentes}
    vistos = set()
    ultima_ok = None

    def _registrar_eventos(eventos, data_fallback):
        for ev in eventos:
            chave = ev.get("id") or (ev.get("date"), ev.get("shortName"))
            if chave in vistos:
                continue
            vistos.add(chave)
            dt_brt = _data_brt_evento(ev, data_fallback)
            if dt_brt in por_data:
                por_data[dt_brt].append(ev)

    for data in pendentes:
        try:
            dados = fetcher(data)
            _registrar_eventos(dados.get("events") or [], data)
            ultima_ok = data
        except ErroESPN as e:
            logger.warning(f"[yellow]⚠ {escape(str(e))}. Os dias a partir desta data serão buscados na próxima execução.[/yellow]")
            break

    # Espia silenciosamente o dia seguinte ao último obtido com sucesso para capturar jogos tardios em UTC
    if ultima_ok is not None:
        dia_seguinte = ultima_ok + datetime.timedelta(days=1)
        try:
            dados_seg = fetcher(dia_seguinte)
            _registrar_eventos(dados_seg.get("events") or [], dia_seguinte)
        except ErroESPN:
            pass

    blocos = []
    for data in pendentes:
        if ultima_ok is None or data > ultima_ok:
            break
        bloco = formatar_resultados({"events": por_data[data]}, data)
        if bloco and "## RESULTADOS RECENTES" in bloco:
            blocos.append(bloco)
    return "\n\n".join(blocos), ultima_ok


def coletar_resultados(hoje=None, buscar=buscar_resultados_futebol, caminho_controle=None):
    """Busca os dias pendentes.

    Retorna (markdown, ultima_data_ok). A data só avança até o último dia consecutivo
    obtido com sucesso, e erros da ESPN não entram no texto.
    """
    hoje = hoje or datetime.date.today()
    pendentes = datas_pendentes(ler_ultima_data(caminho_controle), hoje)
    if not pendentes:
        return "", None
    if buscar is buscar_resultados_futebol:
        return _coletar_espn_brt(pendentes)
    blocos = []
    ultima_ok = None
    for data in pendentes:
        try:
            bloco = buscar(data)
            if bloco and "## RESULTADOS RECENTES" in bloco:
                blocos.append(bloco)
            ultima_ok = data
        except ErroESPN as e:
            logger.warning(f"[yellow]⚠ {escape(str(e))}. Os dias a partir desta data serão buscados na próxima execução.[/yellow]")
            break
    return "\n\n".join(blocos), ultima_ok
