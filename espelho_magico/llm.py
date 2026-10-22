"""Extração via IA: Gemini (rotação de chaves + fallback de modelos) e Perplexity."""
import random
import threading
import time

import httpx
import requests
from google import genai
from google.genai import types
from google.genai.errors import APIError

from . import config
from .log import escape, logger


class FalhaExtracao(Exception):
    """Nenhum modelo conseguiu extrair o conteúdo."""


class ModeloIndisponivel(Exception):
    """Modelo inexistente/descontinuado (404). Não adianta tentar de novo."""


class ErroTransitorio(Exception):
    """Cota esgotada em todas as chaves, 503 ou falha de rede. Vale tentar depois."""

    def __init__(self, mensagem, motivo):
        super().__init__(mensagem)
        self.motivo = motivo  # "429", "503", "rede", "timeout", "vazio"


class RespostaTruncada(Exception):
    """O modelo parou por limite de tokens; o texto parcial fica guardado."""

    def __init__(self, texto):
        super().__init__("Resposta truncada (MAX_TOKENS)")
        self.texto = texto


class GerenciadorGemini:
    def __init__(self, api_keys, modelos, max_output_tokens=None, sleep=time.sleep, client_factory=None,
                 timeout=None, clock=time.monotonic):
        if not api_keys:
            raise ValueError("Nenhuma GEMINI_API_KEY configurada.")
        self.timeout = timeout
        if client_factory is None:
            # Timeout no cliente: sem ele, um 503 pode levar minutos para voltar (já vimos ~5 min).
            http_options = types.HttpOptions(timeout=int(timeout * 1000)) if timeout else None
            client_factory = lambda k: genai.Client(api_key=k, http_options=http_options)  # noqa: E731
        self.clientes = [client_factory(k) for k in api_keys]
        self.modelos = list(modelos)
        self.max_output_tokens = max_output_tokens
        self._sleep = sleep
        self._clock = clock
        self._indice = 0
        self._lock = threading.Lock()
        self._modelos_404 = set()

    # -- rotação de chaves (thread-safe) --
    def _proxima_chave(self):
        with self._lock:
            self._indice = (self._indice + 1) % len(self.clientes)
            return self._indice

    def _codigo(self, erro):
        codigo = getattr(erro, "code", None)
        if isinstance(codigo, int):
            return codigo
        texto = str(erro)
        for c in (429, 503, 404, 500):
            if str(c) in texto:
                return c
        if "RESOURCE_EXHAUSTED" in texto:
            return 429
        if "UNAVAILABLE" in texto:
            return 503
        if "NOT_FOUND" in texto:
            return 404
        return None

    @staticmethod
    def _detalhe(erro, limite=140):
        """Mensagem curta do servidor (ex.: 'The model is overloaded...') para o log."""
        texto = (getattr(erro, "message", None) or str(erro) or "").strip().replace("\n", " ")
        return texto if len(texto) <= limite else texto[: limite - 1] + "…"

    def gerar(self, conteudo, prompt, modelo):
        """Uma tentativa com o modelo dado, passando por todas as chaves em caso de 429."""
        if modelo in self._modelos_404:
            raise ModeloIndisponivel(modelo)

        cfg = {"system_instruction": prompt, "temperature": 0.2}
        if self.max_output_tokens:
            cfg["max_output_tokens"] = self.max_output_tokens

        indice = self._proxima_chave()
        for tentativa in range(len(self.clientes)):
            cliente = self.clientes[indice]
            logger.info(f"[cyan]➜ Processando com {modelo} (Chave {indice + 1}/{len(self.clientes)})...[/cyan]")
            try:
                resposta = cliente.models.generate_content(
                    model=modelo, contents=[conteudo], config=types.GenerateContentConfig(**cfg)
                )
            except APIError as e:
                codigo = self._codigo(e)
                if codigo == 404:
                    self._modelos_404.add(modelo)
                    raise ModeloIndisponivel(f"Modelo {modelo} não encontrado ou descontinuado (404).")
                if codigo == 429:
                    if tentativa < len(self.clientes) - 1:
                        logger.warning(f"[yellow]  ⚠ Cota esgotada na Chave {indice + 1} ({modelo}). Alternando chave...[/yellow]")
                        indice = self._proxima_chave()
                        continue
                    raise ErroTransitorio(f"Cota esgotada em todas as chaves para {modelo}.", "429")
                if codigo in (500, 502, 503, 504):
                    raise ErroTransitorio(
                        f"Serviço indisponível ({codigo}) no {modelo}: {self._detalhe(e)}", "503"
                    )
                raise
            except (httpx.TimeoutException, TimeoutError):
                raise ErroTransitorio(f"Sem resposta do {modelo} em {self.timeout or '?'}s (timeout).", "timeout")
            except (httpx.HTTPError, requests.RequestException, ConnectionError) as e:
                raise ErroTransitorio(f"Falha de rede com {modelo}: {e}", "rede")

            texto = resposta.text
            if not texto or not texto.strip():
                raise ErroTransitorio(f"Resposta vazia/bloqueada do {modelo}.", "vazio")
            finish = None
            if resposta.candidates:
                finish = resposta.candidates[0].finish_reason
            if finish == types.FinishReason.MAX_TOKENS:
                raise RespostaTruncada(texto)
            return texto
        raise ErroTransitorio(f"Cota esgotada em todas as chaves para {modelo}.", "429")

    def extrair(self, conteudo, prompt, max_rodadas=None, backoff_base=None, backoff_max=None, tempo_max=None):
        """Percorre os modelos em ordem; se todos falharem por erro transitório, espera e repete.

        Para no que vier primeiro: max_rodadas ou ~tempo_max segundos (minutos, não horas).
        Sobrecarga (503) do Gemini costuma durar alguns minutos, então vale insistir com
        espera crescente em vez de desistir cedo ou trocar por modelos inferiores.
        """
        max_rodadas = max_rodadas or config.MAX_RODADAS
        backoff_base = backoff_base if backoff_base is not None else config.BACKOFF_BASE
        backoff_max = backoff_max if backoff_max is not None else config.BACKOFF_MAX
        tempo_max = tempo_max if tempo_max is not None else config.IA_TEMPO_MAX

        inicio = self._clock()
        melhor_truncado = None
        motivos = set()
        rodadas_feitas = 0
        for rodada in range(max_rodadas):
            rodadas_feitas = rodada + 1
            for modelo in self.modelos:
                if modelo in self._modelos_404:
                    continue
                try:
                    return self.gerar(conteudo, prompt, modelo)
                except RespostaTruncada as e:
                    logger.warning(f"[yellow]  ⚠ {modelo} atingiu o limite de tokens (resposta incompleta). Tentando outro modelo...[/yellow]")
                    if melhor_truncado is None or len(e.texto) > len(melhor_truncado):
                        melhor_truncado = e.texto
                    motivos.add("truncado")
                except ModeloIndisponivel as e:
                    logger.warning(f"[yellow]  ⚠ {escape(str(e))} Ignorando este modelo nesta execução.[/yellow]")
                except ErroTransitorio as e:
                    motivos.add(e.motivo)
                    logger.warning(f"[yellow]  ⚠ {escape(str(e))} Tentando próximo modelo...[/yellow]")
                except Exception as e:
                    motivos.add("outro")
                    logger.warning(f"[yellow]  ⚠ Erro inesperado com {modelo}: {escape(str(e))}. Tentando próximo modelo...[/yellow]")

            if melhor_truncado is not None:
                logger.warning("[yellow]⚠ Usando a resposta mais longa disponível, que está incompleta.[/yellow]")
                return melhor_truncado
            if all(m in self._modelos_404 for m in self.modelos):
                break
            if rodada < max_rodadas - 1:
                espera = min(backoff_base * (2 ** rodada), backoff_max) + random.uniform(0, 5)
                if tempo_max:
                    restante = tempo_max - (self._clock() - inicio)
                    if restante < 10:
                        break  # orçamento de tempo esgotado
                    espera = min(espera, restante)
                decorrido = self._clock() - inicio
                logger.warning(
                    f"[bold yellow]⏳ Todos os modelos falharam (motivos: {', '.join(sorted(motivos))}). "
                    f"Aguardando {espera:.0f}s antes da rodada {rodada + 2}/{max_rodadas} "
                    f"({decorrido / 60:.1f} de {tempo_max / 60:.0f} min)...[/bold yellow]"
                )
                self._sleep(espera)

        raise FalhaExtracao(
            f"Falha em todos os modelos após {rodadas_feitas} rodada(s) em {(self._clock() - inicio) / 60:.1f} min. "
            f"Motivos: {', '.join(sorted(motivos)) or 'n/d'}"
        )


def extrair_com_perplexity(conteudo, prompt):
    if not config.PERPLEXITY_API_KEY:
        raise RuntimeError("PERPLEXITY_API_KEY ausente.")
    logger.info("[cyan]➜ Extraindo texto via Perplexity...[/cyan]")
    resposta = None
    try:
        resposta = requests.post(
            "https://api.perplexity.ai/chat/completions",
            json={
                "model": config.PERPLEXITY_MODEL,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Filtre e extraia os dados deste conteúdo:\n\n{conteudo}"},
                ],
                "temperature": 0.2,
            },
            headers={"Authorization": f"Bearer {config.PERPLEXITY_API_KEY}", "Content-Type": "application/json"},
            timeout=120,
        )
        resposta.raise_for_status()
        return resposta.json()["choices"][0]["message"]["content"]
    except Exception as e:
        detalhe = resposta.text if resposta is not None else ""
        raise RuntimeError(f"Erro na API do Perplexity: {e} - {detalhe}")


def verificar_completude(entrada, saida, limite=0.15):
    """Heurística: saída muito menor que a entrada (já limpa) sugere extração incompleta."""
    if not entrada or not saida:
        return True
    return len(saida) >= len(entrada) * limite


class Extrator:
    """Fachada usada pelo pipeline: Perplexity opcional + Gemini com fallback."""

    def __init__(self, gemini=None):
        self.gemini = gemini or GerenciadorGemini(
            config.GEMINI_API_KEYS, config.GEMINI_MODELS, config.GEMINI_MAX_OUTPUT_TOKENS,
            timeout=config.GEMINI_TIMEOUT,
        )

    def extrair(self, conteudo, prompt):
        if config.IA_PADRAO == "perplexity":
            try:
                return extrair_com_perplexity(conteudo, prompt)
            except Exception as e:
                logger.warning(f"[yellow]⚠ Erro no Perplexity: {escape(str(e))}. Usando Gemini como fallback...[/yellow]")
        try:
            return self.gemini.extrair(conteudo, prompt)
        except FalhaExtracao as falha:
            if not (config.FALLBACK_PERPLEXITY and config.PERPLEXITY_API_KEY and config.IA_PADRAO != "perplexity"):
                raise
            logger.warning(f"[yellow]⚠ {escape(str(falha))} Tentando Perplexity como último recurso...[/yellow]")
            try:
                return extrair_com_perplexity(conteudo, prompt)
            except Exception as e:
                logger.warning(f"[yellow]⚠ Perplexity também falhou: {escape(str(e))}[/yellow]")
                raise falha
