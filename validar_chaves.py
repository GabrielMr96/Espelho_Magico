import os
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError

def validar_chaves():
    load_dotenv()
    
    chaves = os.environ.get("GEMINI_API_KEY", "").split(",")
    if not chaves or chaves == [""]:
        print("❌ Nenhuma chave GEMINI_API_KEY encontrada no arquivo .env")
        return

    print(f"🔍 Encontradas {len(chaves)} chaves Gemini no .env. Iniciando validação rápida...\n")

    for i, chave in enumerate(chaves):
        chave = chave.strip()
        print(f"🔑 Chave {i+1} (termina em ...{chave[-4:] if len(chave) > 4 else chave}):")
        
        if not chave:
            print("   ❌ Chave vazia ignorada.\n")
            continue
            
        try:
            # Inicializa o client apenas para esta chave
            client = genai.Client(api_key=chave)
            
            # Testa listar os modelos (requisição mais leve e rápida possível)
            modelos = list(client.models.list())
            
            print("   ✅ VÁLIDA e Ativa!")
            print(f"   ℹ️ Tem acesso a {len(modelos)} modelos.")
            
        except APIError as e:
            if "429" in str(e) or "quota" in str(e).lower():
                print("   ⚠️ COTA ESGOTADA (Rate Limit 429). A chave é válida, mas excedeu o limite de requisições.")
            elif "400" in str(e) or "API key not valid" in str(e):
                print("   ❌ INVÁLIDA (Chave incorreta ou revogada).")
            else:
                print(f"   ⚠️ ERRO DESCONHECIDO DA API: {e}")
        except Exception as e:
            print(f"   ❌ ERRO DE CONEXÃO: {e}")
            
        print("-" * 50)

if __name__ == "__main__":
    validar_chaves()
