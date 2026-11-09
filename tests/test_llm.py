from types import SimpleNamespace

import pytest
from google.genai import types
from google.genai.errors import APIError

from espelho_magico.llm import FalhaExtracao, GerenciadorGemini


def _api_error(code):
    return APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


def _resposta(texto, finish=types.FinishReason.STOP):
    return SimpleNamespace(text=texto, candidates=[SimpleNamespace(finish_reason=finish)])


class ClienteFake:
    def __init__(self, comportamento):
        self.comportamento = comportamento  # dict modelo -> callable
        self.chamadas = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.chamadas.append(model)
        acao = self.comportamento.get(model)
        if isinstance(acao, Exception):
            raise acao
        return acao


def _gerenciador(clientes, modelos):
    it = iter(clientes)
    esperas = []
    g = GerenciadorGemini(["k"] * len(clientes), modelos, sleep=esperas.append, client_factory=lambda k: next(it))
    return g, esperas


def test_rotaciona_chave_em_429():
    c1 = ClienteFake({"m1": _api_error(429)})
    c2 = ClienteFake({"m1": _resposta("ok")})
    g, _ = _gerenciador([c1, c2], ["m1"])
    assert g.extrair("conteudo", "prompt") == "ok"


def test_404_vai_para_cache_e_proximo_modelo():
    c = ClienteFake({"velho": _api_error(404), "novo": _resposta("ok")})
    g, _ = _gerenciador([c], ["velho", "novo"])
    assert g.extrair("c", "p") == "ok"
    assert g.extrair("c", "p") == "ok"
    assert c.chamadas.count("velho") == 1


def test_backoff_limitado_e_falha_final():
    c = ClienteFake({"m1": _api_error(503)})
    g, esperas = _gerenciador([c], ["m1"])
    with pytest.raises(FalhaExtracao):
        g.extrair("c", "p", max_rodadas=4, backoff_base=15, backoff_max=40)
    assert len(esperas) == 3
    assert all(e <= 45 for e in esperas)


def test_truncado_tenta_outro_modelo_e_usa_parcial_se_preciso():
    c = ClienteFake({
        "m1": _resposta("parcial longo", types.FinishReason.MAX_TOKENS),
        "m2": _resposta("curto", types.FinishReason.MAX_TOKENS),
    })
    g, esperas = _gerenciador([c], ["m1", "m2"])
    assert g.extrair("c", "p") == "parcial longo"
    assert esperas == []


def test_resposta_vazia_e_transitoria():
    c = ClienteFake({"m1": _resposta(None), "m2": _resposta("ok")})
    g, _ = _gerenciador([c], ["m1", "m2"])
    assert g.extrair("c", "p") == "ok"


def test_orcamento_de_tempo_limita_rodadas():
    relogio = [0.0]
    esperas = []

    def dormir(s):
        esperas.append(s)
        relogio[0] += s

    c = ClienteFake({"m1": _api_error(503)})
    g = GerenciadorGemini(["k"], ["m1"], sleep=dormir, client_factory=lambda k: c, clock=lambda: relogio[0])
    with pytest.raises(FalhaExtracao):
        g.extrair("c", "p", max_rodadas=50, backoff_base=20, backoff_max=300, tempo_max=120)
    assert sum(esperas) <= 120
    assert len(esperas) < 49


def test_timeout_e_transitorio_e_tenta_proximo_modelo():
    import httpx

    c = ClienteFake({"lento": httpx.ReadTimeout("timed out"), "m2": _resposta("ok")})
    g, _ = _gerenciador([c], ["lento", "m2"])
    assert g.extrair("c", "p") == "ok"


# --- Provedor alternativo ------------------------------------------------------

class RespostaHTTP:
    def __init__(self, status, dados=None, texto=""):
        self.status_code = status
        self._dados = dados
        self.text = texto or str(dados)

    def json(self):
        return self._dados

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _post_fake(respostas):
    chamadas = []
    fila = list(respostas)

    def post(url, json, headers, timeout):
        chamadas.append({"url": url, "json": json, "headers": headers})
        return fila.pop(0)
    return post, chamadas


def test_alternativo_anthropic_formato_e_retentativa_em_529():
    from espelho_magico.llm import ProvedorAlternativo

    ok = RespostaHTTP(200, {"content": [{"type": "text", "text": "# Edição"}], "stop_reason": "end_turn"})
    post, chamadas = _post_fake([RespostaHTTP(529, texto="overloaded"), ok])
    p = ProvedorAlternativo("anthropic", "k", "claude-x", sleep=lambda s: None, post=post)
    assert p.gerar("conteudo", "prompt") == "# Edição"
    assert chamadas[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert chamadas[0]["json"]["system"] == "prompt"
    assert chamadas[0]["headers"]["x-api-key"] == "k"
    assert len(chamadas) == 2


def test_alternativo_openai_remove_temperature_se_recusada():
    from espelho_magico.llm import ProvedorAlternativo

    recusa = RespostaHTTP(400, texto='{"error": "Unsupported value: temperature"}')
    ok = RespostaHTTP(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    post, chamadas = _post_fake([recusa, ok])
    p = ProvedorAlternativo("openai", "k", "gpt-x", sleep=lambda s: None, post=post)
    assert p.gerar("c", "p") == "ok"
    assert "temperature" in chamadas[0]["json"] and "temperature" not in chamadas[1]["json"]
    assert "max_completion_tokens" in chamadas[1]["json"]


def test_alternativo_erro_de_cliente_nao_insiste():
    from espelho_magico.llm import ProvedorAlternativo

    post, chamadas = _post_fake([RespostaHTTP(401, texto="invalid x-api-key")])
    p = ProvedorAlternativo("anthropic", "k", "m", sleep=lambda s: None, post=post)
    with pytest.raises(FalhaExtracao):
        p.gerar("c", "p")
    assert len(chamadas) == 1


class AltFake:
    nome = "fake/alt"

    def __init__(self, resultado):
        self.resultado = resultado
        self.chamadas = 0

    def gerar(self, conteudo, prompt):
        self.chamadas += 1
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return self.resultado


def test_extrator_cai_no_alternativo_quando_gemini_fora_do_ar():
    from espelho_magico.llm import Extrator

    c = ClienteFake({"m1": _api_error(503)})
    g, _ = _gerenciador([c], ["m1"])
    g.extrair = lambda conteudo, prompt: (_ for _ in ()).throw(FalhaExtracao("503 em tudo"))
    alt = AltFake("texto do claude")
    assert Extrator(gemini=g, alternativo=alt, ia_padrao="gemini").extrair("c", "p") == "texto do claude"
    assert alt.chamadas == 1


def test_extrator_alt_como_principal_nao_chama_gemini():
    from espelho_magico.llm import Extrator

    c = ClienteFake({})
    g, _ = _gerenciador([c], ["m1"])
    alt = AltFake("ok")
    assert Extrator(gemini=g, alternativo=alt, ia_padrao="alt").extrair("c", "p") == "ok"
    assert c.chamadas == []


def test_perplexity_desativa_busca_externa(monkeypatch):
    import requests
    from espelho_magico import config
    from espelho_magico.llm import extrair_com_perplexity

    capturado = {}

    def post_fake(url, json, headers, timeout):
        capturado["json"] = json
        return RespostaHTTP(200, {"choices": [{"message": {"content": "# 7 de novembro de 2026\n\nTexto"}}]})

    monkeypatch.setattr(config, "PERPLEXITY_API_KEY", "pplx-fake")
    monkeypatch.setattr(requests, "post", post_fake)
    out = extrair_com_perplexity("conteudo", "prompt")
    assert "7 de novembro" in out
    assert capturado["json"]["disable_search"] is True


def test_dividir_conteudo_com_metadados_e_unir_partes():
    from espelho_magico.llm import dividir_conteudo_com_metadados, unir_partes_extraidas

    ed1 = "<metadados>\n[METADADOS DO E-MAIL — Data do e-mail: 9 de novembro de 2026]\n</metadados>\n\n" + (
        "\n\n## Notícia A\n" + ("Parágrafo longo da notícia A. " * 120)
        + "\n\n## Notícia B\n" + ("Parágrafo longo da notícia B. " * 120)
    )
    ed2 = "<metadados>\n[METADADOS DO E-MAIL — Data do e-mail: 8 de novembro de 2026]\n</metadados>\n\n" + (
        "\n\n## Notícia C\n" + ("Parágrafo longo da notícia C. " * 120)
    )
    combinado = (
        f"<documento_entrada>\n--- INÍCIO DA EDIÇÃO 1 ---\n\n{ed1}\n\n"
        f"--- INÍCIO DA EDIÇÃO 2 ---\n\n{ed2}\n</documento_entrada>"
    )
    partes = dividir_conteudo_com_metadados(combinado, max_chars=6000)
    assert len(partes) >= 3
    assert all(len(p) <= 6000 for p in partes)
    assert all("<metadados>" in p and "<documento_entrada>" in p for p in partes)

    unido = unir_partes_extraidas([
        "# 9 de novembro de 2026\n\n## Notícia A\n\nCorpo A.",
        "# 9 de novembro de 2026\n\n## Notícia B\n\nCorpo B.",
        "# 8 de novembro de 2026\n\n## Notícia C\n\nCorpo C.",
    ])
    assert unido.count("# 9 de novembro de 2026") == 1
    assert "# 8 de novembro de 2026" not in unido
    assert "## Notícia A" in unido and "## Notícia B" in unido and "## Notícia C" in unido


def test_gemini_504_divide_entrada_longa_sem_downgrade_de_modelo():
    class ClienteSensivelTamanho:
        def __init__(self):
            self.chamadas = []
            self.models = self

        def generate_content(self, model, contents, config):
            texto_entrada = contents[0]
            self.chamadas.append((model, len(texto_entrada)))
            if len(texto_entrada) > 5000:
                raise _api_error(504)
            return _resposta(f"# 9 de novembro de 2026\n\n## Parte extraída ({len(texto_entrada)})")

    c = ClienteSensivelTamanho()
    g, _ = _gerenciador([c], ["gemini-3.8-flash", "gemini-3.7-flash"])
    entrada_grande = (
        "<metadados>\n[METADADOS DO E-MAIL — Data do e-mail: 9 de novembro de 2026]\n</metadados>\n\n"
        "<documento_entrada>\n## Bloco 1\n" + ("Texto 1. " * 450)
        + "\n\n## Bloco 2\n" + ("Texto 2. " * 450) + "\n</documento_entrada>"
    )
    assert len(entrada_grande) >= 6000
    saida = g.extrair(entrada_grande, "prompt")
    # Todas as chamadas usaram exclusivamente o modelo topo (gemini-3.8-flash), sem cair para o 3.7
    assert [m for m, _ in c.chamadas] == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.8-flash"]
    assert saida.count("# 9 de novembro de 2026") == 1
    assert saida.count("## Parte extraída") == 2


def test_alternativo_413_divide_entrada_e_une_resposta():
    from espelho_magico.llm import ProvedorAlternativo

    erro_413 = RespostaHTTP(
        413,
        texto='{"error":{"message":"Request too large for model openai/gpt-oss-120b on TPM: Limit 8000, Requested 15184"}}',
    )
    ok_1 = RespostaHTTP(200, {"choices": [{"message": {"content": "# 9 de novembro de 2026\n\n## Parte 1\n\nTexto 1"}, "finish_reason": "stop"}]})
    ok_2 = RespostaHTTP(200, {"choices": [{"message": {"content": "# 9 de novembro de 2026\n\n## Parte 2\n\nTexto 2"}, "finish_reason": "stop"}]})
    post, chamadas = _post_fake([erro_413, ok_1, ok_2])
    p = ProvedorAlternativo("openai", "k", "openai/gpt-oss-120b", base_url="https://api.groq.com/openai/v1", sleep=lambda s: None, post=post)

    entrada = "## Artigo 1\n" + ("Conteúdo A. " * 120) + "\n\n## Artigo 2\n" + ("Conteúdo B. " * 120)
    saida = p.gerar(entrada, "prompt")
    assert len(chamadas) == 3
    assert saida.startswith("# 9 de novembro de 2026")
    assert "## Parte 1" in saida and "## Parte 2" in saida


