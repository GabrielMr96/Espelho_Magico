"""Pipeline: agrupamento de e-mails, numeração de edições, extração, pós-processamento e publicação."""
import concurrent.futures
import datetime
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn

from . import config, futebol
from .drive_client import upload_para_drive
from .gmail_client import autenticar_google, buscar_emails, marcar_como_lido
from .html_cleaner import limpar_linhas, preparar_conteudo
from .llm import Extrator, FalhaExtracao, verificar_completude
from .log import console, escape, logger

SEPARADOR = "\n\n---\n\n"


@dataclass
class Opcoes:
    dry_run: bool = False
    unificar: str = "perguntar"  # "perguntar" | "sim" | "nao"
    grupos: list = field(default_factory=list)
    interativo: bool = True
    max_workers: int = 0


@dataclass
class ConfigGrupo:
    chave: str
    emails: list
    titulo_base: str
    nome_final: str
    unificar: bool = False


# --- Funções puras (testáveis) ------------------------------------------------

def carregar_prompt(nome):
    return (config.PROMPTS_DIR / f"{nome}.md").read_text(encoding="utf-8").strip()


def agrupar_emails(emails, mapeamento):
    """Agrupa por prefixo conhecido (no remetente ou no início do assunto); os demais ficam um por grupo."""
    grupos = {}
    for email in emails:
        assunto = email["assunto"].lower()
        remetente = email.get("remetente", "").lower()
        chave = next((p for p in mapeamento if p in remetente or assunto.startswith(p)), None)
        grupos.setdefault(chave or assunto, []).append(email)
    return grupos


def maior_edicao_existente(pasta, titulo_base):
    maior = 0
    for arq in Path(pasta).glob(f"{glob_escape(titulo_base)} #*.md"):
        m = re.search(r"#(\d+)\.md$", arq.name)
        if m:
            maior = max(maior, int(m.group(1)))
    return maior


def glob_escape(texto):
    return re.sub(r"([\[\]\*\?])", r"[\1]", texto)


def nome_fallback(assunto, agora=None):
    agora = agora or datetime.datetime.now()
    base = re.sub(r'[\\/*?:"<>|]', "", assunto)[:50].strip() or "newsletter"
    return base, f"{base}_{agora:%Y%m%d_%H%M%S}.md"


_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")


def _normalizar(linhas):
    return " ".join(" ".join(linhas).split()).lower()


def _separar_rabo(linhas):
    """Separa as linhas finais em branco ou '---' (separadores entre edições)."""
    fim = len(linhas)
    while fim > 0 and linhas[fim - 1].strip() in ("", "---"):
        fim -= 1
    return linhas[:fim], linhas[fim:]


def deduplicar_secoes(texto):
    """Remove seções com título repetido sem perder conteúdo.

    - Mesmo título e mesmo corpo: a seção repetida é descartada.
    - Mesmo título e corpo diferente: o corpo é anexado à primeira ocorrência
      (antes, só o título era apagado e o corpo ficava "colado" na seção anterior).
    Um título "RESULTADOS RECENTES" reinicia o escopo (os nomes de ligas se repetem por data).
    """
    blocos = [{"titulo": None, "linhas": []}]
    for linha in texto.split("\n"):
        m = _HEADING.match(linha)
        if m:
            blocos.append({"titulo": m.group(2).strip().lower(), "linhas": [linha]})
        else:
            blocos[-1]["linhas"].append(linha)

    saida = []
    vistos = {}
    for bloco in blocos:
        titulo = bloco["titulo"]
        if titulo is None:
            saida.append(bloco)
            continue
        if titulo.startswith("resultados recentes"):
            vistos.clear()
        primeiro = vistos.get(titulo)
        if primeiro is None:
            vistos[titulo] = bloco
            saida.append(bloco)
            continue

        miolo, rabo = _separar_rabo(bloco["linhas"][1:])
        corpo_novo = _normalizar(miolo)
        if corpo_novo and corpo_novo not in _normalizar(primeiro["linhas"][1:]):
            conteudo_primeiro, rabo_primeiro = _separar_rabo(primeiro["linhas"])
            primeiro["linhas"] = conteudo_primeiro + [""] + miolo + rabo_primeiro
        if rabo:
            saida.append({"titulo": None, "linhas": rabo})

    linhas = [linha for b in saida for linha in b["linhas"]]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


# --- Pipeline -------------------------------------------------------------------

class Pipeline:
    def __init__(self, opcoes, extrator, drive_service, gmail_service):
        self.opcoes = opcoes
        self.extrator = extrator
        self.drive = drive_service
        self.gmail = gmail_service
        self.prompt_base = carregar_prompt("espelho_magico")
        self.prompt_unificacao = carregar_prompt("unificacao")
        self.pasta_saida = config.DRY_RUN_DIR if opcoes.dry_run else config.RESULTADOS_DIR
        self._lock_pendentes = threading.Lock()
        self._nao_iniciados = 0

    # -- etapa interativa (sequencial) --
    def _perguntar(self, mensagem):
        return input(mensagem) if self.opcoes.interativo else ""

    def preparar_grupos(self, emails):
        grupos = agrupar_emails(emails, config.MAPEAMENTO_PREFIXOS)
        configs = []
        for chave, emails_grupo in grupos.items():
            if chave in config.MAPEAMENTO_PREFIXOS:
                titulo_base = config.MAPEAMENTO_PREFIXOS[chave]
            else:
                titulo_base, nome_final = nome_fallback(chave)

            if self.opcoes.grupos and not any(
                f.lower() in chave or f.lower() in titulo_base.lower() for f in self.opcoes.grupos
            ):
                continue

            if chave in config.MAPEAMENTO_PREFIXOS:
                logger.info(f"\n[bold magenta]Encontrados {len(emails_grupo)} e-mail(s) para: {escape(titulo_base)}[/bold magenta]")
                maior = maior_edicao_existente(config.RESULTADOS_DIR, titulo_base)
                if maior > 0:
                    numero = str(maior + 1)
                    logger.info(f"[bold green]▶ Número da edição automatizado: {numero} (última encontrada: {maior})[/bold green]")
                else:
                    numero = self._perguntar(f"▶ Qual o número da edição para '{titulo_base}'? ").strip()
                    if not numero:
                        numero = time.strftime("%Y%m%d")
                        logger.info(f"[yellow]▶ Sem número informado. Usando a data: {numero}[/yellow]")
                nome_final = f"{titulo_base} #{numero}.md"

            unificar = False
            if len(emails_grupo) >= 2:
                if self.opcoes.unificar == "sim":
                    unificar = True
                elif self.opcoes.unificar == "perguntar" and self.opcoes.interativo:
                    resposta = self._perguntar(
                        f"▶ {len(emails_grupo)} e-mails para '{titulo_base}'. Unificar de forma inteligente "
                        f"em um único texto (mesclando assuntos e evitando repetições)? (S/N): "
                    )
                    unificar = resposta.strip().lower() == "s"

            configs.append(ConfigGrupo(chave, emails_grupo, titulo_base, nome_final, unificar))
        return configs

    # -- extração --
    @staticmethod
    def _preparar(email):
        """Conteúdo a enviar para a IA: HTML pré-limpo em Markdown, ou texto puro só com regex."""
        bruto = email["conteudo_html"]
        if email.get("eh_html", True):
            conteudo = preparar_conteudo(bruto, config.HTML_PRECLEAN)
        else:
            conteudo = limpar_linhas(bruto)
        if conteudo is not bruto and len(bruto):
            reducao = 100 - (len(conteudo) * 100 // len(bruto))
            logger.info(f"[dim]  ✂ Pré-limpeza: {len(bruto):,} → {len(conteudo):,} caracteres (-{reducao}%)[/dim]")
        return conteudo

    def _extrair(self, conteudo, prompt, rotulo):
        try:
            texto = self.extrator.extrair(conteudo, prompt)
        except FalhaExtracao as e:
            logger.error(f"[bold red]✖ {escape(str(e))} ({escape(rotulo)})[/bold red]")
            return None
        if config.HTML_PRECLEAN and not verificar_completude(conteudo, texto):
            logger.warning(
                f"[yellow]⚠ Extração de '{escape(rotulo)}' parece incompleta "
                f"({len(texto):,} caracteres gerados para {len(conteudo):,} de entrada). Vale revisar.[/yellow]"
            )
        return texto.strip()

    def processar_grupo(self, cfg):
        with self._lock_pendentes:
            self._nao_iniciados -= 1
        try:
            return self._processar_grupo(cfg)
        finally:
            with self._lock_pendentes:
                ha_mais = self._nao_iniciados > 0
            if ha_mais and config.PAUSA_ENTRE_GRUPOS > 0:
                logger.info(f"[dim]⏳ Aguardando {config.PAUSA_ENTRE_GRUPOS}s para não sobrecarregar a API...[/dim]")
                time.sleep(config.PAUSA_ENTRE_GRUPOS)

    def _processar_grupo(self, cfg):
        logger.info(f"\n[bold blue]Processando para gerar: {escape(cfg.nome_final)}[/bold blue]")

        resultados_futebol, data_futebol = "", None
        if cfg.chave == config.PREFIXO_FUTEBOL:
            resultados_futebol, data_futebol = futebol.coletar_resultados()

        textos, emails_ok = [], []
        if cfg.unificar:
            combinado = "".join(
                f"\n\n--- INÍCIO DA EDIÇÃO {i + 1} ---\n\n{self._preparar(e)}"
                for i, e in enumerate(cfg.emails)
            )
            logger.info(f"[cyan]➜ Enviando {len(cfg.emails)} e-mails agrupados para extração unificada ({escape(cfg.titulo_base)})...[/cyan]")
            texto = self._extrair(combinado, f"{self.prompt_base}\n\n{self.prompt_unificacao}", cfg.titulo_base)
            if texto:
                textos.append(texto)
                emails_ok.extend(cfg.emails)
        else:
            for i, email in enumerate(cfg.emails):
                texto = self._extrair(self._preparar(email), self.prompt_base, email["assunto"])
                if texto:
                    textos.append(texto)
                    emails_ok.append(email)
                if i < len(cfg.emails) - 1:
                    time.sleep(2)

        if not textos:
            logger.error(f"[bold red]✖ Falha ao gerar texto para {escape(cfg.nome_final)}[/bold red]")
            return f"❌ Erro ao gerar {cfg.nome_final}"

        if resultados_futebol:
            textos[0] = resultados_futebol + SEPARADOR + textos[0]
        final = deduplicar_secoes(SEPARADOR.join(textos))

        self.pasta_saida.mkdir(parents=True, exist_ok=True)
        caminho = self.pasta_saida / cfg.nome_final
        caminho.write_text(final + "\n", encoding="utf-8")
        logger.info(f"[bold green]✔[/bold green] Salvo: [bold]{escape(str(caminho))}[/bold]")

        if self.opcoes.dry_run:
            logger.info("[dim]  (dry-run: sem upload, sem marcar e-mails e sem atualizar controle do futebol)[/dim]")
            return f"🧪 {cfg.nome_final} gerado em modo dry-run"

        if data_futebol:
            try:
                futebol.salvar_ultima_data(data_futebol)
            except Exception as e:
                logger.warning(f"[yellow]⚠ Erro ao salvar controle do futebol: {escape(str(e))}[/yellow]")

        if self.drive:
            upload_para_drive(self.drive, caminho, cfg.nome_final)
        for email in emails_ok:
            marcar_como_lido(self.gmail, email["id"])

        parcial = len(emails_ok) < len(cfg.emails)
        sufixo = f" ({len(emails_ok)}/{len(cfg.emails)} e-mails; os demais continuam não lidos)" if parcial else ""
        return f"{'⚠️' if parcial else '✅'} {cfg.nome_final} gerado com sucesso!{sufixo}"

    # -- execução paralela --
    def executar(self, configs):
        workers = max(1, min(self.opcoes.max_workers or config.MAX_WORKERS, len(configs)))
        self._nao_iniciados = len(configs)
        logger.info(f"\n[bold green]Iniciando processamento de {len(configs)} grupo(s) com {workers} worker(s)...[/bold green]\n")

        resultados = []
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(),
            TaskProgressColumn(), TimeElapsedColumn(), console=console,
        ) as progress:
            tarefa = progress.add_task("[cyan]Extraindo com IA...", total=len(configs))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="grupo") as executor:
                futuros = {executor.submit(self.processar_grupo, cfg): cfg for cfg in configs}
                for futuro in concurrent.futures.as_completed(futuros):
                    cfg = futuros[futuro]
                    try:
                        resultados.append(futuro.result())
                    except Exception as e:
                        logger.exception(f"Erro crítico no grupo {cfg.titulo_base}")
                        resultados.append(f"❌ Erro crítico no grupo {cfg.titulo_base}: {e}")
                    progress.advance(tarefa)
        return resultados


def executar(opcoes):
    inicio = time.time()
    titulo = "Iniciando Extração com Espelho Mágico ✨ (Modo E-mail)"
    if opcoes.dry_run:
        titulo += " [DRY-RUN]"
    console.print(Panel.fit(f"[bold cyan]{escape(titulo)}[/bold cyan]"))

    if not config.GEMINI_API_KEYS and config.IA_PADRAO != "perplexity":
        logger.error("[bold red]Erro:[/bold red] Nenhuma GEMINI_API_KEY encontrada no .env.")
        return 1

    try:
        drive_service, gmail_service = autenticar_google()
    except Exception as e:
        logger.error(f"[bold red]Falha na autenticação do Google: {escape(str(e))}[/bold red]")
        return 1

    emails = buscar_emails(gmail_service)
    if not emails:
        return 0

    pipeline = Pipeline(opcoes, Extrator(), drive_service, gmail_service)
    configs = pipeline.preparar_grupos(emails)
    if not configs:
        logger.info("[yellow]Nenhum grupo corresponde aos filtros informados.[/yellow]")
        return 0

    resultados = pipeline.executar(configs)

    total = time.time() - inicio
    console.print("\n[bold cyan]Resumo do Processamento:[/bold cyan]")
    for r in resultados:
        console.print(escape(r))
    logger.info(f"\n[bold cyan]⏱ Tempo total: {int(total // 60)}min {int(total % 60)}s[/bold cyan]")
    return 0 if all(not r.startswith("❌") for r in resultados) else 2


def interativo_disponivel():
    return sys.stdin.isatty()
