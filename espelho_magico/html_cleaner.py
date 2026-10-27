"""Pré-limpeza do HTML das newsletters antes de enviar à IA.

Remove estilos, scripts, pixels de rastreamento, URLs e lixo recorrente e converte para
Markdown. Isso reduz muito os tokens enviados, o que diminui os erros 429 e o risco de
truncamento da resposta.
"""
import re

from bs4 import BeautifulSoup, Comment
from markdownify import markdownify

_TAGS_REMOVIDAS = ["script", "style", "head", "meta", "link", "noscript", "title", "svg", "iframe", "form"]

_ALT_DECORATIVO = re.compile(
    r"^(logo|logotipo|image|imagem|img|icon|ícone|icone|spacer|banner|pixel|"
    r"divider|separador|avatar|foto|photo|picture|header|footer|rodapé.*|capa.*|\s*)$",
    re.I,
)

# Termos comuns em créditos puros de fotógrafo / agência (ruído puro para áudio no NotebookLM)
_CREDITO_PADRAO = re.compile(
    r"^(?:foto|cr[eé]dito|imagem|fonte|por|by|source|photo|image|credit|illustration|artwork(?:\s+by)?)\s*[:\-–—]\s*.+$|"
    r"^[^\n|/–—]{1,40}\s*(?:[|/–—]|\bvia\b)\s*(?:reprodu[cç][aã]o|getty(?:\s*images)?|globo|uol|afp|reuters|"
    r"estad[aã]o|folha|x|twitter|unsplash|divulga[cç][aã]o|fortune|bloomberg|ap|shutterstock|"
    r"vatican|axios|metr[oó]poles|ge|estadao|cnn|bbc|cnbc|divulga|instagram|facebook|flickr|fanduel|apollo)\s*\.?$",
    re.I,
)

# Linhas descartadas após a conversão (CTAs, rodapés e cabeçalhos de e-mail).
_PADROES_LINHA = [
    r"(?i)^\W*\(?leia mais(?:\s+aqui)?\)?\W*$",
    r"(?i)^\W*\(?clique aqui(?:\s+para\s+.*)?\)?\W*$",
    r"(?i)^\W*\(?aprofund[ea]\)?\W*$",
    r"(?i)^\W*no que estamos clicando\W*$",
    r"(?i)^\W*(?:compartilhe|compartilhar|compartilhe no whatsapp)\b.*$",
    r"(?i)^\W*clique para compartilhar\b.*$",
    r"(?i)^\W*ouça este episódio\b.*$",
    r"(?i)^\W*responda à nossa pesquisa\b.*$",
    r"(?i)^Responder a:.*$",
    r"(?i)^Para:.*$",
    r"<[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}>",
    r"(?i)^Boas-vindas às.*$",
    r"(?i)^.*Leia Online.*$",
    r"(?i)^\s*Read online\s*$",
    r"(?i)^(?:Source|Photo|Image|Credit|Illustration|Artwork(?:\s+by)?)\s*:\s*[^\n]{1,80}$",
    r"(?i)^Assine a .* PRO para continuar lendo.*$",
    r"(?i)^Assinar e continuar lendo.*$",
    r"(?i)^.*Go Premium to keep reading.*$",
    r"(?i)^Atualize suas preferências de e-mail.*$",
    r"(?i)^.*(cancelar inscrição|descadastr|unsubscribe).*$",
    r"(?i)^© \d{4}.*$",
    r"(?i)^Reservar minha vaga.*$",
]
_PADROES_COMPILADOS = [re.compile(p, re.MULTILINE) for p in _PADROES_LINHA]


def _eh_alt_informativo(alt):
    alt = alt.strip()
    if not alt or len(alt) < 4:
        return False
    if _ALT_DECORATIVO.match(alt):
        return False
    if re.search(r"\.(?:png|jpe?g|gif|webp|svg)$", alt, re.I):
        return False
    if alt.startswith("http://") or alt.startswith("https://"):
        return False
    if _CREDITO_PADRAO.match(alt):
        return False
    if alt.lower() in ("getty images", "reprodução", "divulgação", "afp", "reuters", "the news", "tns", "axios"):
        return False
    return True


def _eh_pixel(img):
    for attr in ("width", "height"):
        valor = str(img.get(attr, "")).strip().lower().replace("px", "")
        if valor.isdigit() and int(valor) <= 3:
            return True
    estilo = str(img.get("style", "")).replace(" ", "").lower()
    return "width:1px" in estilo or "height:1px" in estilo or "display:none" in estilo


def _eh_oculto(tag):
    estilo = str(tag.get("style", "")).replace(" ", "").lower()
    return "display:none" in estilo or "visibility:hidden" in estilo or "max-height:0" in estilo


def _compactar_linhas_tabela_dados(soup):
    """Preserva linhas de tabelas de dados (ex: fechamento de mercado, altas/baixas) na mesma linha.

    Tabelas de layout têm `<table>` aninhadas ou parágrafos longos dentro das células.
    Já tabelas de dados têm `<tr>` com 2 a 6 células (`<td>`/`<th>`) curtas e sem tabelas filhas.
    Agrupar essas células por ` | ` evita que uma tabela de cotações vire 200 linhas verticais soltas.
    """
    for tr in soup.find_all("tr"):
        if tr.decomposed or tr.find("table"):
            continue
        celulas = tr.find_all(["td", "th"], recursive=False)
        if not (2 <= len(celulas) <= 6):
            continue
        if any(c.find(["h1", "h2", "h3", "h4", "ul", "ol", "blockquote"]) for c in celulas):
            continue
        textos = [" ".join(c.get_text(" ", strip=True).split()) for c in celulas]
        textos_nao_vazios = [t for t in textos if t and t not in ("▲", "▼")]
        if len(textos_nao_vazios) >= 2 and all(len(t) <= 60 for t in textos_nao_vazios):
            linha_compacta = " | ".join(t for t in textos if t)
            novo_p = soup.new_tag("p")
            novo_p.string = linha_compacta
            tr.replace_with(novo_p)


def limpar_linhas(texto):
    """Aplica as regex de lixo recorrente e normaliza espaços."""
    for padrao in _PADROES_COMPILADOS:
        texto = padrao.sub("", texto)
    texto = texto.replace("\x0c", "").replace("\u00a0", " ").replace("\u200c", "").replace("\u200b", "")
    texto = re.sub(r"[ \t]{2,}", " ", texto)
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def html_para_markdown(html):
    """Converte o HTML do e-mail em Markdown enxuto.

    As imagens viram `[Imagem: texto alternativo]` (somente quando o alt é informativo),
    porque a IA não enxerga a imagem e não deve inventar uma descrição.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(_TAGS_REMOVIDAS):
        tag.decompose()
    for comentario in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comentario.extract()
    for tag in soup.find_all(_eh_oculto):
        if not tag.decomposed:
            tag.decompose()

    for img in soup.find_all("img"):
        if img.decomposed:
            continue
        alt = (img.get("alt") or "").strip()
        if _eh_pixel(img) or not _eh_alt_informativo(alt):
            img.decompose()
        else:
            img.replace_with(f" [Imagem: {alt}] ")

    # Preserva linhas de tabelas de dados (cotações, índices, altas/baixas) antes de desmontar tabelas de layout
    _compactar_linhas_tabela_dados(soup)

    # E-mails usam tabelas aninhadas para layout; transformá-las em blocos evita
    # tabelas Markdown quebradas e economiza tokens.
    for tag in soup.find_all(["table", "tbody", "thead", "tfoot", "tr", "td", "th", "center"]):
        tag.name = "div"
        tag.attrs = {}

    markdown = markdownify(
        str(soup), heading_style="ATX", strip=["a"], bullets="-",
        escape_asterisks=False, escape_underscores=False,
    )
    return limpar_linhas(markdown)


def preparar_conteudo(html, habilitado=True, minimo_caracteres=200):
    """Devolve o conteúdo a enviar para a IA (Markdown limpo ou o HTML original como fallback)."""
    if not habilitado or not html:
        return html
    try:
        markdown = html_para_markdown(html)
    except Exception:
        return html
    return markdown if len(markdown) >= minimo_caracteres else html
