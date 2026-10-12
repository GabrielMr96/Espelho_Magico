"""Ponto de entrada do Espelho Mágico.

Exemplos:
    python orquestrador.py
    python orquestrador.py --dry-run
    python orquestrador.py --grupo "tns sports" --unificar sim
    python orquestrador.py --sem-interacao   # para o Agendador de Tarefas
"""
import argparse
import sys

from espelho_magico.log import configurar_logging
from espelho_magico.pipeline import Opcoes, executar, interativo_disponivel


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Extrai newsletters do Gmail para Markdown limpo (NotebookLM).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Gera os arquivos em resultados_markdown/_dry_run sem subir ao Drive, "
                             "sem marcar e-mails como lidos e sem atualizar o controle do futebol.")
    parser.add_argument("--unificar", choices=["perguntar", "sim", "nao"], default="perguntar",
                        help="O que fazer quando um grupo tiver 2+ e-mails (padrão: perguntar).")
    parser.add_argument("--grupo", action="append", default=[], metavar="FILTRO",
                        help="Processa apenas grupos cujo prefixo ou título contenha FILTRO (pode repetir).")
    parser.add_argument("--sem-interacao", action="store_true",
                        help="Nunca faz perguntas (edição sem histórico usa a data; unificação segue --unificar).")
    parser.add_argument("--max-workers", type=int, default=0,
                        help="Grupos processados em paralelo (padrão: MAX_WORKERS do .env ou nº de chaves, até 3).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configurar_logging()
    opcoes = Opcoes(
        dry_run=args.dry_run,
        unificar=args.unificar,
        grupos=args.grupo,
        interativo=interativo_disponivel() and not args.sem_interacao,
        max_workers=args.max_workers,
    )
    return executar(opcoes)


if __name__ == "__main__":
    sys.exit(main())
