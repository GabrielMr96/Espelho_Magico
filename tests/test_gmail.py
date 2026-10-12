import base64

from espelho_magico.gmail_client import decodificar_base64, extrair_corpo


def _b64(texto, charset="utf-8"):
    return base64.urlsafe_b64encode(texto.encode(charset)).decode().rstrip("=")


def _parte(mime, texto, charset="utf-8", filename=""):
    return {
        "mimeType": mime,
        "filename": filename,
        "headers": [{"name": "Content-Type", "value": f'{mime}; charset="{charset}"'}],
        "body": {"data": _b64(texto, charset)},
    }


def test_prioriza_html_mesmo_com_text_plain_antes():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [_parte("text/plain", "versão texto"), _parte("text/html", "<p>versão html</p>")],
    }
    html, texto = extrair_corpo(payload)
    assert html == "<p>versão html</p>"
    assert texto == "versão texto"


def test_html_aninhado_e_anexo_ignorado():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [_parte("text/plain", "a"), _parte("text/html", "<b>b</b>")]},
            _parte("text/html", "<i>anexo</i>", filename="anexo.html"),
        ],
    }
    html, _ = extrair_corpo(payload)
    assert html == "<b>b</b>"


def test_respeita_charset_latin1():
    payload = _parte("text/html", "<p>Notícias de ação</p>", charset="iso-8859-1")
    html, _ = extrair_corpo(payload)
    assert html == "<p>Notícias de ação</p>"


def test_decodificacao_tolerante():
    dados = base64.urlsafe_b64encode(b"ok \xff\xfe fim").decode()
    assert decodificar_base64(dados).startswith("ok ")
    assert decodificar_base64(dados, "charset-inexistente").endswith("fim")


def test_somente_texto_puro():
    html, texto = extrair_corpo(_parte("text/plain", "só texto"))
    assert html is None and texto == "só texto"
