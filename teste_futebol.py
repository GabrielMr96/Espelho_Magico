import sys
import datetime
from orquestrador import buscar_resultados_futebol

hoje = datetime.datetime.now()
ontem = hoje - datetime.timedelta(days=1)
data_str_br = ontem.strftime("%d/%m/%Y")
dias_semana = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
dia_semana = dias_semana[ontem.weekday()]

print(f"Testando extração isolada para: {data_str_br} ({dia_semana})\n")
resultado = buscar_resultados_futebol(data_str_br, dia_semana)

print("\n--- RESULTADO OBTIDO ---\n")
print(resultado)
