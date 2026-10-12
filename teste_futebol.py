"""Teste isolado da busca de placares na ESPN (usa a mesma lógica do orquestrador)."""
import datetime

from espelho_magico.futebol import ErroESPN, buscar_resultados_futebol

if __name__ == "__main__":
    ontem = datetime.date.today() - datetime.timedelta(days=1)
    print("Testando extração isolada via ESPN API para ontem:")
    try:
        resultado = buscar_resultados_futebol(ontem)
    except ErroESPN as e:
        resultado = f"⚠️ {e}"
    print("\n--- RESULTADO OBTIDO ---\n")
    print(resultado)
