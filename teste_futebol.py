import requests
import datetime
import re

def buscar_resultados_espn(data_alvo):
    # Formata a data para YYYYMMDD (ex: 20231024)
    data_str_api = data_alvo.strftime("%Y%m%d")
    data_str_br = data_alvo.strftime("%d/%m/%Y")
    
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?dates={data_str_api}&limit=500"
    
    try:
        resposta = requests.get(url, timeout=10)
        resposta.raise_for_status()
        dados = resposta.json()
    except Exception as e:
        return f"⚠️ Erro ao buscar API ESPN: {e}"

    if not dados.get("events"):
        return f"Nenhum jogo relevante encontrado para {data_str_br}."

    # Ligas de primeira divisão e torneios de elite
    ligas_principais_permitidas = [
        "brasileiro serie a", "copa do brasil", "copa libertadores", "copa sudamericana",
        "english premier league", "spanish laliga", "laliga", "german bundesliga", 
        "italian serie a", "french ligue 1", "uefa champions league", "uefa europa league",
        "copa america", "uefa european championship", "fifa world cup", "fifa club world cup",
        "conmebol copa america", "world cup qualifying", "eliminatórias"
    ]

    # Clubes gigantes mundiais / brasileiros para filtrar ligas secundárias ou amistosos de peso
    clubes_destaque = {
        # Brasil
        "flamengo", "palmeiras", "corinthians", "são paulo", "sao paulo", "santos", "grêmio", 
        "gremio", "internacional", "atlético-mg", "atletico-mg", "cruzeiro", "botafogo", 
        "fluminense", "vasco", "bahia", "fortaleza", "athletico-pr", "coritiba",
        # Europa / Mundo
        "real madrid", "barcelona", "manchester city", "manchester united", "liverpool", 
        "arsenal", "chelsea", "bayern munich", "bayern", "borussia dortmund", "dortmund", 
        "paris saint-germain", "psg", "juventus", "inter milan", "milan", "ac milan",
        "benfica", "porto", "sporting cp", "al nassr", "al hilal", "inter miami"
    }

    resultado_md = f"## RESULTADOS RECENTES ({data_str_br})\n"
    jogos_por_liga = {}

    for evento in dados.get("events", []):
        nome_liga = "Outros"
        if "competitions" in evento and evento["competitions"]:
            try:
                nome_liga = evento["season"]["slug"]
            except KeyError:
                pass

        liga_nome_amigavel = nome_liga.replace('-', ' ').title()
        liga_nome_amigavel = re.sub(r'^\d{4}(\s+\d{2})?\s+', '', liga_nome_amigavel)
        liga_lower = liga_nome_amigavel.lower()
        
        # Ignorar ligas femininas/universitárias se caírem no filtro genérico
        if "womens" in liga_lower or "ncaa" in liga_lower or "2 bundesliga" in liga_lower or "ligue 2" in liga_lower or "championship" in liga_lower:
            continue
            
        # Bloquear expressamente ligas periféricas que contenham "premier league" ou "serie a" no nome
        ligas_excluidas = [
            "russian", "chinese", "scottish", "bolivian", "chile", "allsvenskan", "eliteserien",
            "turkish", "colombian", "mexican", "peruvian", "paraguayan", "uruguayan", "venezuelan",
            "ecuadorian", "austrian", "greek", "danish", "swiss", "clausura", "apertura"
        ]
        if any(exc in liga_lower for exc in ligas_excluidas):
            continue

        competicao = evento["competitions"][0]
        times = competicao["competitors"]
        
        time_home = next((t for t in times if t["homeAway"] == "home"), times[0])
        time_away = next((t for t in times if t["homeAway"] == "away"), times[1])
        
        nome_1 = time_home["team"]["shortDisplayName"]
        placar_1 = time_home.get("score", "")
        
        nome_2 = time_away["team"]["shortDisplayName"]
        placar_2 = time_away.get("score", "")
        
        # Critério de relevância:
        # 1. É uma das ligas de elite (Brasileirão Série A, Champions, Premier League, LaLiga, etc.)
        # 2. OU envolve pelo menos um dos times de grande apelo/audiência
        eh_liga_elite = any(liga_alvo in liga_lower for liga_alvo in ligas_principais_permitidas)
        
        times_nomes = [
            nome_1.lower(), 
            nome_2.lower(), 
            time_home["team"].get("name", "").lower(), 
            time_away["team"].get("name", "").lower()
        ]
        
        def match_time_destaque(nome):
            for c in clubes_destaque:
                if c == "inter miami":
                    if "inter miami" in nome or ("miami" in nome and "inter" in nome):
                        return True
                    continue
                if c == "inter milan" or c == "internacional":
                    if "bogota" in nome or "bogotá" in nome:
                        continue
                if "sarandi" in nome or "sarandí" in nome:
                    continue
                if nome == c or f" {c} " in f" {nome} ":
                    return True
            return False

        tem_time_destaque = any(match_time_destaque(tn) for tn in times_nomes)

        if not eh_liga_elite and not tem_time_destaque:
            continue

        if "regular season" in liga_lower:
            liga_nome_amigavel = "Major League Soccer"

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
