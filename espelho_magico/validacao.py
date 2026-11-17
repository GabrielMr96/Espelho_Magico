"""Dupla verificação pós-extração.

Depois que a IA devolve o Markdown, este módulo confere se as regras do prompt de extração
(`prompts/espelho_magico.md`) foram cumpridas e se o arquivo está no formato que o prompt do
podcast (`prompts_notebooklm/`) espera. O que for seguro é corrigido automaticamente; o resto
vira aviso no relatório exibido no terminal e gravado no log.

Regras verificadas:
  1. Cabeçalho `#` com data na linha 1 e H1 único
  2. Notícias extraídas (seções `##`)
  3. Integridade: números da fonte preservados + proporção de tamanho
  4. Sem tabelas cruas (converte para tópicos narrativos de áudio)
  5. Sem links/imagens crus (o ouvinte não clica em nada)
  6. Sem ruído residual (publicidade, paywall, sumários)
  7. Idioma consistente (code-switching em PT / títulos em PT num documento EN)
  8. Voz dos Ouvintes unificada e adaptada ("leitores" → "ouvintes")
  9. Enquete do Dia presente com opções (gera com IA se faltar; fallback determinístico)
 10. Bloco final na ordem: notícias → Voz dos Ouvintes → Enquete do Dia
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from rich import box
from rich.markup import escape
from rich.table import Table

OK, CORRIGIDO, AVISO, FALHA = "ok", "corrigido", "aviso", "falha"
ICONES = {OK: "✔", CORRIGIDO: "🔧", AVISO: "⚠", FALHA: "✖"}
CORES = {OK: "green", CORRIGIDO: "cyan", AVISO: "yellow", FALHA: "red"}

TITULO_ENQUETE = {"pt": "Enquete do Dia", "en": "Daily Poll"}
TITULO_VOZ = {"pt": "Voz dos Ouvintes", "en": "Listener Feedback"}

_RE_H2 = re.compile(r"^##\s+(.+?)\s*$")
_RE_CABECALHO_SEC = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_ENQUETE_NOME = (
    r"(?:enquete(?:\s+(?:d[oe]\s+dia|de\s+hoje))?|daily\s+poll|poll(?:\s+of\s+the\s+day)?|today'?s\s+poll|the\s+poll)"
)
_RE_ENQUETE = re.compile(
    rf"^{_ENQUETE_NOME}(?:\s*[/|—–\-]\s*{_ENQUETE_NOME})*\s*$",
    re.I,
)
_RE_VOZ = re.compile(
    r"^(voz\s+d[oe]s?\s+(leitor|leitores|ouvinte|ouvintes)|opini[aã]o\s+d[oe]s?\s+(leitor|leitores|ouvinte|ouvintes)|"
    r"coment[aá]rios\s+d[oe]s?\s+(leitores|ouvintes)|resultado\s+da\s+enquete.*|"
    r"listener\s+feedback|reader\s+(feedback|comments)|poll\s+results.*)$",
    re.I,
)
_RE_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
_RE_BULLET_CAPTURA = re.compile(r"^([ \t]*)(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
_RE_LINHA_TABELA = re.compile(r"^\s*\|.*\|\s*$")
_RE_SEPARADOR_TABELA = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_RE_IMAGEM_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK_MD = re.compile(r"(?<!!)\[([^\]]+)\]\((?:https?://|www\.)[^)]*\)")
_RE_URL_CRUA = re.compile(r"\s*\(?\b(?:https?://|www\.)\S+?\)?(?=[\s,;]|$)")
_RE_URL_TEXTO = re.compile(r"https?://\S+|www\.\S+|\]\([^)]*\)")
_RE_NUMERO = re.compile(r"\d+(?:[.,]\d+|[\u00a0\u202f]\d{3})*")
_RE_META_BLOCO = re.compile(r"<metadados>.*?</metadados>", re.S | re.I)
_RE_TAGS_DOC = re.compile(r"</?(?:documento_entrada|metadados)>", re.I)
_RE_IMG_DESC = re.compile(r"\[(?:Descrição de Imagem|Imagem):[^\]]*\]", re.I)
_RE_GERADO_EM = re.compile(r"^\s*(?:Gerado|Generated)\s+(?:em|on|at)\b.*$", re.I | re.M)
_RE_TEMPO_LEITURA = re.compile(
    r"^\s*\d+(?:\s*[–-]\s*\d+)?\s*(?:min|minutos?|minutes?)(?:\s+de\s+leitura)?\s*$",
    re.I | re.M,
)
_RE_HORA = re.compile(r"(?:\bàs\s+\d{1,2}h(?:\d{2})?\b|\bàs\s+\d{1,2}:\d{2}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b)", re.I)
_RE_DATA_NUM = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_RE_LEIA_TAMBEM = re.compile(
    r"^#{2,4}[ \t]*(?:Leia\s+também|Veja\s+também|Saiba\s+mais|Read\s+also|Related)\b[^\n]*"
    r"(?:\n(?:[ \t]*\n)?[ \t]*[-*•][ \t]+[^\n]+)*",
    re.I | re.M,
)
_RE_SECAO_RUIDO = re.compile(
    r"^(?:"
    r"Peak\s+Premium(?:\s*&\s*Advertising)?|"
    r"Advertise\s+with\b.*|"
    r"(?:[ÚU]ltima\s+chamada|Concorra\s+a|Sorteio)\b.*\biPhone\b.*|"
    r"Programa\s+de\s+indica[cç][aã]o.*|"
    r"Indique\s+(?:amigos|e\s+ganhe|o\s+the\s+news).*|"
    r"D[eê]nius\s+Games(?:\b.*)?|"
    r"Campanha\s+FOMO\s+Zero(?:\b.*)?|"
    r"Dropped\s+by\b.*"
    r")$",
    re.I,
)
_RE_SECAO_CLICKBAIT = re.compile(r"^(?:Extras?|Quick\s+Takes?)$", re.I)
_RE_CLICKBAIT_CHAMADA = (
    r"^\s*(?:[-*•>]\s*)?\*{0,2}(?:"
    r"PARA\s+(?:VER|CLICAR|LER|OUVIR|ASSISTIR|SABER|REFLETIR|ENTENDER)|"
    r"DESCUBRA|NESTE\s+DIA|SAIBA\s+MAIS|VEJA\s+AQUI|CONFIRA"
    r")\s*:\*{0,2}(?:\s+.*)?$"
)
_RE_CLICKBAIT_LINHA = re.compile(_RE_CLICKBAIT_CHAMADA)
_RE_CLICKBAIT_LINHA_CI = re.compile(_RE_CLICKBAIT_CHAMADA, re.I)
_RE_TITULO_PATROCINADOR = re.compile(
    r"^(#{2,3}\s+.+?)\s*(?:\(\s*(?:ARQ|Oferecimento|Patroc[ií]nio|Publieditorial|Sponsored|Ad)\s*\)|"
    r"[—\-–]\s*(?:Oferecimento|Patroc[ií]nio|Publieditorial)\b.*)$",
    re.I,
)
_RE_TITULO_PEAK_PICKS = re.compile(r"^(#{2,3}\s+)Peak\s+Picks\s*$", re.I)
_RE_RUIDO = re.compile(
    r"(?:\b(?:assine\s+(?:j[aá]|agora|a\s+pro)|assinatura\s+premium|"
    r"conte[uú]do\s+patrocinado|publieditorial|sponsored\s+content|"
    r"go\s+premium|upgrade\s+to\s+(?:pro|premium)|continue\s+lendo|keep\s+reading|"
    r"indique\s+(?:um\s+amigo|e\s+ganhe)|refer\s+a\s+friend|use\s+o\s+cupom|"
    r"Try\s+Peak\s+Premium|Advertise\s+with\s+The\s+Peak|Head\s+Office|"
    r"Taxas\s+e\s+ativos\s+no\s+ARQ|Investback\s+de\s+lan[cç]amento|"
    r"Destaque\s+editorial|"
    r"concorrer\s+a\s+(?:um|uma)\s+\*{0,2}iPhone\b|"
    r"(?:para\s+)?receber\s+o\s+\*{0,2}the\s+news\*{0,2}\b|"
    r"(?:registrando|batendo|bater)\s+(?:o\s+|seu\s+)\*{0,2}Streak\*{0,2}\b|"
    r"My\s+Minerva\s+Foods|Kit\s+Anti\s+FOMO\s+Club)\b|"
    r"👉\s*(?:Veja|Confira|Acesse|Clique)\s+aqui\b|"
    r"O\s+que\s+(?:voc[eê]\s+)?achou\s+d[ae]\s+edi[cç][aã]o\s+de\s+hoje\??|"
    r"Dropped\s+by\b|"
    r"^#{2,3}\s+(?:Peak\s+Picks|D[eê]nius\s+Games|Campanha\s+FOMO\s+Zero)\b|"
    r"^\s*(?:[-*•]\s*)?\*{0,2}(?:oferecimento|apresentado\s+por|sponsored\s+by|together\s+with|"
    r"presented\s+by|na\s+edi[cç][aã]o\s+de\s+hoje|in\s+today'?s\s+(?:edition|issue))\*{0,2}\s*:)",
    re.I | re.M,
)
_RE_RUIDO_LINHA = re.compile(
    r"^\s*(?:[-*•>]\s*)?(?:\*{0,2}(?:"
    r"(?:oferecimento|apresentado\s+por|sponsored\s+by|together\s+with|"
    r"presented\s+by|na\s+edi[cç][aã]o\s+de\s+hoje|in\s+today'?s\s+(?:edition|issue)|"
    r"Try\s+Peak\s+Premium|Advertise\s+with(?:\s+The\s+Peak)?|Head\s+Office|"
    r"Taxas\s+e\s+ativos\s+n[oa]\s+ARQ|Investback\s+de\s+lan[cç]amento|Cashback\s+de\s+lan[cç]amento|"
    r"Destaque\s+editorial)\b[^:\n]*:?\*{0,2}|"
    r"(?:assine\s+(?:j[aá]|agora|a\s+pro)|assinatura\s+premium|conte[uú]do\s+patrocinado|"
    r"publieditorial|sponsored\s+content|go\s+premium|upgrade\s+to\s+(?:pro|premium)|"
    r"continue\s+lendo|keep\s+reading|indique\s+(?:um\s+amigo|e\s+ganhe)|"
    r"refer\s+a\s+friend|use\s+o\s+cupom)\b|"
    r"Next\s+week,\s+a\s+(?:meditation|look|deep\s+dive|tour|preview)\s+on\b|"
    r"O\s+que\s+(?:voc[eê]\s+)?achou\s+d[ae]\s+edi[cç][aã]o\s+de\s+hoje\??\s*\*{0,2}$|"
    r"(?:🥇|🥈|🥉)\s*(?:Gostei|Bem\s+mais\s+ou\s+menos|Mais\s+ou\s+menos|N[aã]o\s+gostei)\b[^.\n]*$|"
    r"🤩\s*Baita\b[^.\n]*$|"
    r"(?:EDI[ÇC][ÃA]O\s*#\d+\b|Essa\s+news\s+foi\s+encaminhada|Conhe[çc]a\s+as\s+outras\s+news|"
    r"Elevando\s+o\s+QI\s+da\s+internet|Dropped\s+pelos\s+humanos|Se\s+amarra\s+em\s+Youtube|"
    r"Rod\.\s+[^,\n]+,\s*\d+\b)|"
    r"Quer\s+fazer\s+sua\s+marca\b.*\bAnuncie\b)|"
    r".*?(?:\b\d{5}-\d{3}\b(?:,\s*(?:Brazil|Brasil))?\s*$|"
    r"concorrer\s+a\s+(?:um|uma)\s+\*{0,2}iPhone\b|"
    r"indicando\b.*?\b(?:para\s+receber\s+o\s+\*{0,2}the\s+news\*{0,2}|\*{0,2}Streak\*{0,2})|"
    r"(?:registrando|batendo|bater)\s+(?:o\s+|seu\s+)\*{0,2}Streak\*{0,2}\b|"
    r"\bhub\s+gratuito\b.*?(?:te\s+mostra|confira|veja\s+aqui)|"
    r"apenas\s+nas\s+pr[oó]ximas\s+\*{0,2}\d+\s*horas\*{0,2},?\s+o\s+\*{0,2}ARQ\*{0,2}\s+devolve\b))",
    re.I,
)
_RE_RUIDO_INLINE = re.compile(
    r"(?:\s*(?:[—\-–|]\s*|\(\s*)?(?:\b(?:continue\s+lendo|keep\s+reading|assine\s+(?:j[aá]|agora|a\s+pro)|"
    r"upgrade\s+to\s+(?:pro|premium)|go\s+premium|conte[uú]do\s+patrocinado|sponsored\s+content)\b|"
    r"Next\s+week,\s+a\s+(?:meditation|look|deep\s+dive|tour|preview)\s+on\b|"
    r"👉\s*(?:Veja|Confira|Acesse|Clique)\s+aqui\b)[^.!?\n]*[.!?\)]?|"
    r"\bNesta\s+mat[eé]ria,\s+a\s+\*{0,2}My\s+Minerva\s+Foods\*{0,2}\s+explicou\s+que\s+)",
    re.I,
)
_MESES_PT = (
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)
_MESES_EN = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_DEZENAS_EXT = {
    "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50,
    "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_UNIDADES_EXT = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_CARDINAIS_EXT = {
    **_DEZENAS_EXT,
    "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19, "cem": 100,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "hundred": 100,
}
_RE_CODE_SWITCH = re.compile(
    r"\b(?:é|são|foi|foram|tendo|sendo|está|estão|ser|será|versões|versão|muito|mais)\s+"
    r"([a-z]{3,}(?:ed|ing))\b"
)
_ESTRANGEIRISMOS_PT = {
    "briefing", "bullying", "camping", "coaching", "coworking", "crowdfunding",
    "doping", "dumping", "gaming", "holding", "hosting", "leasing", "marketing",
    "networking", "parking", "phishing", "ranking", "rating", "roaming",
    "shopping", "streaming", "training",
}
_RE_TITULO_PT_EM_EN = re.compile(
    r"^#{2,3}\s+(Enquete|Fechamento|Voz dos|Notícias|Giro|Agenda do|Resultados|Opinião)", re.I | re.M
)
_RE_PALAVRAS_PT = re.compile(r"\b(não|você|também|então|porque|está|estão|são)\b", re.I)

_ROTULOS_COLUNA_PT = {"ytd": "no ano", "1 dia": "no dia", "dia": "no dia", "variação": "variação"}


# --- Relatório ---------------------------------------------------------------

def _amostra(itens, limite=3, aspas=True):
    """Formata uma lista curta de exemplos únicos para o usuário ler o que foi encontrado/corrigido."""
    vistos = []
    for item in itens:
        t = str(item).strip()
        if t and t not in vistos:
            vistos.append(t)
    if not vistos:
        return ""
    exibidos = [f"'{x}'" if aspas else x for x in vistos[:limite]]
    sufixo = f" (+{len(vistos) - limite})" if len(vistos) > limite else ""
    return ", ".join(exibidos) + sufixo


def _emitir(notificar, evento, regra, **dados):
    if notificar:
        notificar(evento, regra, **dados)


@dataclass
class Checagem:
    regra: str
    status: str
    detalhe: str = ""
    encontrado: str = ""
    corrigido: str = ""


@dataclass
class RelatorioValidacao:
    checagens: list = field(default_factory=list)

    def add(self, regra, status, detalhe="", encontrado="", corrigido=""):
        if not detalhe:
            if encontrado and corrigido:
                detalhe = f"Encontrado: {encontrado} → Corrigido: {corrigido}"
            elif encontrado:
                detalhe = f"Encontrado: {encontrado}"
        self.checagens.append(Checagem(regra, status, detalhe, encontrado, corrigido))

    def contagem(self):
        cont = {OK: 0, CORRIGIDO: 0, AVISO: 0, FALHA: 0}
        for c in self.checagens:
            cont[c.status] = cont.get(c.status, 0) + 1
        return cont

    @property
    def resumo(self):
        cont = self.contagem()
        partes = [f"{cont[OK]} ✔"]
        if cont[CORRIGIDO]:
            partes.append(f"{cont[CORRIGIDO]} 🔧")
        if cont[AVISO]:
            partes.append(f"{cont[AVISO]} ⚠")
        if cont[FALHA]:
            partes.append(f"{cont[FALHA]} ✖")
        return " · ".join(partes)

    @property
    def tem_aviso(self):
        return any(c.status in (AVISO, FALHA) for c in self.checagens)

    def como_texto(self):
        return "\n".join(f"  {ICONES[c.status]} {c.regra}: {c.detalhe}" for c in self.checagens)


def renderizar_relatorio(relatorio, titulo):
    """Tabela Rich com o resultado de cada regra (exibida no terminal ao fim de cada programa)."""
    tabela = Table(
        title=f"🔎 Dupla verificação · {escape(titulo)}",
        title_style="bold magenta",
        title_justify="left",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold",
        expand=False,
    )
    tabela.add_column("", width=2, justify="center")
    tabela.add_column("Regra", style="bold", no_wrap=True)
    tabela.add_column("Resultado")
    for c in relatorio.checagens:
        cor = CORES[c.status]
        if c.encontrado and c.corrigido:
            celula = (
                f"[yellow]Encontrado:[/yellow] {escape(c.encontrado)}\n"
                f"[cyan]Corrigido:[/cyan] {escape(c.corrigido)}"
            )
        elif c.encontrado and c.status in (AVISO, FALHA):
            etiqueta_cor = "red" if c.status == FALHA else "yellow"
            celula = f"[{etiqueta_cor}]Encontrado:[/{etiqueta_cor}] {escape(c.encontrado)}"
        else:
            celula = f"[{cor}]{escape(c.detalhe)}[/{cor}]"
        tabela.add_row(f"[{cor}]{ICONES[c.status]}[/{cor}]", escape(c.regra), celula)
    tabela.caption = f"Resumo: {relatorio.resumo}  (✔ ok · 🔧 corrigido automaticamente · ⚠ revisar)"
    tabela.caption_justify = "left"
    return tabela


# --- Seções ------------------------------------------------------------------

def dividir_secoes(md):
    """Separa o preâmbulo (H1 + texto antes do 1º ##) das seções `##` (cada uma com seus ###)."""
    preambulo, secoes, atual = [], [], None
    for linha in md.split("\n"):
        m = _RE_H2.match(linha)
        if m:
            atual = {"titulo": m.group(1).strip(), "linhas": [linha]}
            secoes.append(atual)
        elif atual is None:
            preambulo.append(linha)
        else:
            atual["linhas"].append(linha)
    return preambulo, secoes


def juntar_secoes(preambulo, secoes):
    partes = ["\n".join(preambulo).strip()] + ["\n".join(s["linhas"]).strip() for s in secoes]
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(p for p in partes if p)).strip()


def _sem_rabo(linhas):
    fim = len(linhas)
    while fim > 1 and linhas[fim - 1].strip() in ("", "---"):
        fim -= 1
    return linhas[:fim]


def _eh_voz(secao):
    return bool(_RE_VOZ.match(secao["titulo"]))


def _eh_enquete(secao):
    return bool(_RE_ENQUETE.match(secao["titulo"])) and not _eh_voz(secao)


def _eh_secao_noticia(secao):
    return not _eh_voz(secao) and not _eh_enquete(secao)


def contar_noticias(md):
    """Quantidade de seções `##` de conteúdo (exclui Voz dos Ouvintes e Enquete)."""
    return sum(1 for s in dividir_secoes(md or "")[1] if _eh_secao_noticia(s))


# --- Correções seguras ---------------------------------------------------------

def _celulas(linha):
    return [c.strip() for c in linha.strip().strip("|").split("|")]


def _converter_tabelas_detalhado(md, idioma="pt"):
    """Troca tabelas Markdown por tópicos narrativos e devolve detalhes do que foi convertido."""
    linhas = md.split("\n")
    saida, i, convertidas, linhas_convertidas, rotulos = [], 0, 0, 0, []
    while i < len(linhas):
        if (
            _RE_LINHA_TABELA.match(linhas[i])
            and i + 1 < len(linhas)
            and _RE_SEPARADOR_TABELA.match(linhas[i + 1])
        ):
            cabecalho = _celulas(linhas[i])
            j, itens = i + 2, []
            while j < len(linhas) and _RE_LINHA_TABELA.match(linhas[j]):
                celulas = _celulas(linhas[j])
                if any(celulas):
                    rotulo = celulas[0]
                    rotulo_limpo = rotulo.strip("*_ ").strip()
                    if rotulo_limpo:
                        rotulos.append(rotulo_limpo)
                    resto = []
                    for k in range(1, len(celulas)):
                        if not celulas[k]:
                            continue
                        nome_col = cabecalho[k] if k < len(cabecalho) else ""
                        if idioma == "pt":
                            nome_col = _ROTULOS_COLUNA_PT.get(nome_col.lower(), nome_col)
                        resto.append(f"{nome_col}: {celulas[k]}" if nome_col else celulas[k])
                    itens.append(f"- **{rotulo}:** " + " · ".join(resto) if resto else f"- {rotulo}")
                j += 1
            if saida and saida[-1].strip():
                saida.append("")
            saida.extend(itens)
            if j < len(linhas) and linhas[j].strip():
                saida.append("")
            convertidas += 1
            linhas_convertidas += len(itens)
            i = j
            continue
        saida.append(linhas[i])
        i += 1
    return "\n".join(saida), {
        "convertidas": convertidas,
        "linhas": linhas_convertidas,
        "rotulos": rotulos,
    }


def converter_tabelas_em_topicos(md, idioma="pt"):
    """Troca tabelas Markdown por tópicos `- **Ativo:** coluna valor · coluna valor` (100% dos dados)."""
    novo_md, detalhes = _converter_tabelas_detalhado(md, idioma)
    return novo_md, detalhes["convertidas"]


def _remover_links_e_imagens_detalhado(md):
    """Remove imagens Markdown, links e URLs cruas e devolve resumo do que foi encontrado."""
    links_md = _RE_LINK_MD.findall(md)
    n_imgs = len(_RE_IMAGEM_MD.findall(md))
    n_links_md = len(links_md)
    md = _RE_IMAGEM_MD.sub("", md)
    md = _RE_LINK_MD.sub(r"\1", md)
    urls_cruas, n_urls, novas = [], 0, []
    for linha in md.split("\n"):
        if re.search(r"https?://|www\.", linha):
            urls_cruas.extend(re.findall(r"(?:https?://|www\.)[^\s)\]]+", linha))
            n_urls += len(re.findall(r"https?://|www\.", linha))
            linha = _RE_URL_CRUA.sub("", linha).rstrip()
            if re.fullmatch(r"\s*[-*•]?\s*", linha):
                continue
        novas.append(linha)
    return "\n".join(novas), {
        "total": n_imgs + n_links_md + n_urls,
        "n_imgs": n_imgs,
        "n_links_md": n_links_md,
        "n_urls": n_urls,
        "links_md": links_md,
        "urls_cruas": urls_cruas,
    }


def remover_links_e_imagens(md):
    """Remove imagens Markdown, troca `[texto](url)` por `texto` e apaga URLs cruas (não narráveis)."""
    novo_md, detalhes = _remover_links_e_imagens_detalhado(md)
    return novo_md, detalhes["total"]


def _adaptar_ouvintes(linhas, idioma):
    trocas, exemplos = 0, []
    if idioma == "en":
        pares = [
            (r"\b(?:Subscribe\s+to|Sign\s+up\s+for)\s+our\s+newsletters?\b", "Follow our shows"),
            (r"\b(?:subscribe\s+to|sign\s+up\s+for)\s+our\s+newsletters?\b", "follow our shows"),
            (r"\bSubscribers\b", "Listeners"),
            (r"\bsubscribers\b", "listeners"),
            (r"\bSubscriber\b", "Listener"),
            (r"\bsubscriber\b", "listener"),
            (r"\bReaders\b", "Listeners"),
            (r"\breaders\b", "listeners"),
            (r"\bReader\b", "Listener"),
            (r"\breader\b", "listener"),
            (r"\bThe Peak\b", "The Out of Order Dispatch"),
            (r"\bnewsletter\b", "show"),
        ]
    else:
        pares = [
            (r"\bAssinem\s+(?:as\s+nossas\s+|nossas\s+)newsletters\b", "Acompanhem os nossos programas"),
            (r"\bassinem\s+(?:as\s+nossas\s+|nossas\s+)newsletters\b", "acompanhem os nossos programas"),
            (r"\bAssine\s+(?:a\s+nossa\s+|nossa\s+)newsletter\b", "Acompanhe o nosso programa"),
            (r"\bassine\s+(?:a\s+nossa\s+|nossa\s+)newsletter\b", "acompanhe o nosso programa"),
            (r"\bAssinantes\b", "Ouvintes"),
            (r"\bassinantes\b", "ouvintes"),
            (r"\bAssinante\b", "Ouvinte"),
            (r"\bassinante\b", "ouvinte"),
            (r"\bLeitores\b", "Ouvintes"),
            (r"\bleitores\b", "ouvintes"),
            (r"\bLeitor(a)?\b", "Ouvinte"),
            (r"\bleitor(a)?\b", "ouvinte"),
            (r"\b(?:The\s+News|the\s+news)\b", "programa"),
            (r"\bnewsletters\b", "edições"),
            (r"\bnewsletter\b", "edição"),
        ]
    novas = []
    for linha in linhas:
        for padrao, troca in pares:
            if re.search(padrao, linha):
                for m in re.finditer(padrao, linha):
                    par = f"'{m.group(0)}' → '{troca}'"
                    if par not in exemplos:
                        exemplos.append(par)
                linha, n = re.subn(padrao, troca, linha)
                trocas += n
        novas.append(linha)
    return novas, trocas, exemplos


def reorganizar_bloco_final(md, idioma="pt"):
    """Garante a ordem notícias → Voz dos Ouvintes → Enquete do Dia e padroniza os títulos.

    Devolve (md, info) com: mudou_ordem, voz (qtde de seções unificadas), trocas_ouvintes,
    enquete_renomeada, opcoes_enquete, perguntas_enquete e detalhes para o relatório.
    """
    preambulo, secoes = dividir_secoes(md)
    voz = [s for s in secoes if _eh_voz(s)]
    enquetes = [s for s in secoes if _eh_enquete(s)]
    corpo = [s for s in secoes if _eh_secao_noticia(s)]
    idx_ultima_noticia = max((i for i, s in enumerate(secoes) if _eh_secao_noticia(s)), default=-1)
    primeiro_voz = next((i for i, s in enumerate(secoes) if _eh_voz(s)), len(secoes))
    fora_de_ordem = [
        s["titulo"]
        for i, s in enumerate(secoes)
        if (not _eh_secao_noticia(s))
        and (i < idx_ultima_noticia or (_eh_enquete(s) and voz and i < primeiro_voz))
    ]
    info = {
        "mudou_ordem": False,
        "voz": len(voz),
        "titulos_voz": [s["titulo"] for s in voz],
        "trocas_ouvintes": 0,
        "exemplos_trocas": [],
        "enquete_renomeada": False,
        "enquete_duplicada": len(enquetes) > 1,
        "titulo_enquete_original": enquetes[0]["titulo"] if enquetes else "",
        "opcoes_enquete": 0,
        "perguntas_enquete": 0,
        "tem_enquete": bool(enquetes),
        "fora_de_ordem": fora_de_ordem,
    }

    nova_ordem = corpo + voz + enquetes
    info["mudou_ordem"] = [id(s) for s in nova_ordem] != [id(s) for s in secoes]

    bloco_voz = []
    if voz:
        canon = TITULO_VOZ[idioma]
        linhas_voz = [f"## {canon}", ""]
        for s in voz:
            corpo_s = _sem_rabo(s["linhas"][1:]) if len(s["linhas"]) > 1 else []
            if s["titulo"].lower() != canon.lower():
                linhas_voz += [f"### {s['titulo']}", ""]
            linhas_voz += [l for l in corpo_s] + [""]
        linhas_voz, info["trocas_ouvintes"], info["exemplos_trocas"] = _adaptar_ouvintes(linhas_voz, idioma)
        bloco_voz = [{"titulo": canon, "linhas": _sem_rabo(linhas_voz)}]

    bloco_enquete = []
    if enquetes:
        canon = TITULO_ENQUETE[idioma]
        principal = enquetes[0]
        info["enquete_renomeada"] = principal["titulo"].lower() != canon.lower()
        linhas_combinadas = []
        for idx_e, s in enumerate(enquetes):
            norm = _sem_rabo(_normalizar_linhas_enquete(s["linhas"][1:]))
            if idx_e > 0:
                linhas_combinadas.append("")
            linhas_combinadas.extend(norm)
        if len(enquetes) > 1:
            escolhido = _escolher_bloco_enquete(_extrair_blocos_enquete(linhas_combinadas))
            if escolhido and escolhido[1]:
                p_txt, ops_txt = escolhido
                linhas = [f"## {canon}"] + ([""] + p_txt if p_txt else []) + ["", *ops_txt]
            else:
                linhas = [f"## {canon}"] + linhas_combinadas
        else:
            linhas = [f"## {canon}"] + linhas_combinadas
        linhas = _sem_rabo(linhas)
        info["opcoes_enquete"] = sum(1 for l in linhas[1:] if _RE_BULLET.match(l))
        info["perguntas_enquete"] = _contar_blocos_de_opcoes(linhas[1:])
        info["pergunta_enquete"] = _extrair_pergunta_enquete("\n".join(linhas))
        bloco_enquete = [{"titulo": canon, "linhas": linhas}]

    if corpo and (voz or enquetes):
        corpo[-1]["linhas"] = _sem_rabo(corpo[-1]["linhas"])
    return juntar_secoes(preambulo, corpo + bloco_voz + bloco_enquete), info


def _indent_len(espacos):
    return len(espacos.expandtabs(4))


def _normalizar_linhas_enquete(linhas):
    """Converte pergunta em bullet seguida de sub-bullets indentados em parágrafo normal + opções nível 0."""
    saida, i, total = [], 0, len(linhas)
    while i < total:
        linha = linhas[i]
        m = _RE_BULLET_CAPTURA.match(linha)
        if m:
            ind_pai = _indent_len(m.group(1))
            j = i + 1
            while j < total and not linhas[j].strip():
                j += 1
            m_sub = _RE_BULLET_CAPTURA.match(linhas[j]) if j < total else None
            if m_sub and _indent_len(m_sub.group(1)) > ind_pai:
                saida.append(m.group(2).strip())
                if i + 1 < total and linhas[i + 1].strip():
                    saida.append("")
                k = i + 1
                while k < total:
                    lk = linhas[k]
                    if not lk.strip():
                        p = k + 1
                        while p < total and not linhas[p].strip():
                            p += 1
                        mp = _RE_BULLET_CAPTURA.match(linhas[p]) if p < total else None
                        if mp and _indent_len(mp.group(1)) > ind_pai:
                            saida.append("")
                            k += 1
                            continue
                        break
                    mk = _RE_BULLET_CAPTURA.match(lk)
                    if mk and _indent_len(mk.group(1)) > ind_pai:
                        saida.append(re.sub(r"^[ \t]+", "", lk).rstrip())
                        k += 1
                        continue
                    break
                i = k
                continue
        saida.append(linha)
        i += 1
    return saida


def _extrair_blocos_enquete(linhas):
    """Separa pares `(linhas_pergunta, linhas_opcoes)` dentro de uma seção de enquete."""
    blocos, pergunta, opcoes = [], [], []
    for linha in linhas:
        if _RE_BULLET.match(linha):
            opcoes.append(linha)
        elif opcoes:
            blocos.append((pergunta, opcoes))
            pergunta = [linha] if linha.strip() else []
            opcoes = []
        elif linha.strip():
            pergunta.append(linha)
    if opcoes or pergunta:
        blocos.append((pergunta, opcoes))
    return blocos


def _escolher_bloco_enquete(blocos):
    """Prefere enquete com 4+ opções quando houver alternativa menor (<= 3), senão a 1ª completa (>= 2)."""
    validos = [b for b in blocos if len(b[1]) >= 2]
    if not validos:
        return blocos[0] if blocos else None
    completos4 = [b for b in validos if len(b[1]) >= 4]
    return completos4[0] if completos4 else validos[0]


def _contar_blocos_de_opcoes(linhas):
    blocos, dentro = 0, False
    for linha in linhas:
        if _RE_BULLET.match(linha):
            if not dentro:
                blocos += 1
            dentro = True
        elif linha.strip():
            dentro = False
    return blocos


# --- Enquete -----------------------------------------------------------------

PROMPT_ENQUETE = {
    "pt": (
        "Você é roteirista do podcast ácido e bem-humorado da rede Central do Caos. Com base na pauta "
        "fornecida, crie UMA enquete provocativa e inteligente sobre a notícia mais polêmica, absurda ou "
        "impactante da edição, para os apresentadores debaterem no encerramento.\n"
        "Responda SOMENTE neste formato, sem nenhum texto antes ou depois:\n\n"
        "## Enquete do Dia\n\n<pergunta curta terminando com ?>\n\n- <opção 1>\n- <opção 2>\n- <opção 3>\n- <opção 4>\n\n"
        "As 4 opções devem ser curtas (até 12 palavras), contrastantes entre si e afiadas, sem palavrões."
    ),
    "en": (
        "You are a writer for a sharp, funny podcast on the Central do Caos network. Based on the rundown "
        "provided, create ONE provocative, smart poll about the most controversial, absurd or impactful story "
        "of the edition, for the hosts to debate at the end of the episode.\n"
        "Reply ONLY in this format, with no text before or after:\n\n"
        "## Daily Poll\n\n<short question ending with ?>\n\n- <option 1>\n- <option 2>\n- <option 3>\n- <option 4>\n\n"
        "The 4 options must be short (up to 12 words), contrasting and witty, with no profanity."
    ),
}


def _contexto_para_enquete(md, limite=3500):
    _, secoes = dividir_secoes(md)
    partes = []
    for s in secoes:
        if not _eh_secao_noticia(s):
            continue
        corpo = " ".join(l.strip() for l in s["linhas"][1:] if l.strip())
        partes.append(f"## {s['titulo']}\n{corpo[:400]}")
    texto = "\n\n".join(partes)
    return texto[:limite] or md[:limite]


def _montar_bloco_enquete(resposta, idioma):
    """Valida a resposta da IA e devolve o bloco canônico da enquete (ou None se vier fora do formato)."""
    if not resposta:
        return None
    linhas_raw = [l.rstrip() for l in resposta.strip().strip("`").split("\n")]
    linhas = [l.strip() for l in _normalizar_linhas_enquete(linhas_raw) if l.strip()]
    blocos_validos = []
    for p_linhas, op_linhas in _extrair_blocos_enquete(linhas):
        pergunta = next((l for l in p_linhas if l and not l.startswith("#") and "?" in l), None)
        opcoes = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", l) for l in op_linhas]
        if pergunta and len(opcoes) >= 3:
            blocos_validos.append((pergunta, opcoes[:4]))
    if not blocos_validos:
        return None
    escolhido = next((b for b in blocos_validos if len(b[1]) >= 4), blocos_validos[0])
    pergunta, opcoes = escolhido
    return "\n".join([f"## {TITULO_ENQUETE[idioma]}", "", pergunta, ""] + [f"- {o}" for o in opcoes])


def enquete_de_fallback(md, idioma="pt"):
    """Enquete determinística com as manchetes do dia (usada se a IA não responder)."""
    _, secoes = dividir_secoes(md)
    titulos = [s["titulo"] for s in secoes if _eh_secao_noticia(s) and not s["titulo"].upper().startswith("RESULTADOS")]
    opcoes = titulos[:4]
    if len(opcoes) < 2:
        return None
    if idioma == "en":
        pergunta = "Which story from today's rundown deserves the biggest facepalm?"
    else:
        pergunta = "Qual notícia de hoje mais merecia um troféu de vergonha alheia?"
    return "\n".join([f"## {TITULO_ENQUETE[idioma]}", "", pergunta, ""] + [f"- {o}" for o in opcoes])


# --- Verificações e correção automática --------------------------------------

_RE_BLOCO_PATROCINADO_FONTE = re.compile(
    r"^#{1,5}[ \t]*(?:DROPPED[ \t]+BY|SPONSORED[ \t]+BY|CAMPANHA[ \t]+FOMO[ \t]+ZERO)\b.*?(?=\n#{5}[ \t]+\S|\Z)",
    re.S | re.I | re.M,
)


def _filtrar_para_integridade(texto, eh_entrada=False):
    """Remove metadados de e-mail, URLs, timestamps e caixas 'Leia também' antes de contar números."""
    texto = _RE_META_BLOCO.sub(" ", texto or "")
    texto = _RE_TAGS_DOC.sub(" ", texto)
    texto = _RE_URL_TEXTO.sub(" ", texto)
    texto = _RE_IMG_DESC.sub(" ", texto)
    if eh_entrada:
        texto = _RE_BLOCO_PATROCINADO_FONTE.sub(" ", texto)
        texto = _RE_LEIA_TAMBEM.sub(" ", texto)
        texto, _ = remover_ruido_residual(texto)
    texto = _RE_GERADO_EM.sub(" ", texto)
    texto = _RE_TEMPO_LEITURA.sub(" ", texto)
    texto = _RE_HORA.sub(" ", texto)
    texto = _RE_DATA_NUM.sub(" ", texto)
    return texto


def _extensos_no_texto(texto):
    """Reconhece números por extenso (dez..cem / compostos 21..99) presentes no texto."""
    achados = set()
    baixo = (texto or "").lower()
    for palavra in re.findall(r"\b[a-záéíóúâêôãõ]+\b", baixo):
        if palavra in _CARDINAIS_EXT:
            achados.add(str(_CARDINAIS_EXT[palavra]))
    for dez, uni in re.findall(
        r"\b(" + "|".join(_DEZENAS_EXT) + r")\s*(?:e\s+|-)\s*(" + "|".join(_UNIDADES_EXT) + r")\b",
        baixo,
    ):
        achados.add(str(_DEZENAS_EXT[dez] + _UNIDADES_EXT[uni]))
    return achados


def _numeros(texto):
    texto = _RE_URL_TEXTO.sub(" ", texto or "")
    mapa = {}
    for bruto in _RE_NUMERO.findall(texto):
        normal = re.sub(r"[.,\u00a0\u202f]", "", bruto).lstrip("0")
        if len(normal) >= 2:
            mapa.setdefault(normal, bruto)
    return mapa


def _analisar_integridade(entrada, saida):
    """Calcula estatísticas limpas de preservação de números e tamanho entre entrada e saída."""
    entrada_limpa = _filtrar_para_integridade(entrada, eh_entrada=True)
    saida_limpa = _filtrar_para_integridade(saida, eh_entrada=False)
    nums_in = _numeros(entrada_limpa)
    nums_out = set(_numeros(saida_limpa)) | _extensos_no_texto(saida_limpa)
    proporcao = (len(saida_limpa) * 100 // len(entrada_limpa)) if entrada_limpa.strip() else 0
    if not nums_in:
        return 100, [], {}, proporcao, entrada_limpa
    presentes = [n for n in nums_in if n in nums_out]
    pct = len(presentes) * 100 // len(nums_in)
    faltantes = {n: nums_in[n] for n in nums_in if n not in nums_out}
    return pct, presentes, faltantes, proporcao, entrada_limpa


def checar_integridade(entrada, saida):
    """(status, detalhe): % de números editoriais da fonte preservados no Markdown e proporção."""
    pct, presentes, faltantes, proporcao, entrada_limpa = _analisar_integridade(entrada, saida)
    total_in = len(presentes) + len(faltantes)
    if total_in < 5:
        return OK, f"poucos números na fonte para comparar · saída com {proporcao}% do tamanho da entrada limpa"
    detalhe = f"{pct}% dos números da fonte preservados ({len(presentes)}/{total_in}) · saída com {proporcao}% do tamanho"
    if pct >= 80:
        return OK, detalhe
    sufixo = f" · ausentes ex.: {', '.join(list(faltantes.values())[:4])}" if faltantes else ""
    if pct >= 60:
        return AVISO, detalhe + " (pode ser publicidade removida)" + sufixo
    return AVISO, detalhe + " — possível resumo/corte, vale revisar" + sufixo


def _formatar_data(dt, idioma="pt"):
    if not hasattr(dt, "month"):
        return ""
    if idioma == "en":
        return f"{_MESES_EN[dt.month]} {dt.day}, {dt.year}"
    return f"{dt.day} de {_MESES_PT[dt.month]} de {dt.year}"


def _corrigir_cabecalho(md, entrada="", idioma="pt", extrair_data=None):
    """Garante H1 único na linha 1 com data reconhecível e detalha o que foi encontrado e corrigido."""
    achados, ajustes = [], []
    md = md.strip()

    def data_de(txt):
        if not extrair_data or not txt:
            return ""
        return _formatar_data(extrair_data(txt), idioma)

    linhas = md.split("\n")
    primeira = linhas[0].strip() if linhas else ""
    if not primeira.startswith("# "):
        dt_str = data_de(entrada) or data_de(md)
        if dt_str:
            pre_desc = f"'{primeira[:40]}'" if primeira else "início vazio"
            achados.append(f"linha 1 sem '# Data' ({pre_desc})")
            md = f"# {dt_str}\n\n{md}" if md else f"# {dt_str}"
            ajustes.append(f"inserido '# {dt_str}' no topo")
    else:
        data_ok = extrair_data(primeira) if extrair_data else True
        if not data_ok:
            dt_str = data_de(entrada) or data_de("\n".join(linhas[1:]))
            if dt_str:
                titulo_atual = primeira.lstrip("# ").strip()
                achados.append(f"título '{primeira[:50]}' sem data reconhecível")
                if not titulo_atual or re.fullmatch(r"(?:sem\s+data|edi[cç][aã]o(?:\s+de\s+hoje)?)", titulo_atual, re.I):
                    linhas[0] = f"# {dt_str}"
                else:
                    linhas[0] = f"# {titulo_atual} — {dt_str}"
                md = "\n".join(linhas)
                ajustes.append(f"ajustado para '{linhas[0]}'")

    linhas = md.split("\n")
    novas, h1_extras = [], []
    viu_h1 = False
    for linha in linhas:
        if re.match(r"^#\s+", linha):
            if not viu_h1:
                viu_h1 = True
                novas.append(linha)
            else:
                h1_extras.append(linha.strip()[:50])
                novas.append("#" + linha)
        else:
            novas.append(linha)
    if h1_extras:
        achados.append(f"{len(h1_extras)} título(s) '#' extra(s) no corpo ({_amostra(h1_extras, 2)})")
        ajustes.append("rebaixado(s) para '##'")
    md = "\n".join(novas).strip()

    linhas_fin = md.split("\n")
    primeira_fin = linhas_fin[0].strip() if linhas_fin else ""
    if not primeira_fin.startswith("# "):
        enc = f"linha 1 não é um título '#' com a data da edição ('{primeira_fin[:40]}')"
        return md, AVISO, "", enc, ""
    if extrair_data and not extrair_data(primeira_fin):
        enc = f"sem data reconhecível em '{primeira_fin[:60]}'"
        return md, AVISO, "", enc, ""
    if achados:
        return md, CORRIGIDO, "", "; ".join(achados), "; ".join(ajustes)
    return md, OK, primeira_fin.lstrip("# ").strip(), "", ""


def _remover_ruido_detalhado(md):
    """Remove seções comerciais, sufixos de patrocinador, linhas de CTA/sorteio e trechos embutidos seguros."""
    removidos = 0
    amostras = []
    linhas_filtradas = []
    nivel_ignorado = None

    for linha in md.split("\n"):
        m_cab = _RE_CABECALHO_SEC.match(linha)
        if m_cab:
            nivel = len(m_cab.group(1))
            titulo_limpo = re.sub(r"[*_`]+", "", m_cab.group(2)).strip()
            if nivel_ignorado is not None:
                if nivel <= nivel_ignorado:
                    nivel_ignorado = None
                else:
                    continue
            if nivel in (2, 3) and _RE_SECAO_RUIDO.match(titulo_limpo):
                nivel_ignorado = nivel
                removidos += 1
                amostras.append(f"seção '{titulo_limpo[:45]}'")
                continue
        elif nivel_ignorado is not None:
            continue
        linhas_filtradas.append(linha)

    novas = []
    em_clickbait = False
    for linha in linhas_filtradas:
        m_cab = _RE_CABECALHO_SEC.match(linha)
        if m_cab:
            nivel_c = len(m_cab.group(1))
            tit_c = re.sub(r"[*_`]+", "", m_cab.group(2)).strip()
            em_clickbait = nivel_c in (2, 3) and bool(_RE_SECAO_CLICKBAIT.match(tit_c))
        m_pp = _RE_TITULO_PEAK_PICKS.match(linha)
        if m_pp:
            removidos += 1
            amostras.append("título 'Peak Picks' → 'Quick Picks'")
            linha = f"{m_pp.group(1)}Quick Picks"
        m_pat = _RE_TITULO_PATROCINADOR.match(linha)
        if m_pat:
            removidos += 1
            amostras.append(linha.strip()[:60])
            linha = m_pat.group(1).rstrip(" :-–—")
        if _RE_RUIDO_LINHA.match(linha) or (em_clickbait and _RE_CLICKBAIT_LINHA_CI.match(linha)) or _RE_CLICKBAIT_LINHA.match(linha):
            removidos += 1
            amostras.append(linha.strip()[:60])
            continue
        matches_inline = [m.group(0).strip()[:60] for m in _RE_RUIDO_INLINE.finditer(linha)]
        if matches_inline:
            amostras.extend(matches_inline)
        nova, n = _RE_RUIDO_INLINE.subn("", linha)
        if n:
            removidos += n
            nova = nova.rstrip()
            if not nova:
                continue
        novas.append(nova)

    limpas = []
    i = 0
    while i < len(novas):
        linha = novas[i]
        m_cab = _RE_CABECALHO_SEC.match(linha)
        if m_cab and len(m_cab.group(1)) in (2, 3):
            tit_c = re.sub(r"[*_`]+", "", m_cab.group(2)).strip()
            if _RE_SECAO_CLICKBAIT.match(tit_c):
                j = i + 1
                while j < len(novas) and not novas[j].strip():
                    j += 1
                if j >= len(novas) or _RE_CABECALHO_SEC.match(novas[j]):
                    removidos += 1
                    amostras.append(f"seção '{tit_c[:45]}'")
                    i = j
                    continue
        limpas.append(linha)
        i += 1
    return "\n".join(limpas), {"removidos": removidos, "amostras": amostras}


def remover_ruido_residual(md):
    """Remove linhas de anúncio/paywall/CTA residuais e CTAs embutidos seguros. Devolve (md, removidos)."""
    novo_md, info = _remover_ruido_detalhado(md)
    return novo_md, info["removidos"]


def _promover_subtitulos_se_sem_noticias(md):
    """Se a IA gerou ### em vez de ## para as notícias, promove para ## e retorna os títulos ajustados."""
    if contar_noticias(md) > 0:
        return md, []
    promovidos = []
    novas = []
    for linha in md.split("\n"):
        m = re.match(r"^###\s+(.+?)\s*$", linha)
        if m and not _RE_ENQUETE.match(m.group(1)) and not _RE_VOZ.match(m.group(1)):
            titulo = m.group(1).strip()
            novas.append(f"## {titulo}")
            promovidos.append(titulo)
        else:
            novas.append(linha)
    return "\n".join(novas), promovidos


def _diagnostico_idioma(md, idioma):
    """Retorna (tem_problema, detalhe) indicando mistura de idiomas no Markdown."""
    if idioma == "en":
        titulos_pt = _RE_TITULO_PT_EM_EN.findall(md)
        palavras_pt = len(_RE_PALAVRAS_PT.findall(md))
        if titulos_pt or palavras_pt >= 3:
            return True, f"{len(titulos_pt)} título(s) e {palavras_pt} palavra(s) em português"
        return False, "sem trechos em português"
    mistos = sorted({
        m.group(0)
        for m in _RE_CODE_SWITCH.finditer(md)
        if m.group(1).lower() not in _ESTRANGEIRISMOS_PT
    })
    if mistos:
        return True, "possível code-switching: " + ", ".join(f"'{x}'" for x in mistos[:3])
    return False, "português consistente"


def _secoes_com_problema_idioma(md, idioma):
    _, secoes = dividir_secoes(md)
    afetadas = []
    for s in secoes:
        prob, det = _diagnostico_idioma("\n".join(s["linhas"]), idioma)
        if prob:
            afetadas.append((s["titulo"], det))
    return afetadas


def _higienizar_candidato(md, idioma):
    """Reaplica limpezas determinísticas sobre textos devolvidos por uma etapa de reparo da IA."""
    md, _ = remover_links_e_imagens(md)
    md, _ = converter_tabelas_em_topicos(md, idioma)
    md, _ = remover_ruido_residual(md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _indice_secao_correspondente(titulo, secoes, indice_norm=None):
    """Encontra o índice da seção em `secoes` por título normalizado exato ou similaridade (>= 0.65)."""
    chave = _norm_titulo(titulo)
    if indice_norm is None:
        indice_norm = {_norm_titulo(s["titulo"]): i for i, s in enumerate(secoes)}
    idx = indice_norm.get(chave)
    if idx is not None:
        return idx
    palavras = set(chave.split())
    if not palavras or not secoes:
        return None
    melhor = max(
        range(len(secoes)),
        key=lambda i: len(palavras & set(_norm_titulo(secoes[i]["titulo"]).split())) / len(palavras),
    )
    comum = len(palavras & set(_norm_titulo(secoes[melhor]["titulo"]).split())) / len(palavras)
    return melhor if comum >= 0.65 else None


def _trocas_idioma_concisas(linhas_antes, linhas_depois, idioma):
    """Identifica substituições curtas de frase/termo feitas pela IA nas linhas com mistura de idioma."""
    trocas = []
    for i, l_a in enumerate(linhas_antes):
        if idioma == "en":
            tem_misto = bool(_RE_TITULO_PT_EM_EN.search(l_a) or _RE_PALAVRAS_PT.search(l_a))
        else:
            tem_misto = _diagnostico_idioma(l_a, "pt")[0]
        if not tem_misto:
            continue
        l_d = linhas_depois[i] if i < len(linhas_depois) else max(
            linhas_depois,
            key=lambda x: SequenceMatcher(None, l_a, x).ratio(),
            default="",
        )
        if not l_d or l_a == l_d:
            continue
        palavras_a, palavras_d = l_a.split(), l_d.split()
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, palavras_a, palavras_d).get_opcodes():
            if tag == "replace" and 1 <= (i2 - i1) <= 4 and 1 <= (j2 - j1) <= 4:
                ant = " ".join(palavras_a[i1:i2])
                dep = " ".join(palavras_d[j1:j2])
                trocas.append(f"'{ant}' → '{dep}'")
    return trocas


def _resumir_reparo_idioma(md_antes, md_depois, idioma, afetadas):
    _, secs_antes = dividir_secoes(md_antes)
    _, secs_depois = dividir_secoes(md_depois)
    idx_depois = {_norm_titulo(s["titulo"]): i for i, s in enumerate(secs_depois)}
    titulos_afetados = {t for t, _ in afetadas}
    trocas = []
    for s_a in secs_antes:
        if s_a["titulo"] not in titulos_afetados:
            continue
        idx_d = _indice_secao_correspondente(s_a["titulo"], secs_depois, idx_depois)
        if idx_d is not None:
            trocas.extend(_trocas_idioma_concisas(s_a["linhas"], secs_depois[idx_d]["linhas"], idioma))

    secoes_str = _amostra([t for t, _ in afetadas], 2)
    onde = f" na(s) seção(ões) {secoes_str}" if secoes_str else ""
    if trocas:
        return f"corrigido com IA{onde}: {_amostra(trocas, 3, aspas=False)}"
    return f"reescrita com IA{onde} (eliminado: {_amostra([d for _, d in afetadas], 2, aspas=False)})"


def _reparar_idioma_com_ia(md, idioma, reparar):
    """Usa IA para eliminar mistura de idiomas nas seções afetadas sem perder fatos ou números."""
    pre, secoes = dividir_secoes(md)
    afetadas = [s for s in secoes if _diagnostico_idioma("\n".join(s["linhas"]), idioma)[0]]
    afetadas_info = [(s["titulo"], _diagnostico_idioma("\n".join(s["linhas"]), idioma)[1]) for s in afetadas]
    if not afetadas or len(md) <= 8500:
        alvo = md
        parcial = False
    else:
        alvo = "\n\n".join("\n".join(s["linhas"]) for s in afetadas)
        parcial = True

    if idioma == "en":
        prompt = (
            "Rewrite ONLY the leftover Portuguese headings, labels, or phrases in the Markdown below into natural, "
            "idiomatic English. Preserve 100% of facts, numbers, bullet formatting, and heading hierarchy (##). "
            "Return ONLY the corrected Markdown with no commentary."
        )
    else:
        prompt = (
            "Corrija APENAS a mistura de idiomas no Markdown abaixo (verbos ou palavras em inglês no meio de frases "
            "em português, traduzindo para português natural). Mantenha 100% dos fatos, números, formatação e títulos ##. "
            "Responda SOMENTE com o Markdown corrigido, sem explicações."
        )
    try:
        cand = _higienizar_candidato(reparar(alvo, prompt), idioma)
    except Exception:
        return md, False, ""
    if not cand:
        return md, False, ""

    if parcial:
        _, secs_rep = dividir_secoes(cand)
        mapa = {_norm_titulo(s["titulo"]): s for s in secs_rep}
        novas = [mapa.get(_norm_titulo(s["titulo"]), s) if s in afetadas else s for s in secoes]
        cand = juntar_secoes(pre, novas)

    prob, _ = _diagnostico_idioma(cand, idioma)
    if prob or contar_noticias(cand) < contar_noticias(md):
        return md, False, ""
    if len(_numeros(cand)) < max(0, len(_numeros(md)) - 1):
        return md, False, ""
    return cand, True, _resumir_reparo_idioma(md, cand, idioma, afetadas_info)


def _norm_titulo(titulo):
    return re.sub(r"\W+", " ", (titulo or "").lower()).strip()


def _trechos_para_integridade(entrada_limpa, faltantes, limite=7500):
    """Seleciona da entrada apenas seções/blocos que contêm os números ausentes."""
    chaves = set(faltantes)
    blocos = [b.strip() for b in re.split(r"\n{2,}", entrada_limpa) if b.strip()]
    escolhidos, tamanho = [], 0
    for bloco in blocos:
        if any(n in chaves for n in _numeros(bloco)):
            if tamanho + len(bloco) > limite and escolhidos:
                break
            escolhidos.append(bloco)
            tamanho += len(bloco) + 2
    return "\n\n".join(escolhidos)


def _mesclar_secoes_recuperadas(md, recuperado, full_doc):
    """Mescla seções restauradas pela IA no documento sem duplicar notícias nem apagar o bloco final."""
    pre_orig, secs_orig = dividir_secoes(md)
    corpos = [s for s in secs_orig if _eh_secao_noticia(s)]
    finais = [s for s in secs_orig if not _eh_secao_noticia(s)]

    pre_rec, secs_rec = dividir_secoes(recuperado)
    if full_doc and pre_rec and pre_rec[0].strip().startswith("# "):
        pre = pre_rec
        corpos_rec = [s for s in secs_rec if _eh_secao_noticia(s)]
        finais_rec = [s for s in secs_rec if not _eh_secao_noticia(s)]
        return juntar_secoes(pre, (corpos_rec or corpos) + (finais_rec or finais))

    indice_corpos = {_norm_titulo(s["titulo"]): i for i, s in enumerate(corpos)}
    for s_rec in secs_rec:
        if not _eh_secao_noticia(s_rec):
            continue
        chave = _norm_titulo(s_rec["titulo"])
        idx = _indice_secao_correspondente(s_rec["titulo"], corpos, indice_corpos)
        if idx is not None:
            txt_rec = "\n".join(s_rec["linhas"])
            txt_old = "\n".join(corpos[idx]["linhas"])
            if len(_numeros(txt_rec)) >= len(_numeros(txt_old)) and len(txt_rec) >= len(txt_old) * 0.85:
                corpos[idx] = s_rec
        else:
            indice_corpos[chave] = len(corpos)
            corpos.append(s_rec)
    return juntar_secoes(pre_orig, corpos + finais)


def _resumir_recuperacao_integridade(md_antes, md_depois, entrada, pct_antes, faltantes_antes):
    pct_depois, presentes_depois, faltantes_depois, _, _ = _analisar_integridade(entrada, md_depois)
    total_in = len(presentes_depois) + len(faltantes_depois)
    nums_recuperados = [bruto for norm, bruto in faltantes_antes.items() if norm not in faltantes_depois]

    corpos_antes = [s for s in dividir_secoes(md_antes)[1] if _eh_secao_noticia(s)]
    corpos_depois = [s for s in dividir_secoes(md_depois)[1] if _eh_secao_noticia(s)]
    idx_antes = {_norm_titulo(s["titulo"]): i for i, s in enumerate(corpos_antes)}
    novas_secoes, completadas = [], []
    for s_dep in corpos_depois:
        idx = _indice_secao_correspondente(s_dep["titulo"], corpos_antes, idx_antes)
        if idx is None:
            novas_secoes.append(s_dep["titulo"])
            continue
        txt_a = "\n".join(corpos_antes[idx]["linhas"])
        txt_d = "\n".join(s_dep["linhas"])
        if (set(_numeros(txt_d)) - set(_numeros(txt_a))) or len(txt_d) > len(txt_a) + 20:
            completadas.append(s_dep["titulo"])

    partes = [f"preservação subiu de {pct_antes}% para {pct_depois}% ({len(presentes_depois)}/{total_in})"]
    if nums_recuperados:
        partes.append(f"+{len(nums_recuperados)} número(s) restaurado(s) ({_amostra(nums_recuperados, 6, aspas=False)})")
    if novas_secoes:
        partes.append(f"seção(ões) adicionada(s): {_amostra(novas_secoes, 2)}")
    if completadas:
        partes.append(f"completada(s): {_amostra(completadas, 2)}")
    return " · ".join(partes), faltantes_depois


def _reparar_integridade_com_ia(md, entrada, idioma, reparar):
    """Tenta recuperar com IA notícias/números omitidos da fonte e detalha o que foi restaurado."""
    pct_antes, _, faltantes, _, entrada_limpa = _analisar_integridade(entrada, md)
    trechos = _trechos_para_integridade(entrada_limpa, faltantes)
    if not trechos:
        return md, pct_antes, pct_antes, False, "", faltantes

    lista_ausentes = ", ".join(list(faltantes.values())[:10])
    full_doc = len(md) <= 8500
    if full_doc:
        instrucao = (
            "A dupla verificação detectou possível omissão de dados/números da fonte original "
            f"(ausentes: {lista_ausentes}). Compare o RASCUNHO ATUAL com os TRECHOS DA FONTE e devolva o "
            "Markdown COMPLETO restaurando apenas os fatos e números editoriais faltantes, mantendo o formato limpo "
            "de roteiro (título #, seções ##, tópicos em vez de tabelas, sem URLs e preservando Enquete do Dia/Daily Poll ao final). "
            "NUNCA restaure anúncios, patrocínios, taxas promocionais de marcas (ex.: ARQ, investback), sorteios de iPhone, "
            "planos de assinatura (ex.: Peak Premium), promoções/guia de compras (ex.: Black Friday), mídia kit ou endereços postais."
        )
        conteudo = f"<rascunho_atual>\n{md}\n</rascunho_atual>\n\n<trechos_fonte>\n{trechos}\n</trechos_fonte>"
    else:
        titulos_atuais = "\n".join(f"- {s['titulo']}" for s in dividir_secoes(md)[1] if _eh_secao_noticia(s))
        instrucao = (
            "A dupla verificação detectou possível omissão de dados/números da fonte original "
            f"(ausentes: {lista_ausentes}). Com base nos TRECHOS DA FONTE, escreva SOMENTE as seções ## completas "
            "que contêm esses números editoriais omitidos (mantendo o mesmo título ## exato se já existir na pauta para "
            "completá-la, ou criando a seção ## omitida), em formato limpo para podcast (sem URLs nem tabelas). "
            "NUNCA restaure anúncios, patrocínios, taxas promocionais de marcas (ex.: ARQ, investback), sorteios de iPhone, "
            "planos de assinatura (ex.: Peak Premium), promoções/guia de compras (ex.: Black Friday), mídia kit ou endereços postais."
        )
        conteudo = (
            f"<titulos_ja_extraidos>\n{titulos_atuais}\n</titulos_ja_extraidos>\n\n"
            f"<trechos_fonte>\n{trechos}\n</trechos_fonte>"
        )

    try:
        resposta = reparar(conteudo, instrucao)
    except Exception:
        return md, pct_antes, pct_antes, False, "", faltantes
    if not resposta:
        return md, pct_antes, pct_antes, False, "", faltantes

    resposta = _higienizar_candidato(resposta, idioma)
    cand = _mesclar_secoes_recuperadas(md, resposta, full_doc=full_doc)
    cand = _higienizar_candidato(cand, idioma)
    if contar_noticias(cand) < contar_noticias(md) or len(cand) < len(md) * 0.85:
        return md, pct_antes, pct_antes, False, "", faltantes

    pct_depois, _, _, _, _ = _analisar_integridade(entrada, cand)
    if pct_depois <= pct_antes:
        return md, pct_antes, pct_antes, False, "", faltantes

    det_rec, faltantes_depois = _resumir_recuperacao_integridade(md, cand, entrada, pct_antes, faltantes)
    return cand, pct_antes, pct_depois, True, det_rec, faltantes_depois


def _extrair_pergunta_enquete(bloco):
    for linha in (bloco or "").split("\n"):
        l = linha.strip()
        if l and not l.startswith("#") and not _RE_BULLET.match(l):
            return l[:100]
    return ""


def _limpar_enquete_multipla(md, idioma="pt"):
    """Mantém apenas uma enquete válida (com normalização e preferência por 4 opções) dentro do bloco Enquete do Dia."""
    pre, secoes = dividir_secoes(md)
    canon = TITULO_ENQUETE[idioma]
    alterou = False
    pergunta_mantida = ""
    for s in secoes:
        if not _eh_enquete(s):
            continue
        blocos = _extrair_blocos_enquete(_normalizar_linhas_enquete(s["linhas"][1:]))
        escolhido = _escolher_bloco_enquete(blocos)
        if escolhido and escolhido[1]:
            p_txt = escolhido[0]
            pergunta_mantida = " ".join(l.strip() for l in p_txt if l.strip())[:100]
            s["linhas"] = [f"## {canon}"] + ([""] + p_txt if p_txt else []) + ["", *escolhido[1]]
            alterou = True
    return juntar_secoes(pre, secoes), alterou, pergunta_mantida


def _substituir_enquete_incompleta(md, idioma, gerar=None):
    """Substitui um bloco de enquete quebrado (< 2 opções) por uma enquete válida (IA ou fallback)."""
    pre, secoes = dividir_secoes(md)
    sem_enq = juntar_secoes(pre, [s for s in secoes if not _eh_enquete(s)])
    bloco, origem, usou_ia = None, "", False
    if gerar:
        try:
            bloco = _montar_bloco_enquete(gerar(_contexto_para_enquete(sem_enq), PROMPT_ENQUETE[idioma]), idioma)
            if bloco:
                origem = "recriada pela IA a partir da pauta"
                usou_ia = True
        except Exception:
            bloco = None
    if not bloco:
        bloco = enquete_de_fallback(sem_enq, idioma)
        origem = "recriada com as manchetes do dia (IA indisponível)"
    if not bloco:
        return md, None, "", False
    return (sem_enq.rstrip() + "\n\n" + bloco).strip(), bloco, origem, usou_ia


def aplicar_dupla_verificacao(md, entrada="", idioma="pt", gerar=None, extrair_data=None, reparar=None, notificar=None):
    """Executa todas as regras sobre o Markdown final. Devolve (md_corrigido, RelatorioValidacao).

    - `gerar(conteudo, prompt) -> str`: função de IA para criar/recriar a enquete quando faltar (opcional).
    - `extrair_data(texto) -> date|None`: parser de data para conferir/reparar o cabeçalho (opcional).
    - `reparar(conteudo, prompt) -> str`: função de IA para corrigir mistura de idioma ou omissões (opcional).
    - `notificar(evento, regra, **dados)`: callback para acompanhar o que foi encontrado e corrigido em tempo real.
    """
    rel = RelatorioValidacao()
    md = (md or "").strip()

    def registrar(regra, status, detalhe="", encontrado="", corrigido="", usou_ia=False):
        rel.add(regra, status, detalhe=detalhe, encontrado=encontrado, corrigido=corrigido)
        if status == CORRIGIDO:
            _emitir(notificar, "corrigido", regra, encontrado=encontrado, corrigido=corrigido, usou_ia=usou_ia)
        elif status in (AVISO, FALHA):
            _emitir(notificar, "aviso", regra, encontrado=encontrado or detalhe, status=status)

    # 1. Cabeçalho de data e H1 único
    md, status_cab, det_cab, enc_cab, corr_cab = _corrigir_cabecalho(md, entrada, idioma, extrair_data)
    registrar("Cabeçalho de data", status_cab, detalhe=det_cab, encontrado=enc_cab, corrigido=corr_cab)

    # 2. Links e imagens (correção segura)
    md, info_links = _remover_links_e_imagens_detalhado(md)
    if info_links["total"]:
        partes_enc, partes_corr = [], []
        if info_links["n_links_md"]:
            partes_enc.append(f"{info_links['n_links_md']} link(s) Markdown ({_amostra(info_links['links_md'], 2)})")
            partes_corr.append("links convertidos em texto puro")
        if info_links["n_imgs"]:
            partes_enc.append(f"{info_links['n_imgs']} imagem(ns) Markdown")
            partes_corr.append("marcações de imagem removidas")
        if info_links["n_urls"]:
            partes_enc.append(f"{info_links['n_urls']} URL(s) crua(s) ({_amostra(info_links['urls_cruas'], 2)})")
            partes_corr.append("URLs cruas apagadas")
        registrar("Links e imagens", CORRIGIDO, encontrado=", ".join(partes_enc), corrigido="; ".join(partes_corr))
    else:
        registrar("Links e imagens", OK, "nenhum link cru no texto")

    # 3. Tabelas → tópicos narrativos
    md, info_tab = _converter_tabelas_detalhado(md, idioma)
    if info_tab["convertidas"]:
        amostra_rot = _amostra(info_tab["rotulos"], 4, aspas=False)
        sufixo_rot = f": {amostra_rot}" if amostra_rot else ""
        registrar(
            "Tabelas → tópicos", CORRIGIDO,
            encontrado=f"{info_tab['convertidas']} tabela(s) Markdown ({info_tab['linhas']} linha(s){sufixo_rot})",
            corrigido=f"convertida(s) em {info_tab['linhas']} tópico(s) narrativo(s) para locução",
        )
    else:
        registrar("Tabelas → tópicos", OK, "nenhuma tabela crua")

    # 4. Ruído residual (remove linhas/CTAs promocionais e recorre à IA se sobrar trecho embutido)
    md, info_ruido = _remover_ruido_detalhado(md)
    n_ruido = info_ruido["removidos"]
    amostras_ruido = list(info_ruido["amostras"])
    ruidos = sorted({m.group(0).strip() for m in _RE_RUIDO.finditer(md)})
    usou_ia_ruido = False
    if ruidos and reparar:
        _emitir(
            notificar, "ia_inicio", "Ruído (anúncio/paywall)",
            encontrado=f"chamada(s) promocional(is)/paywall embutida(s): {_amostra(ruidos, 3)}",
            acao="removendo trechos promocionais com IA...",
            etapa="🔧 removendo ruído com IA",
        )
        try:
            prompt_ruido = (
                "Remova apenas as chamadas promocionais, de assinatura ou paywall residuais do Markdown abaixo "
                "(ex.: " + ", ".join(ruidos[:3]) + "), sem apagar nenhuma notícia, dado numérico ou título ##."
            )
            cand_ruido = _higienizar_candidato(reparar(md, prompt_ruido), idioma)
            if cand_ruido and contar_noticias(cand_ruido) >= contar_noticias(md) and not _RE_RUIDO.search(cand_ruido):
                md = cand_ruido
                n_ruido += len(ruidos)
                amostras_ruido.extend(ruidos)
                ruidos = []
                usou_ia_ruido = True
        except Exception:
            pass
    if ruidos:
        registrar("Ruído (anúncio/paywall)", AVISO, encontrado=f"trechos suspeitos: {_amostra(ruidos, 4)}")
    elif n_ruido:
        registrar(
            "Ruído (anúncio/paywall)", CORRIGIDO,
            encontrado=f"{n_ruido} trecho(s) de publicidade/paywall ({_amostra(amostras_ruido, 3)})",
            corrigido="removido(s) automaticamente" + (" com apoio de IA" if usou_ia_ruido else ""),
            usou_ia=usou_ia_ruido,
        )
    else:
        registrar("Ruído (anúncio/paywall)", OK, "sem publicidade, paywall ou sumários")

    # 5. Promove ### → ## caso nenhuma seção ## de notícia exista ainda
    md, titulos_promovidos = _promover_subtitulos_se_sem_noticias(md)

    # 6. Idioma (corrige com IA quando detecta mistura)
    nome_regra_idioma = "Idioma (100% inglês)" if idioma == "en" else "Idioma (sem inglês misturado)"
    prob_idioma, det_idioma = _diagnostico_idioma(md, idioma)
    if prob_idioma:
        afetadas = _secoes_com_problema_idioma(md, idioma)
        secoes_str = _amostra([t for t, _ in afetadas], 2)
        enc_idioma = det_idioma + (f" na(s) seção(ões) {secoes_str}" if secoes_str else "")
        if reparar:
            _emitir(
                notificar, "ia_inicio", nome_regra_idioma,
                encontrado=enc_idioma,
                acao="corrigindo mistura de idioma com IA...",
                etapa="🔧 corrigindo idioma com IA",
            )
            md, corrigiu_id, corr_idioma = _reparar_idioma_com_ia(md, idioma, reparar)
            if corrigiu_id:
                registrar(nome_regra_idioma, CORRIGIDO, encontrado=enc_idioma, corrigido=corr_idioma, usou_ia=True)
            else:
                registrar(nome_regra_idioma, AVISO, encontrado=enc_idioma)
        else:
            registrar(nome_regra_idioma, AVISO, encontrado=enc_idioma)
    else:
        registrar(nome_regra_idioma, OK, det_idioma)

    # 7. Integridade (filtra falsos positivos de timestamps/metadados; recupera com IA se ainda faltar)
    if entrada:
        status_int, det_int = checar_integridade(entrada, md)
        if status_int == AVISO and reparar:
            pct_antes, presentes_antes, faltantes_antes, _, _ = _analisar_integridade(entrada, md)
            total_in = len(presentes_antes) + len(faltantes_antes)
            enc_int = (
                f"{pct_antes}% dos números da fonte ({len(presentes_antes)}/{total_in}) — "
                f"{len(faltantes_antes)} ausente(s) ({_amostra(faltantes_antes.values(), 6, aspas=False)})"
            )
            _emitir(
                notificar, "ia_inicio", "Integridade (números)",
                encontrado=enc_int,
                acao="restaurando trechos e números omitidos com IA...",
                etapa=f"🔧 restaurando números omitidos ({pct_antes}%)",
            )
            md_rec, _, _, melhorou, det_rec, faltantes_depois = _reparar_integridade_com_ia(md, entrada, idioma, reparar)
            if melhorou:
                md = md_rec
                status_int, det_int = checar_integridade(entrada, md)
                if status_int == OK:
                    registrar("Integridade (números)", CORRIGIDO, encontrado=enc_int, corrigido=det_rec, usou_ia=True)
                else:
                    faltam_str = _amostra(faltantes_depois.values(), 4, aspas=False)
                    sufixo_falt = f"; ainda faltam: {faltam_str}" if faltam_str else ""
                    registrar(
                        "Integridade (números)", AVISO,
                        encontrado=f"recuperado parcialmente com IA ({det_rec}){sufixo_falt}",
                    )
            else:
                registrar("Integridade (números)", AVISO, encontrado=enc_int)
        elif status_int == AVISO:
            registrar("Integridade (números)", AVISO, encontrado=det_int)
        else:
            registrar("Integridade (números)", OK, det_int)

    # 8. Bloco final: Voz dos Ouvintes + Enquete (reordena e padroniza)
    md, info = reorganizar_bloco_final(md, idioma)
    titulo_voz, titulo_enq = TITULO_VOZ[idioma], TITULO_ENQUETE[idioma]
    if info["voz"]:
        houve_renome_voz = any(t.lower() != titulo_voz.lower() for t in info["titulos_voz"])
        if info["voz"] > 1 or info["trocas_ouvintes"] or houve_renome_voz:
            partes_enc, partes_corr = [], []
            if info["voz"] > 1 or houve_renome_voz:
                partes_enc.append(f"{info['voz']} bloco(s) ({_amostra(info['titulos_voz'], 3)})")
                partes_corr.append(
                    f"unificado(s) em '## {titulo_voz}'" if info["voz"] > 1 else f"padronizado em '## {titulo_voz}'"
                )
            if info["trocas_ouvintes"]:
                ex_tr = _amostra(info["exemplos_trocas"], 3, aspas=False)
                partes_enc.append(f"{info['trocas_ouvintes']} termo(s) de newsletter ({ex_tr})")
                partes_corr.append("adaptado para linguagem de áudio")
            registrar(titulo_voz, CORRIGIDO, encontrado="; ".join(partes_enc), corrigido="; ".join(partes_corr))
        else:
            registrar(titulo_voz, OK, f"1 bloco em '## {titulo_voz}' (já adaptado para áudio)")
    else:
        registrar(titulo_voz, OK, "fonte sem comentários/resultados anteriores (opcional)")

    # 9. Enquete do Dia (obrigatória; recria se faltar ou vier quebrada, unifica se múltipla)
    if info["tem_enquete"]:
        if info["opcoes_enquete"] < 2:
            enc_enq = f"enquete '{info['titulo_enquete_original']}' com apenas {info['opcoes_enquete']} opção(ões)"
            if gerar:
                _emitir(
                    notificar, "ia_inicio", titulo_enq,
                    encontrado=enc_enq,
                    acao=f"recriando {titulo_enq} com IA...",
                    etapa=f"🗳 recriando {titulo_enq} com IA",
                )
            md_novo, bloco, origem, usou_ia_enq = _substituir_enquete_incompleta(md, idioma, gerar)
            if bloco:
                md = md_novo
                n_op = sum(1 for l in bloco.split("\n") if _RE_BULLET.match(l))
                perg = _extrair_pergunta_enquete(bloco)
                corr_enq = f"{origem} ({n_op} opções" + (f": '{perg}'" if perg else "") + ")"
                registrar(titulo_enq, CORRIGIDO, encontrado=enc_enq, corrigido=corr_enq, usou_ia=usou_ia_enq)
            else:
                registrar(titulo_enq, AVISO, encontrado=enc_enq + " e não foi possível recriar")
        elif info["perguntas_enquete"] > 1:
            enc_enq = (
                f"{info['perguntas_enquete']} enquetes no bloco '{info['titulo_enquete_original']}' "
                f"({info['opcoes_enquete']} opções no total)"
            )
            md, unificada, perg = _limpar_enquete_multipla(md, idioma)
            if unificada:
                _, info_pos = reorganizar_bloco_final(md, idioma)
                corr_enq = (
                    f"mantida apenas uma enquete em '## {titulo_enq}' ({info_pos['opcoes_enquete']} opções"
                    + (f": '{perg}'" if perg else "") + ")"
                )
                registrar(titulo_enq, CORRIGIDO, encontrado=enc_enq, corrigido=corr_enq)
            else:
                registrar(titulo_enq, AVISO, encontrado=enc_enq)
        elif info["enquete_renomeada"] or info["enquete_duplicada"]:
            enc_parts = []
            if info["enquete_renomeada"]:
                enc_parts.append(f"título fora do padrão ('{info['titulo_enquete_original']}')")
            if info["enquete_duplicada"]:
                enc_parts.append("mais de uma seção de enquete separada")
            perg = info.get("pergunta_enquete") or ""
            corr_enq = (
                f"mantida apenas uma enquete em '## {titulo_enq}' ({info['opcoes_enquete']} opções"
                + (f": '{perg}'" if perg else "") + ")"
                if info["enquete_duplicada"]
                else f"padronizado para '## {titulo_enq}' ({info['opcoes_enquete']} opções)"
            )
            registrar(
                titulo_enq, CORRIGIDO,
                encontrado="; ".join(enc_parts),
                corrigido=corr_enq,
            )
        else:
            registrar(titulo_enq, OK, f"presente com {info['opcoes_enquete']} opções")
    else:
        enc_enq = "edição sem seção de enquete"
        bloco, origem, usou_ia_enq = None, "", False
        if gerar:
            _emitir(
                notificar, "ia_inicio", titulo_enq,
                encontrado=enc_enq,
                acao=f"criando {titulo_enq} com IA a partir da pauta...",
                etapa=f"🗳 criando {titulo_enq} com IA",
            )
            try:
                bloco = _montar_bloco_enquete(gerar(_contexto_para_enquete(md), PROMPT_ENQUETE[idioma]), idioma)
                if bloco:
                    origem = "criada pela IA a partir da pauta"
                    usou_ia_enq = True
            except Exception:
                bloco = None
        if not bloco:
            bloco = enquete_de_fallback(md, idioma)
            origem = "criada com as manchetes do dia (IA indisponível)"
        if bloco:
            md = md.rstrip() + "\n\n" + bloco
            n_op = sum(1 for l in bloco.split("\n") if _RE_BULLET.match(l))
            perg = _extrair_pergunta_enquete(bloco)
            corr_enq = f"{origem} ({n_op} opções" + (f": '{perg}'" if perg else "") + ")"
            registrar(titulo_enq, CORRIGIDO, encontrado=enc_enq, corrigido=corr_enq, usou_ia=usou_ia_enq)
        else:
            registrar(titulo_enq, AVISO, encontrado="edição sem enquete e pauta muito curta para criar uma")

    # 10. Notícias extraídas + Ordem final
    _, secoes = dividir_secoes(md)
    n_noticias = sum(1 for s in secoes if _eh_secao_noticia(s))
    if n_noticias:
        if titulos_promovidos:
            registrar(
                "Notícias extraídas", CORRIGIDO,
                encontrado=f"nenhuma seção '##' (notícias estavam em '###': {_amostra(titulos_promovidos, 3)})",
                corrigido=f"{len(titulos_promovidos)} subtítulo(s) '###' promovido(s) para '##' ({n_noticias} seção(ões) de conteúdo)",
            )
        else:
            registrar("Notícias extraídas", OK, f"{n_noticias} seção(ões) ## de conteúdo")
    else:
        registrar("Notícias extraídas", FALHA, encontrado="nenhuma seção ## de notícia encontrada")

    ultima = secoes[-1]["titulo"] if secoes else ""
    if _RE_ENQUETE.match(ultima):
        if info["mudou_ordem"]:
            fora = _amostra(info.get("fora_de_ordem") or [], 3)
            enc_ord = (
                f"seção(ões) fora de posição ({fora}) antes do fim das notícias"
                if fora else "seções finais fora da ordem esperada"
            )
            registrar(
                "Ordem do bloco final", CORRIGIDO,
                encontrado=enc_ord,
                corrigido=f"reordenado para notícias → {titulo_voz if info['voz'] else '…'} → {titulo_enq}",
            )
        else:
            registrar("Ordem do bloco final", OK, f"notícias → {titulo_voz if info['voz'] else '…'} → {titulo_enq}")
    else:
        registrar("Ordem do bloco final", AVISO, encontrado=f"última seção é '{ultima}' (esperado: {titulo_enq})")

    ordem = ["Cabeçalho", "Notícias", "Integridade", "Tabelas", "Links", "Ruído", "Idioma",
             titulo_voz, titulo_enq, "Ordem"]
    rel.checagens.sort(key=lambda c: next((i for i, p in enumerate(ordem) if c.regra.startswith(p)), len(ordem)))
    return re.sub(r"\n{3,}", "\n\n", md).strip(), rel
