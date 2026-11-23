"""Extração via IA: Gemini (rotação de chaves + fallback de modelos), provedor alternativo e Perplexity."""
import json
import random
import re
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


MIN_CHARS_PARA_DIVIDIR = 4000
_RE_BLOCO_METADADOS = re.compile(r"^\s*(<metadados>.*?</metadados>)\s*", re.S)
_RE_BLOCO_DOC = re.compile(r"^\s*<documento_entrada>\s*(.*?)\s*</documento_entrada>\s*$", re.S)
_RE_LINHA_META_LEGADO = re.compile(r"^\s*(\[METADADOS DO E-MAIL[^\n]*\])\s*\n+", re.S)
_RE_CORTE_ARTIGO_FEED = re.compile(
    r"\n(?=## [^\n]+\n+\s*\d{1,2}/\d{1,2}/\d{4}\s*(?:[àa]s|[-–,])?\s*\d{1,2}:\d{2})",
    re.I,
)
_RE_CORTE_SECAO_CAT = re.compile(r"\n(?=#{5,6}\s+\S)")
_RE_CORTE_SECAO_H1 = re.compile(r"\n(?=#\s+\S)")


def modelo_eh_inferior(modelo):
    """Retorna True para modelos de fallback leves/inferiores (lite ou gemma)."""
    low = (modelo or "").strip().lower()
    return "lite" in low or low.startswith("gemma")


def _desempacotar_conteudo(conteudo):
    """Separa (prefixo_metadados, corpo_interno, tinha_tag_doc)."""
    texto = conteudo.strip()
    meta = ""
    m_meta = _RE_BLOCO_METADADOS.match(texto)
    if m_meta:
        meta = m_meta.group(1).strip()
        texto = texto[m_meta.end():].strip()
    else:
        m_leg = _RE_LINHA_META_LEGADO.match(texto)
        if m_leg:
            meta = m_leg.group(1).strip()
            texto = texto[m_leg.end():].strip()

    m_doc = _RE_BLOCO_DOC.match(texto)
    if m_doc:
        return meta, m_doc.group(1).strip(), True
    return meta, texto, False


def _empacotar_conteudo(meta, corpo, com_tag_doc):
    corpo_fmt = f"<documento_entrada>\n{corpo.strip()}\n</documento_entrada>" if com_tag_doc else corpo.strip()
    if meta:
        return f"{meta}\n\n{corpo_fmt}"
    return corpo_fmt


def dividir_em_duas_partes(conteudo):
    """Divide uma entrada longa em 2 partes equilibradas respeitando fronteiras de edição, seção (##) ou parágrafo."""
    meta, corpo, com_tag_doc = _desempacotar_conteudo(conteudo)
    if len(corpo) < 400:
        return [conteudo]

    # 1) Se houver múltiplas edições unificadas, prefere separar na fronteira entre edições
    partes_ed = [p.strip() for p in re.split(r"(?=\n*--- INÍCIO DA EDIÇÃO \d+ ---)", corpo) if p.strip()]
    if len(partes_ed) >= 2:
        meio = len(corpo) / 2
        melhor_k = min(
            range(1, len(partes_ed)),
            key=lambda k: abs(sum(len(x) for x in partes_ed[:k]) - meio),
        )
        c1 = "\n\n".join(partes_ed[:melhor_k])
        c2 = "\n\n".join(partes_ed[melhor_k:])
        return [_empacotar_conteudo(meta, c1, com_tag_doc), _empacotar_conteudo(meta, c2, com_tag_doc)]

    # 2) Prioriza fronteira de artigos datados de feeds (## Título + DD/MM/AAAA às HH:MM) para nunca cortar no meio de uma matéria
    meio = len(corpo) // 2
    corte = None
    ini_feed, fim_feed = int(len(corpo) * 0.15), int(len(corpo) * 0.85)
    pos_feed = [m.start() for m in _RE_CORTE_ARTIGO_FEED.finditer(corpo) if ini_feed <= m.start() <= fim_feed]
    if pos_feed:
        corte = min(pos_feed, key=lambda p: abs(p - meio))

    # 3) Em newsletters estruturadas por categoria (##### / ######) ou H1 (#), corta antes da próxima grande matéria
    ini_janela = int(len(corpo) * 0.2)
    fim_janela = int(len(corpo) * 0.8)
    if corte is None:
        for padrao in (_RE_CORTE_SECAO_CAT, _RE_CORTE_SECAO_H1):
            posicoes = [m.start() for m in padrao.finditer(corpo) if ini_janela <= m.start() <= fim_janela]
            if posicoes:
                corte = min(posicoes, key=lambda p: abs(p - meio))
                break

    # 4) Caso contrário, divide no marcador estrutural mais próximo do meio (## -> ### -> \n\n -> \n)
    if corte is None:
        for sep in ("\n## ", "\n### ", "\n\n", "\n"):
            posicoes = [m.start() for m in re.finditer(re.escape(sep), corpo) if ini_janela <= m.start() <= fim_janela]
            if posicoes:
                corte = min(posicoes, key=lambda p: abs(p - meio))
                break
    if corte is None:
        corte = meio

    c1, c2 = corpo[:corte].strip(), corpo[corte:].strip()
    if not c1 or not c2:
        return [conteudo]

    # Se o metadado estava embutido dentro do bloco da edição (modo unificado), preserva-o na segunda metade
    if not meta and "<metadados>" not in c2:
        m_meta_int = re.search(r"(<metadados>.*?</metadados>)", c1, flags=re.S)
        if m_meta_int:
            c2 = f"{m_meta_int.group(1)}\n\n{c2}"

    return [_empacotar_conteudo(meta, c1, com_tag_doc), _empacotar_conteudo(meta, c2, com_tag_doc)]


def dividir_conteudo_com_metadados(conteudo, max_chars=14000):
    """Divide recursivamente o conteúdo até que cada parte tenha no máximo `max_chars` caracteres."""
    if not max_chars or len(conteudo) <= max_chars:
        return [conteudo]
    metades = dividir_em_duas_partes(conteudo)
    if len(metades) == 1:
        return [conteudo]
    saida = []
    for metade in metades:
        saida.extend(dividir_conteudo_com_metadados(metade, max_chars=max_chars))
    return saida


_MESES_H1 = {
    "janeiro": 1, "jan": 1, "january": 1,
    "fevereiro": 2, "fev": 2, "feb": 2, "february": 2,
    "março": 3, "marco": 3, "mar": 3, "march": 3,
    "abril": 4, "abr": 4, "apr": 4, "april": 4,
    "maio": 5, "mai": 5, "may": 5,
    "junho": 6, "jun": 6, "june": 6,
    "julho": 7, "jul": 7, "july": 7,
    "agosto": 8, "ago": 8, "aug": 8, "august": 8,
    "setembro": 9, "set": 9, "sep": 9, "sept": 9, "september": 9,
    "outubro": 10, "out": 10, "oct": 10, "october": 10,
    "novembro": 11, "nov": 11, "november": 11,
    "dezembro": 12, "dez": 12, "dec": 12, "december": 12,
}


def _chave_data_h1(cabecalho):
    m = re.search(
        r"\b(\d{1,2})\s+de\s+(janeiro|fevereiro|março|marco|abril|maio|junho|"
        r"julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(20\d{2})\b",
        cabecalho,
        re.I,
    )
    if m:
        return (int(m.group(3)), _MESES_H1[m.group(2).lower()], int(m.group(1)))
    m = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
        cabecalho,
        re.I,
    )
    if m:
        return (int(m.group(3)), _MESES_H1[m.group(1).lower().rstrip(".")], int(m.group(2)))
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b", cabecalho)
    if m:
        return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return (0, 0, 0)


def unir_partes_extraidas(partes):
    """Junta o Markdown extraído mantendo a ordem das partes e usando o '# Data' mais recente no topo."""
    validas = []
    for p in partes:
        if not p or not p.strip():
            continue
        limpa = p.strip()
        m_cerca = re.match(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n\s*```\s*$", limpa, flags=re.S | re.I)
        if m_cerca:
            limpa = m_cerca.group(1).strip()
        if "[SEM_CONTEUDO_EDITORIAL]" in limpa:
            continue
        validas.append(limpa)

    if not validas:
        return "[SEM_CONTEUDO_EDITORIAL]"

    cabecalhos = []
    corpos = []
    for idx, parte in enumerate(validas):
        m_h1 = re.match(r"^\s*(#\s+[^\n]+)\n*(.*)$", parte, flags=re.S)
        if m_h1:
            cab = m_h1.group(1).strip()
            cabecalhos.append((idx, cab, _chave_data_h1(cab)))
            corpo = m_h1.group(2).strip()
            if corpo:
                corpos.append(corpo)
        else:
            corpos.append(parte)

    if cabecalhos:
        _, h1_escolhido, _ = max(cabecalhos, key=lambda item: (item[2], -item[0]))
        return "\n\n".join([h1_escolhido] + corpos) if corpos else h1_escolhido
    return "\n\n".join(corpos)


class GerenciadorGemini:
    def __init__(self, api_keys, modelos, max_output_tokens=None, sleep=time.sleep, client_factory=None,
                 timeout=None, clock=time.monotonic, pausa_antes_downgrade=None):
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
        self.pausa_antes_downgrade = pausa_antes_downgrade
        self._indice = 0
        self._lock = threading.Lock()
        self._modelos_404 = set()
        self._tls = threading.local()

    def resetar_modelos_usados(self):
        self._tls.modelos_usados = []

    def obter_modelos_usados(self):
        return list(getattr(self._tls, "modelos_usados", []))

    def _registrar_modelo_usado(self, modelo):
        usados = getattr(self._tls, "modelos_usados", None)
        if usados is None:
            usados = []
            self._tls.modelos_usados = usados
        usados.append(modelo)

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
        for c in (429, 503, 504, 404, 500):
            if str(c) in texto:
                return c
        if "RESOURCE_EXHAUSTED" in texto:
            return 429
        if "DEADLINE_EXCEEDED" in texto:
            return 504
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
                if codigo == 504:
                    raise ErroTransitorio(
                        f"Serviço indisponível (504) no {modelo}: {self._detalhe(e)}", "504"
                    )
                if codigo in (500, 502, 503):
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
            self._registrar_modelo_usado(modelo)
            return texto
        raise ErroTransitorio(f"Cota esgotada em todas as chaves para {modelo}.", "429")

    def extrair(self, conteudo, prompt, max_rodadas=None, backoff_base=None, backoff_max=None, tempo_max=None,
                apenas_tier_principal=False, pausa_antes_downgrade=None):
        """Percorre os modelos em ordem; se todos falharem por erro transitório, espera e repete.

        Antes de rebaixar dos modelos principais para modelos leves (lite/gemma) por pico de 503/timeout,
        faz uma pausa curta e tenta novamente o tier principal para evitar perda de qualidade.
        """
        max_rodadas = max_rodadas or config.MAX_RODADAS
        backoff_base = backoff_base if backoff_base is not None else config.BACKOFF_BASE
        backoff_max = backoff_max if backoff_max is not None else config.BACKOFF_MAX
        tempo_max = tempo_max if tempo_max is not None else config.IA_TEMPO_MAX
        if pausa_antes_downgrade is None:
            pausa_antes_downgrade = (
                self.pausa_antes_downgrade
                if self.pausa_antes_downgrade is not None
                else config.PAUSA_ANTES_DOWNGRADE
            )

        modelos_ativos = (
            [m for m in self.modelos if not modelo_eh_inferior(m)] or list(self.modelos)
            if apenas_tier_principal
            else list(self.modelos)
        )
        tier_principal = [m for m in modelos_ativos if not modelo_eh_inferior(m)]
        tier_fallback = [m for m in modelos_ativos if modelo_eh_inferior(m)]

        inicio = self._clock()
        melhor_truncado = None
        motivos = set()
        rodadas_feitas = 0

        def _tentar_lista(lista_modelos):
            nonlocal melhor_truncado
            motivos_locais = set()
            for modelo in lista_modelos:
                if modelo in self._modelos_404:
                    continue
                try:
                    return True, self.gerar(conteudo, prompt, modelo), motivos_locais
                except RespostaTruncada as e:
                    if len(conteudo) >= MIN_CHARS_PARA_DIVIDIR:
                        metades = dividir_em_duas_partes(conteudo)
                        if len(metades) == 2:
                            logger.warning(
                                f"[yellow]  ⚠ {modelo} atingiu MAX_TOKENS em entrada longa ({len(conteudo):,} chars). "
                                f"Dividindo em 2 partes para manter o modelo principal...[/yellow]"
                            )
                            r1 = self.extrair(
                                metades[0], prompt, max_rodadas, backoff_base, backoff_max, tempo_max,
                                apenas_tier_principal=apenas_tier_principal,
                                pausa_antes_downgrade=pausa_antes_downgrade,
                            )
                            r2 = self.extrair(
                                metades[1], prompt, max_rodadas, backoff_base, backoff_max, tempo_max,
                                apenas_tier_principal=apenas_tier_principal,
                                pausa_antes_downgrade=pausa_antes_downgrade,
                            )
                            return True, unir_partes_extraidas([r1, r2]), motivos_locais
                    logger.warning(f"[yellow]  ⚠ {modelo} atingiu o limite de tokens (resposta incompleta). Tentando outro modelo...[/yellow]")
                    if melhor_truncado is None or len(e.texto) > len(melhor_truncado):
                        melhor_truncado = e.texto
                    motivos_locais.add("truncado")
                except ModeloIndisponivel as e:
                    logger.warning(f"[yellow]  ⚠ {escape(str(e))} Ignorando este modelo nesta execução.[/yellow]")
                except ErroTransitorio as e:
                    if e.motivo in ("504", "timeout") and len(conteudo) >= MIN_CHARS_PARA_DIVIDIR:
                        metades = dividir_em_duas_partes(conteudo)
                        if len(metades) == 2:
                            logger.warning(
                                f"[yellow]  ⚠ {escape(str(e))} Dividindo entrada ({len(conteudo):,} chars) "
                                f"em 2 partes menores para processar sem downgradar o modelo...[/yellow]"
                            )
                            r1 = self.extrair(
                                metades[0], prompt, max_rodadas, backoff_base, backoff_max, tempo_max,
                                apenas_tier_principal=apenas_tier_principal,
                                pausa_antes_downgrade=pausa_antes_downgrade,
                            )
                            r2 = self.extrair(
                                metades[1], prompt, max_rodadas, backoff_base, backoff_max, tempo_max,
                                apenas_tier_principal=apenas_tier_principal,
                                pausa_antes_downgrade=pausa_antes_downgrade,
                            )
                            return True, unir_partes_extraidas([r1, r2]), motivos_locais
                    motivos_locais.add(e.motivo)
                    logger.warning(f"[yellow]  ⚠ {escape(str(e))} Tentando próximo modelo...[/yellow]")
                except Exception as e:
                    motivos_locais.add("outro")
                    logger.warning(f"[yellow]  ⚠ Erro inesperado com {modelo}: {escape(str(e))}. Tentando próximo modelo...[/yellow]")
            return False, None, motivos_locais

        for rodada in range(max_rodadas):
            rodadas_feitas = rodada + 1
            if tier_principal and tier_fallback:
                ok, res, mot_t1 = _tentar_lista(tier_principal)
                if ok:
                    return res
                motivos.update(mot_t1)

                # Se os modelos principais falharam por instabilidade transitória (503/timeout/rede/vazio),
                # espera brevemente e tenta o tier principal mais uma vez antes de cair para lite/gemma.
                tem_t1_disponivel = any(m not in self._modelos_404 for m in tier_principal)
                instabilidade_t1 = bool(mot_t1 & {"503", "504", "timeout", "rede", "vazio"})
                if tem_t1_disponivel and instabilidade_t1 and pausa_antes_downgrade and pausa_antes_downgrade > 0:
                    espera_t1 = float(pausa_antes_downgrade)
                    pode_esperar = True
                    if tempo_max:
                        restante = tempo_max - (self._clock() - inicio)
                        if restante <= espera_t1:
                            pode_esperar = False
                    if pode_esperar:
                        logger.warning(
                            f"[yellow]  ⏳ Modelos principais instáveis ({', '.join(sorted(mot_t1))}). "
                            f"Aguardando {espera_t1:.0f}s para nova tentativa antes de baixar para modelos lite...[/yellow]"
                        )
                        self._sleep(espera_t1)
                        ok, res, mot_t1_retry = _tentar_lista(tier_principal)
                        if ok:
                            return res
                        motivos.update(mot_t1_retry)

                ok, res, mot_fb = _tentar_lista(tier_fallback)
                if ok:
                    return res
                motivos.update(mot_fb)
            else:
                ok, res, mot_all = _tentar_lista(modelos_ativos)
                if ok:
                    return res
                motivos.update(mot_all)

            if melhor_truncado is not None:
                logger.warning("[yellow]⚠ Usando a resposta mais longa disponível, que está incompleta.[/yellow]")
                return melhor_truncado
            if all(m in self._modelos_404 for m in modelos_ativos):
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
                "disable_search": True,
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


class ProvedorAlternativo:
    """Provedor fora do Google: Claude (API nativa da Anthropic) ou qualquer API compatível com OpenAI."""

    URLS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}

    def __init__(self, provedor, api_key, modelo, base_url="", max_tokens=16000, timeout=600,
                 tentativas=4, sleep=time.sleep, post=None):
        if provedor not in self.URLS:
            raise ValueError(f"ALT_PROVEDOR inválido: {provedor!r} (use 'anthropic' ou 'openai').")
        if not api_key or not modelo:
            raise ValueError("ALT_API_KEY e ALT_MODELO são obrigatórios para o provedor alternativo.")
        self.provedor = provedor
        self.api_key = api_key
        self.modelo = modelo
        self.base_url = (base_url or self.URLS[provedor]).rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.tentativas = tentativas
        self._sleep = sleep
        self._post = post or requests.post
        self._sem_temperatura = False  # alguns modelos de raciocínio só aceitam o padrão

    @classmethod
    def do_config(cls):
        if not (config.ALT_PROVEDOR and config.ALT_API_KEY and config.ALT_MODELO):
            return None
        return cls(config.ALT_PROVEDOR, config.ALT_API_KEY, config.ALT_MODELO, config.ALT_BASE_URL,
                   config.ALT_MAX_TOKENS, config.ALT_TIMEOUT)

    @property
    def nome(self):
        return f"{self.provedor}/{self.modelo}"

    def _requisicao(self, conteudo, prompt):
        if self.provedor == "anthropic":
            corpo = {
                "model": self.modelo, "max_tokens": self.max_tokens, "system": prompt,
                "messages": [{"role": "user", "content": conteudo}],
            }
            cabecalhos = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
            url = f"{self.base_url}/v1/messages"
        else:
            # A API oficial da OpenAI usa max_completion_tokens; as compatíveis ainda usam max_tokens
            campo = "max_completion_tokens" if "api.openai.com" in self.base_url else "max_tokens"
            corpo = {
                "model": self.modelo, campo: self.max_tokens,
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": conteudo}],
            }
            cabecalhos = {"Authorization": f"Bearer {self.api_key}"}
            url = f"{self.base_url}/chat/completions"
        if not self._sem_temperatura:
            corpo["temperature"] = 0.2
        cabecalhos["Content-Type"] = "application/json"
        return url, corpo, cabecalhos

    def _ler(self, dados):
        if self.provedor == "anthropic":
            texto = "".join(b.get("text", "") for b in dados.get("content", []) if b.get("type") == "text")
            truncado = dados.get("stop_reason") == "max_tokens"
        else:
            escolha = dados["choices"][0]
            texto = escolha["message"].get("content") or ""
            truncado = escolha.get("finish_reason") == "length"
        return texto, truncado

    @staticmethod
    def _resumo_erro_http(status_code, detalhe):
        """Extrai resumo legível de erro HTTP (sem despejar JSON cru no terminal)."""
        msg = (detalhe or "").strip()
        try:
            dados = json.loads(msg)
            err = dados.get("error") if isinstance(dados, dict) else None
            if isinstance(err, dict) and err.get("message"):
                msg = str(err["message"])
            elif isinstance(err, str):
                msg = err
        except Exception:
            pass
        low = msg.lower()
        if status_code == 429:
            if "tokens per minute" in low or "tpm" in low or "rate limit" in low:
                return "429 limite de tokens/minuto (TPM)"
            return "429 limite de requisições"
        if status_code == 413 or any(t in low for t in ("request too large", "reduce your message size", "context_length")):
            return f"{status_code} payload acima do limite de TPM"
        if status_code in (500, 502, 503, 504, 529):
            return f"{status_code} serviço temporariamente indisponível"
        msg = " ".join(msg.split())
        return f"{status_code}: {msg[:110]}" if msg else f"HTTP {status_code}"

    def gerar(self, conteudo, prompt):
        ultimo_erro = "n/d"
        eh_tpm = False
        for tentativa in range(self.tentativas):
            url, corpo, cabecalhos = self._requisicao(conteudo, prompt)
            logger.info(f"[cyan]➜ Processando com {self.nome} (tentativa {tentativa + 1}/{self.tentativas})...[/cyan]")
            try:
                resposta = self._post(url, json=corpo, headers=cabecalhos, timeout=self.timeout)
            except (requests.RequestException, ConnectionError, TimeoutError) as e:
                ultimo_erro = f"rede: {e}"
            else:
                if resposta.status_code == 200:
                    texto, truncado = self._ler(resposta.json())
                    if not texto.strip():
                        raise FalhaExtracao(f"Resposta vazia do {self.nome}.")
                    if truncado:
                        logger.warning(f"[yellow]⚠ {self.nome} atingiu ALT_MAX_TOKENS; a resposta pode estar incompleta.[/yellow]")
                    return texto
                detalhe = resposta.text[:300]
                detalhe_low = detalhe.lower()
                if resposta.status_code == 400 and "temperature" in detalhe_low and not self._sem_temperatura:
                    self._sem_temperatura = True
                    continue
                eh_erro_tamanho = resposta.status_code == 413 or (
                    resposta.status_code in (400, 429)
                    and any(t in detalhe_low for t in ("request too large", "reduce your message size", "context_length"))
                )
                if eh_erro_tamanho and len(conteudo) >= 1500:
                    metades = dividir_em_duas_partes(conteudo)
                    if len(metades) == 2:
                        logger.warning(
                            f"[yellow]  ⚠ {self.nome}: {resposta.status_code} acima do limite de TPM — "
                            f"dividindo entrada ({len(conteudo):,} chars) em 2 partes menores...[/yellow]"
                        )
                        r1 = self.gerar(metades[0], prompt)
                        self._sleep(15)
                        r2 = self.gerar(metades[1], prompt)
                        return unir_partes_extraidas([r1, r2])
                resumo_erro = self._resumo_erro_http(resposta.status_code, detalhe)
                if resposta.status_code not in (408, 409, 429, 500, 502, 503, 504, 529):
                    raise FalhaExtracao(f"{self.nome} recusou a requisição ({resumo_erro})")
                ultimo_erro = resumo_erro
                eh_tpm = "tokens per minute" in detalhe_low or "tpm" in detalhe_low or "tpm" in resumo_erro.lower()
            if tentativa < self.tentativas - 1:
                espera = min(10 * (2 ** tentativa), 90) + random.uniform(0, 3)
                if eh_tpm:
                    espera = max(espera, 35)
                logger.warning(
                    f"[yellow]  ⚠ {self.nome}: {escape(ultimo_erro)} — nova tentativa em {espera:.0f}s...[/yellow]"
                )
                self._sleep(espera)
        raise FalhaExtracao(f"{self.nome} falhou após {self.tentativas} tentativa(s): {ultimo_erro}")


class Extrator:
    """Fachada usada pelo pipeline: Gemini com fallback para provedor alternativo e/ou Perplexity."""

    def __init__(self, gemini=None, alternativo=None, ia_padrao=None):
        self.ia_padrao = (ia_padrao or config.IA_PADRAO).lower()
        self.alternativo = alternativo if alternativo is not None else ProvedorAlternativo.do_config()
        if self.ia_padrao == "alt" and not self.alternativo:
            raise ValueError("IA 'alt' escolhida, mas ALT_PROVEDOR/ALT_API_KEY/ALT_MODELO não estão no .env.")
        self.gemini = gemini or GerenciadorGemini(
            config.GEMINI_API_KEYS, config.GEMINI_MODELS, config.GEMINI_MAX_OUTPUT_TOKENS,
            timeout=config.GEMINI_TIMEOUT,
        )
        self._gemini_esgotado = False

    def resetar_modelos_usados(self):
        if hasattr(self.gemini, "resetar_modelos_usados"):
            self.gemini.resetar_modelos_usados()

    def obter_modelos_usados(self):
        if hasattr(self.gemini, "obter_modelos_usados"):
            return self.gemini.obter_modelos_usados()
        return []

    def extrair(self, conteudo, prompt, apenas_tier_principal=False):
        if self.ia_padrao == "alt" or (self._gemini_esgotado and self.alternativo):
            try:
                return self.alternativo.gerar(conteudo, prompt)
            except FalhaExtracao as e:
                if apenas_tier_principal:
                    raise
                logger.warning(f"[yellow]⚠ {escape(str(e))} Usando Gemini como fallback...[/yellow]")
        if self.ia_padrao == "perplexity":
            try:
                return extrair_com_perplexity(conteudo, prompt)
            except Exception as e:
                if apenas_tier_principal:
                    raise FalhaExtracao(str(e))
                logger.warning(f"[yellow]⚠ Erro no Perplexity: {escape(str(e))}. Usando Gemini como fallback...[/yellow]")
        try:
            if apenas_tier_principal:
                return self.gemini.extrair(
                    conteudo, prompt, max_rodadas=2, tempo_max=120, apenas_tier_principal=True,
                )
            return self.gemini.extrair(conteudo, prompt)
        except FalhaExtracao as falha:
            if apenas_tier_principal:
                raise
            # Gemini fora do ar (503 em todos os modelos): sai do Google em vez de desistir
            if self.alternativo and config.FALLBACK_ALT and self.ia_padrao != "alt":
                logger.warning(f"[yellow]⚠ {escape(str(falha))} Tentando {self.alternativo.nome}...[/yellow]")
                try:
                    resposta = self.alternativo.gerar(conteudo, prompt)
                    self._gemini_esgotado = True
                    return resposta
                except FalhaExtracao as e:
                    logger.warning(f"[yellow]⚠ {escape(str(e))}[/yellow]")
            if not (config.FALLBACK_PERPLEXITY and config.PERPLEXITY_API_KEY and self.ia_padrao != "perplexity"):
                raise falha
            logger.warning(f"[yellow]⚠ {escape(str(falha))} Tentando Perplexity como último recurso...[/yellow]")
            try:
                return extrair_com_perplexity(conteudo, prompt)
            except Exception as e:
                logger.warning(f"[yellow]⚠ Perplexity também falhou: {escape(str(e))}[/yellow]")
                raise falha
