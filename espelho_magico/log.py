"""Logging unificado: console (Rich, com cores) + arquivo rotativo (texto puro)."""
import logging
import sys
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

__all__ = ["console", "logger", "escape", "configurar_logging"]


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

    for ruidoso in ("pdfminer", "httpx", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.ERROR)
