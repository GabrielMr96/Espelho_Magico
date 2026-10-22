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
