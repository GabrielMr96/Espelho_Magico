"""Configuração central: caminhos absolutos, variáveis de ambiente e config.json."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def _env_bool(nome, padrao=False):
    valor = os.environ.get(nome)
    if valor is None:
        return padrao
    return valor.strip().lower() in ("1", "true", "sim", "yes", "s")


def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except (TypeError, ValueError):
        return padrao


def _env_lista(nome):
    return [v.strip() for v in os.environ.get(nome, "").split(",") if v.strip()]


# --- Caminhos (independentes do diretório de execução) ---------------------
# ESPELHO_SECRETS_DIR permite guardar token/credentials fora da pasta sincronizada do OneDrive.
SECRETS_DIR = Path(os.environ.get("ESPELHO_SECRETS_DIR") or BASE_DIR)
TOKEN_PATH = SECRETS_DIR / "token.json"
CREDENTIALS_PATH = SECRETS_DIR / "credentials.json"

LOG_PATH = BASE_DIR / "espelho_magico.log"
RESULTADOS_DIR = BASE_DIR / "resultados_markdown"
DRY_RUN_DIR = RESULTADOS_DIR / "_dry_run"
CONTROLE_GOL_PATH = BASE_DIR / "controle_gol.json"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
CONFIG_JSON_PATH = BASE_DIR / "config.json"

# --- config.json -------------------------------------------------------------
with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as _f:
    _CONFIG = json.load(_f)

MAPEAMENTO_PREFIXOS = _CONFIG["mapeamento_prefixos"]
PREFIXO_FUTEBOL = _CONFIG.get("prefixo_futebol", "tns sports")
FUTEBOL = _CONFIG["futebol"]

# --- Google ------------------------------------------------------------------
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.modify",
]
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "Espelho_Magico")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID") or None

# --- IA ----------------------------------------------------------------------
GEMINI_API_KEYS = _env_lista("GEMINI_API_KEY")
# GEMINI_MODELS no .env (separado por vírgula) sobrescreve a lista do config.json
GEMINI_MODELS = list(dict.fromkeys(_env_lista("GEMINI_MODELS") or _CONFIG["gemini_models"]))
GEMINI_MAX_OUTPUT_TOKENS = _env_int("GEMINI_MAX_OUTPUT_TOKENS", 0) or None
IA_PADRAO = os.environ.get("IA_PADRAO", "gemini").lower()
PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY")
PERPLEXITY_MODEL = os.environ.get("PERPLEXITY_MODEL", "sonar-pro")

# Política de retentativas (pior caso fica na casa de minutos, não horas)
MAX_RODADAS = _env_int("MAX_RODADAS", 4)
BACKOFF_BASE = _env_int("BACKOFF_BASE", 15)
BACKOFF_MAX = _env_int("BACKOFF_MAX", 300)

# --- Execução ----------------------------------------------------------------
HTML_PRECLEAN = _env_bool("HTML_PRECLEAN", True)
PAUSA_ENTRE_GRUPOS = _env_int("PAUSA_ENTRE_GRUPOS", 15)
MAX_WORKERS = _env_int("MAX_WORKERS", 0) or max(1, min(len(GEMINI_API_KEYS), 3))
