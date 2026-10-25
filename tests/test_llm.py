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
