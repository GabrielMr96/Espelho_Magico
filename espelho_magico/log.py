"""Logging unificado: console (Rich, com cores) + arquivo rotativo (texto puro)."""
import logging
import sys
import threading
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.text import Text

from . import config

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

console = Console()
logger = logging.getLogger("espelho_magico")
# Relatórios detalhados (ex.: dupla verificação) vão só para o arquivo de log, sem poluir a tela
log_arquivo = logging.getLogger("espelho_magico_relatorio")
log_arquivo.setLevel(logging.INFO)

__all__ = ["console", "logger", "log_arquivo", "escape", "configurar_logging", "contexto_grupo"]

_contexto = threading.local()


@contextmanager
def contexto_grupo(rotulo):
    """Prefixa todas as mensagens desta thread com o programa em processamento (ex.: 'Gol Contra #198').

    Com vários programas em paralelo, cada linha do terminal mostra de qual programa ela é.
    """
    anterior = getattr(_contexto, "rotulo", None)
    _contexto.rotulo = rotulo
    try:
        yield
    finally:
        _contexto.rotulo = anterior


class _FiltroContexto(logging.Filter):
    def filter(self, record):
        rotulo = getattr(_contexto, "rotulo", None)
        if rotulo and isinstance(record.msg, str) and not getattr(record, "_com_contexto", False):
            msg = record.msg
            quebras = len(msg) - len(msg.lstrip("\n"))
            record.msg = f"{chr(10) * quebras}[dim]┃[/dim] [bold magenta]{escape(rotulo)}[/bold magenta] [dim]›[/dim] {msg.lstrip(chr(10))}"
            record._com_contexto = True
        return True


class _FormatterSemMarkup(logging.Formatter):
    """Remove as tags de cor do Rich antes de gravar no arquivo."""

    def format(self, record):
        texto = super().format(record)
        try:
            return Text.from_markup(texto).plain
        except Exception:
            return texto


_configurado = False


def configurar_logging():
    global _configurado
    if _configurado:
        return
    _configurado = True

    # Root: só recebe WARNING+ de bibliotecas, mas o handler de arquivo grava tudo que propagar.
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    arquivo = RotatingFileHandler(config.LOG_PATH, maxBytes=500_000, backupCount=3, encoding="utf-8")
    arquivo.setLevel(logging.INFO)
    arquivo.setFormatter(_FormatterSemMarkup("%(asctime)s - %(levelname)s - %(threadName)s - %(message)s"))
    root.addHandler(arquivo)

    tela = RichHandler(
        console=console,
        markup=True,
        show_time=False,
        show_path=False,
        show_level=False,
        rich_tracebacks=True,
    )
    tela.setLevel(logging.INFO)
    logger.setLevel(logging.INFO)
    logger.addHandler(tela)
    logger.addFilter(_FiltroContexto())

    for ruidoso in ("pdfminer", "httpx", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.ERROR)
