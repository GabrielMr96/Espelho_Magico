import os
import time
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError
from rich.console import Console
from rich.table import Table
import winsound

console = Console()

def monitorar():
    load_dotenv()
    
    api_keys_str = os.environ.get("GEMINI_API_KEY", "")
    api_keys = [k.strip() for k in api_keys_str.split(",") if k.strip()]
    
    if not api_keys:
        console.print("[bold red]Nenhuma GEMINI_API_KEY encontrada no .env.[/bold red]")
        return
    
    clientes = [genai.Client(api_key=k) for k in api_keys]
    modelo = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    
    console.print(f"[cyan]Monitorando a cota de {len(clientes)} chave(s) no modelo {modelo}...[/cyan]")
    console.print("[yellow]Pressione Ctrl+C para cancelar.[/yellow]\n")
    
    while True:
        chaves_disponiveis = []
        chaves_esgotadas = []
        
        for i, cliente in enumerate(clientes):
            nome_chave = f"Chave {i + 1}"
            try:
                # Faz uma chamada minúscula apenas para testar a cota
                cliente.models.generate_content(
                    model=modelo,
                    contents="Teste"
                )
                chaves_disponiveis.append(nome_chave)
            except APIError as e:
                erro_str = str(e)
                if "429" in erro_str or "RESOURCE_EXHAUSTED" in erro_str:
                    chaves_esgotadas.append(nome_chave)
                else:
                    console.print(f"\n[bold red]Erro diferente de cota na {nome_chave}: {erro_str}[/bold red]")
                    return
            except Exception as e:
                console.print(f"\n[bold red]Erro inesperado na {nome_chave}: {str(e)}[/bold red]")
                return
        
        if chaves_disponiveis:
            console.print(f"\n\n[bold green]✅ COTA RESTAURADA![/bold green]")
            
            tabela = Table(title="Status das Chaves")
            tabela.add_column("Chave", style="cyan")
            tabela.add_column("Status", style="bold")
            for c in chaves_disponiveis:
                tabela.add_row(c, "[green]✅ Disponível[/green]")
            for c in chaves_esgotadas:
                tabela.add_row(c, "[red]❌ Esgotada[/red]")
            console.print(tabela)
            
            console.print("Você já pode rodar o orquestrador novamente.")
            
            # Toca um som de alerta no Windows 3 vezes
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                time.sleep(0.5)
            break
        else:
            # Imprime um ponto para mostrar que está vivo e testando
            console.print(".", end="", style="yellow")
        
        time.sleep(60) # Espera 1 minuto antes de tentar de novo

if __name__ == "__main__":
    try:
        monitorar()
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitoramento cancelado pelo usuário.[/yellow]")
