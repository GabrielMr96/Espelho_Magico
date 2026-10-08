import requests
import datetime

def buscar_resultados_espn(data_alvo):
    # Formata a data para YYYYMMDD (ex: 20231024)
    data_str_api = data_alvo.strftime("%Y%m%d")
    data_str_br = data_alvo.strftime("%d/%m/%Y")
    
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?dates={data_str_api}"
    
    try:
        resposta = requests.get(url, timeout=10)
        resposta.raise_for_status()
        dados = resposta.json()
    except Exception as e:
        return f"⚠️ Erro ao buscar API ESPN: {e}"

    if not dados.get("events"):
        return f"Nenhum jogo relevante encontrado para {data_str_br}."

    # Ligas principais para filtrar
    ligas_importantes = [
        "brasileiro", "copa do brasil", "libertadores", "sudamericana", "champions", 
        "premier league", "laliga", "serie a", "bundesliga", "ligue 1", "copa america", 
        "euro", "world cup", "paulista", "carioca", "mineiro", "gaucho", "copa do nordeste", "copa sul-americana"
    ]

    resultado_md = f"## RESULTADOS RECENTES ({data_str_br})\n"
    jogos_por_liga = {}

    for evento in dados.get("events", []):
        nome_liga = "Outros"
        if "competitions" in evento and evento["competitions"]:
            try:
                nome_liga = evento["season"]["slug"]
            except KeyError:
                pass

        # Adicionar o nome da liga amigável
        liga_nome_amigavel = nome_liga.replace('-', ' ').title()
        
        # Filtro: só aceita se o nome da liga contiver alguma das strings importantes
        liga_lower = liga_nome_amigavel.lower()
        if not any(importante in liga_lower for importante in ligas_importantes):
            continue

        competicao = evento["competitions"][0]
        times = competicao["competitors"]
        
        time_home = next((t for t in times if t["homeAway"] == "home"), times[0])
        time_away = next((t for t in times if t["homeAway"] == "away"), times[1])
        
        nome_1 = time_home["team"]["shortDisplayName"]
        placar_1 = time_home.get("score", "")
        
        nome_2 = time_away["team"]["shortDisplayName"]
        placar_2 = time_away.get("score", "")
        
        if liga_nome_amigavel not in jogos_por_liga:
            jogos_por_liga[liga_nome_amigavel] = []
            
        jogos_por_liga[liga_nome_amigavel].append(f"{nome_1} {placar_1} x {placar_2} {nome_2}")

    # Monta o Markdown Final
    if not jogos_por_liga:
        return f"Nenhum jogo dos principais campeonatos encontrado para {data_str_br}."

    for liga, jogos in jogos_por_liga.items():
        resultado_md += f"\n### {liga}\n"
        for jogo in jogos:
            resultado_md += f"- {jogo}\n"

    return resultado_md

if __name__ == "__main__":
    ontem = datetime.datetime.now() - datetime.timedelta(days=1)
    print("Testando extração isolada via ESPN API para ontem:")
    resultado = buscar_resultados_espn(ontem)
    
    print("\n--- RESULTADO OBTIDO ---\n")
    print(resultado)
