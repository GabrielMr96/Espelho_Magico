from espelho_magico.html_cleaner import html_para_markdown, limpar_linhas, preparar_conteudo

HTML = """
<html><head><style>.x{color:red}</style><title>t</title></head>
<body>
  <div style="display:none">preheader escondido</div>
  <table><tr><td>
    <h1>12 de outubro de 2026</h1>
    <h2>Notícia importante</h2>
    <p>O texto da <a href="https://tracking.example/abc?x=1">notícia</a> continua aqui com *ênfase* e snake_case.</p>
    <img src="https://img/foto.jpg" alt="Presidente discursa no Congresso">
    <img src="https://img/logo.png" alt="logo">
    <img src="https://track/p.gif" width="1" height="1" alt="pixel de tracking">
    <p>Leia mais aqui</p>
    <p>© 2026 Newsletter</p>
  </td></tr></table>
  <script>alert(1)</script>
</body></html>
"""


def test_remove_lixo_e_preserva_conteudo():
    md = html_para_markdown(HTML)
    assert "# 12 de outubro de 2026" in md  # data mantida (vira o título principal)
    assert "## Notícia importante" in md
    assert "O texto da notícia continua aqui com *ênfase* e snake_case." in md
    assert "[Imagem: Presidente discursa no Congresso]" in md
    for lixo in ("tracking.example", "logo", "pixel", "preheader", "alert", "color:red", "Leia mais", "©", "|"):
        assert lixo not in md, lixo


def test_preparar_conteudo_fallback_para_html_curto():
    html = "<p>curto</p>"
    assert preparar_conteudo(html) == html
    assert preparar_conteudo(HTML, habilitado=False) == HTML


def test_limpar_linhas_normaliza_espacos():
    assert limpar_linhas("a   b\n\n\n\nc  \nClique aqui para ver") == "a b\n\nc"


def test_remove_creditos_fotograficos():
    html = """
    <div>
        <p>Notícia</p>
        <img src="1.jpg" alt="Fortune | Getty Images">
        <img src="2.jpg" alt="Manoella Mello | Globo">
        <img src="3.jpg" alt="Reprodução / X">
        <img src="4.jpg" alt="Foto: João Silva">
        <img src="5.jpg" alt="Gráfico detalhado mostrando a inflação de 2026">
    </div>
    """
    md = html_para_markdown(html)
    assert "[Imagem: Gráfico detalhado mostrando a inflação de 2026]" in md
    assert "Fortune" not in md
    assert "Manoella Mello" not in md
    assert "Getty Images" not in md
    assert "João Silva" not in md
