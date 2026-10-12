"""Placares de futebol via API pública da ESPN (usado no Gol Contra)."""
import datetime
import json
import re

import requests

from . import config
from .log import escape, logger

DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
URL_ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?dates={data}&limit=500"


class ErroESPN(Exception):
    pass


def eh_time_destaque(nome, clubes_destaque):
    """Casa nomes de clubes evitando falsos positivos (Arsenal Sarandí, Inter Bogotá...)."""
    for c in clubes_destaque:
        if c == "inter miami":
            if "inter miami" in nome or ("miami" in nome and "inter" in nome):
                return True
            continue
        if c in ("inter milan", "internacional") and ("bogota" in nome or "bogotá" in nome):
            continue
        if "sarandi" in nome or "sarandí" in nome:
            continue
        if nome == c or f" {c} " in f" {nome} ":
            return True
    return False


def nome_liga_amigavel(evento):
    slug = (evento.get("season") or {}).get("slug") or "Outros"
    nome = slug.replace("-", " ").title()
    return re.sub(r"^\d{4}(\s+\d{2})?\s+", "", nome)


def formatar_resultados(dados, data, filtros=None):
    """Transforma o JSON do scoreboard da ESPN em Markdown com os jogos relevantes."""
    filtros = filtros or config.FUTEBOL
    ligas_permitidas = filtros["ligas_permitidas"]
    termos_ignorados = filtros["termos_ligas_ignoradas"]
    ligas_excluidas = filtros["ligas_excluidas"]
    clubes = set(filtros["clubes_destaque"])
    data_br = data.strftime("%d/%m/%Y")

    eventos = dados.get("events") or []
    if not eventos:
        return f"Nenhum jogo relevante encontrado para {data_br}."

    jogos_por_liga = {}
    for evento in eventos:
        competicoes = evento.get("competitions") or []
        if not competicoes:
            continue
        liga = nome_liga_amigavel(evento)
        liga_lower = liga.lower()

        if any(t in liga_lower for t in termos_ignorados):
            continue
        if any(exc in liga_lower for exc in ligas_excluidas):
            continue

        times = competicoes[0].get("competitors") or []
        if len(times) < 2:
            continue
        casa = next((t for t in times if t.get("homeAway") == "home"), times[0])
        fora = next((t for t in times if t.get("homeAway") == "away"), times[1])

        nome_casa = casa["team"].get("shortDisplayName") or casa["team"].get("name", "?")
        nome_fora = fora["team"].get("shortDisplayName") or fora["team"].get("name", "?")

        eh_elite = any(alvo in liga_lower for alvo in ligas_permitidas)
        nomes = [
            nome_casa.lower(), nome_fora.lower(),
            casa["team"].get("name", "").lower(), fora["team"].get("name", "").lower(),
        ]
        if not eh_elite and not any(eh_time_destaque(n, clubes) for n in nomes):
            continue

        if "regular season" in liga_lower:
            liga = "Major League Soccer"

        jogos_por_liga.setdefault(liga, []).append(
            f"{nome_casa} {casa.get('score', '')} x {fora.get('score', '')} {nome_fora}"
        )

    if not jogos_por_liga:
        return f"Nenhum jogo dos principais campeonatos encontrado para {data_br}."

    linhas = [f"## RESULTADOS RECENTES ({data_br})"]
    for liga, jogos in jogos_por_liga.items():
        linhas.append(f"\n### {liga}")
        linhas.extend(f"- {j}" for j in jogos)
    return "\n".join(linhas) + "\n"


def buscar_resultados_futebol(data):
    """Busca e formata os resultados de uma data. Lança ErroESPN se a API falhar."""
    logger.info(f"[cyan]➜ Buscando placares do dia {data:%d/%m/%Y} ({DIAS_SEMANA[data.weekday()]}) na ESPN...[/cyan]")
    try:
        resposta = requests.get(URL_ESPN.format(data=data.strftime("%Y%m%d")), timeout=10)
        resposta.raise_for_status()
        dados = resposta.json()
    except Exception as e:
        raise ErroESPN(f"Erro ao buscar API ESPN para {data:%d/%m/%Y}: {e}") from e
    return formatar_resultados(dados, data)


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


def datas_pendentes(ultima_data, hoje):
    """Datas de (ultima_data + 1) até ontem. Sem histórico, só ontem."""
    ontem = hoje - datetime.timedelta(days=1)
    inicio = (ultima_data + datetime.timedelta(days=1)) if ultima_data else ontem
    datas = []
    atual = inicio
    while atual <= ontem:
        datas.append(atual)
        atual += datetime.timedelta(days=1)
    return datas


def coletar_resultados(hoje=None, buscar=buscar_resultados_futebol, caminho_controle=None):
    """Busca os dias pendentes.

    Retorna (markdown, ultima_data_ok). A data só avança até o último dia consecutivo
    obtido com sucesso, e erros da ESPN não entram no texto.
    """
    hoje = hoje or datetime.date.today()
    pendentes = datas_pendentes(ler_ultima_data(caminho_controle), hoje)
    blocos = []
    ultima_ok = None
    for data in pendentes:
        try:
            blocos.append(buscar(data))
            ultima_ok = data
        except ErroESPN as e:
            logger.warning(f"[yellow]⚠ {escape(str(e))}. Os dias a partir desta data serão buscados na próxima execução.[/yellow]")
            break
    return "\n\n".join(blocos), ultima_ok
