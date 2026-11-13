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

from rich import box
from rich.table import Table

OK, CORRIGIDO, AVISO, FALHA = "ok", "corrigido", "aviso", "falha"
ICONES = {OK: "✔", CORRIGIDO: "🔧", AVISO: "⚠", FALHA: "✖"}
CORES = {OK: "green", CORRIGIDO: "cyan", AVISO: "yellow", FALHA: "red"}

TITULO_ENQUETE = {"pt": "Enquete do Dia", "en": "Daily Poll"}
TITULO_VOZ = {"pt": "Voz dos Ouvintes", "en": "Listener Feedback"}

_RE_H2 = re.compile(r"^##\s+(.+?)\s*$")
_RE_ENQUETE = re.compile(
    r"^(enquete(\s+(d[oe]\s+dia|de\s+hoje))?|daily\s+poll|poll(\s+of\s+the\s+day)?|today'?s\s+poll|the\s+poll)\s*$",
    re.I,
)
_RE_VOZ = re.compile(
    r"^(voz\s+d[oe]s?\s+(leitor|leitores|ouvinte|ouvintes)|opini[aã]o\s+d[oe]s?\s+(leitor|leitores|ouvinte|ouvintes)|"
    r"coment[aá]rios\s+d[oe]s?\s+(leitores|ouvintes)|resultado\s+da\s+enquete.*|"
    r"listener\s+feedback|reader\s+(feedback|comments)|poll\s+results.*)$",
    re.I,
)
_RE_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
_RE_LINHA_TABELA = re.compile(r"^\s*\|.*\|\s*$")
_RE_SEPARADOR_TABELA = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_RE_IMAGEM_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK_MD = re.compile(r"(?<!!)\[([^\]]+)\]\((?:https?://|www\.)[^)]*\)")
_RE_URL_CRUA = re.compile(r"\s*\(?\b(?:https?://|www\.)\S+?\)?(?=[\s,;]|$)")
_RE_URL_TEXTO = re.compile(r"https?://\S+|www\.\S+|\]\([^)]*\)")
_RE_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")
_RE_META_BLOCO = re.compile(r"<metadados>.*?</metadados>", re.S | re.I)
_RE_TAGS_DOC = re.compile(r"</?(?:documento_entrada|metadados)>", re.I)
_RE_IMG_DESC = re.compile(r"\[(?:Descrição de Imagem|Imagem):[^\]]*\]", re.I)
_RE_GERADO_EM = re.compile(r"^\s*(?:Gerado|Generated)\s+(?:em|on|at)\b.*$", re.I | re.M)
_RE_TEMPO_LEITURA = re.compile(
    r"^\s*\d+(?:\s*[–-]\s*\d+)?\s*(?:min|minutos?|minutes?)(?:\s+de\s+leitura)?\s*$",
    re.I | re.M,
)
_RE_HORA = re.compile(r"(?:\bàs\s+\d{1,2}[:h]\d{2}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b)", re.I)
_RE_DATA_NUM = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_RE_LEIA_TAMBEM = re.compile(
    r"^#{2,4}[ \t]*(?:Leia\s+também|Veja\s+também|Saiba\s+mais|Read\s+also|Related)\b[^\n]*"
    r"(?:\n(?:[ \t]*\n)?[ \t]*[-*•][ \t]+[^\n]+)*",
    re.I | re.M,
)
_RE_RUIDO = re.compile(
    r"(?:\b(?:assine\s+(?:j[aá]|agora|a\s+pro)|assinatura\s+premium|"
    r"conte[uú]do\s+patrocinado|publieditorial|sponsored\s+content|"
    r"go\s+premium|upgrade\s+to\s+(?:pro|premium)|continue\s+lendo|keep\s+reading|"
    r"indique\s+(?:um\s+amigo|e\s+ganhe)|refer\s+a\s+friend|use\s+o\s+cupom)\b|"
    r"^\s*(?:[-*•]\s*)?\*{0,2}(?:oferecimento|apresentado\s+por|sponsored\s+by|together\s+with|"
    r"presented\s+by|na\s+edi[cç][aã]o\s+de\s+hoje|in\s+today'?s\s+(?:edition|issue))\*{0,2}\s*:)",
    re.I | re.M,
)
_RE_RUIDO_LINHA = re.compile(
    r"^\s*(?:[-*•>]\s*)?\*{0,2}(?:"
    r"(?:oferecimento|apresentado\s+por|sponsored\s+by|together\s+with|"
    r"presented\s+by|na\s+edi[cç][aã]o\s+de\s+hoje|in\s+today'?s\s+(?:edition|issue))\*{0,2}\s*:|"
    r"(?:assine\s+(?:j[aá]|agora|a\s+pro)|assinatura\s+premium|conte[uú]do\s+patrocinado|"
    r"publieditorial|sponsored\s+content|go\s+premium|upgrade\s+to\s+(?:pro|premium)|"
    r"continue\s+lendo|keep\s+reading|indique\s+(?:um\s+amigo|e\s+ganhe)|"
    r"refer\s+a\s+friend|use\s+o\s+cupom)\b)",
    re.I,
)
_RE_RUIDO_INLINE = re.compile(
    r"\s*(?:[—\-–|]\s*|\(\s*)?\b(?:continue\s+lendo|keep\s+reading|assine\s+(?:j[aá]|agora|a\s+pro)|"
    r"upgrade\s+to\s+(?:pro|premium)|go\s+premium|conte[uú]do\s+patrocinado|sponsored\s+content)\b"
    r"[^.!?\n]*[.!?\)]?",
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

@dataclass
class Checagem:
    regra: str
    status: str
    detalhe: str = ""


@dataclass
class RelatorioValidacao:
    checagens: list = field(default_factory=list)

    def add(self, regra, status, detalhe=""):
        self.checagens.append(Checagem(regra, status, detalhe))

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
        title=f"🔎 Dupla verificação · {titulo}",
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
        tabela.add_row(f"[{cor}]{ICONES[c.status]}[/{cor}]", c.regra, f"[{cor}]{c.detalhe}[/{cor}]")
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


def converter_tabelas_em_topicos(md, idioma="pt"):
    """Troca tabelas Markdown por tópicos `- **Ativo:** coluna valor · coluna valor` (100% dos dados)."""
    linhas = md.split("\n")
    saida, i, convertidas = [], 0, 0
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
            i = j
            continue
        saida.append(linhas[i])
        i += 1
    return "\n".join(saida), convertidas


def remover_links_e_imagens(md):
    """Remove imagens Markdown, troca `[texto](url)` por `texto` e apaga URLs cruas (não narráveis)."""
    total = len(_RE_IMAGEM_MD.findall(md)) + len(_RE_LINK_MD.findall(md))
    md = _RE_IMAGEM_MD.sub("", md)
    md = _RE_LINK_MD.sub(r"\1", md)
    novas = []
    for linha in md.split("\n"):
        if re.search(r"https?://|www\.", linha):
            total += len(re.findall(r"https?://|www\.", linha))
            linha = _RE_URL_CRUA.sub("", linha).rstrip()
            if re.fullmatch(r"\s*[-*•]?\s*", linha):
                continue
        novas.append(linha)
    return "\n".join(novas), total


def _adaptar_ouvintes(linhas, idioma):
    trocas = 0
    if idioma == "en":
        pares = [
            (r"\bReaders\b", "Listeners"),
            (r"\breaders\b", "listeners"),
            (r"\bReader\b", "Listener"),
            (r"\breader\b", "listener"),
            (r"\bThe Peak\b", "The Out of Order Dispatch"),
            (r"\bnewsletter\b", "show"),
        ]
    else:
        pares = [
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
            linha, n = re.subn(padrao, troca, linha)
            trocas += n
        novas.append(linha)
    return novas, trocas


def reorganizar_bloco_final(md, idioma="pt"):
    """Garante a ordem notícias → Voz dos Ouvintes → Enquete do Dia e padroniza os títulos.

    Devolve (md, info) com: mudou_ordem, voz (qtde de seções unificadas), trocas_ouvintes,
    enquete_renomeada, opcoes_enquete, perguntas_enquete.
    """
    preambulo, secoes = dividir_secoes(md)
    voz = [s for s in secoes if _eh_voz(s)]
    enquetes = [s for s in secoes if _eh_enquete(s)]
    corpo = [s for s in secoes if _eh_secao_noticia(s)]
    info = {"mudou_ordem": False, "voz": len(voz), "trocas_ouvintes": 0, "enquete_renomeada": False,
            "opcoes_enquete": 0, "perguntas_enquete": 0, "tem_enquete": bool(enquetes)}

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
        linhas_voz, info["trocas_ouvintes"] = _adaptar_ouvintes(linhas_voz, idioma)
        bloco_voz = [{"titulo": canon, "linhas": _sem_rabo(linhas_voz)}]

    bloco_enquete = []
    if enquetes:
        canon = TITULO_ENQUETE[idioma]
        principal = enquetes[0]
        linhas = [f"## {canon}"] + principal["linhas"][1:]
        info["enquete_renomeada"] = principal["titulo"].lower() != canon.lower()
        for extra in enquetes[1:]:
            linhas += [""] + extra["linhas"][1:]
        linhas = _sem_rabo(linhas)
        info["opcoes_enquete"] = sum(1 for l in linhas[1:] if _RE_BULLET.match(l))
        info["perguntas_enquete"] = _contar_blocos_de_opcoes(linhas[1:])
        bloco_enquete = [{"titulo": canon, "linhas": linhas}]

    if corpo and (voz or enquetes):
        corpo[-1]["linhas"] = _sem_rabo(corpo[-1]["linhas"])
    return juntar_secoes(preambulo, corpo + bloco_voz + bloco_enquete), info


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


def _contexto_para_enquete(md, limite=6000):
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
    linhas = [l.strip() for l in resposta.strip().strip("`").split("\n")]
    opcoes = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", l) for l in linhas if _RE_BULLET.match(l)]
    pergunta = next(
        (l for l in linhas if l and not l.startswith("#") and not _RE_BULLET.match(l) and "?" in l),
        None,
    )
    if not pergunta or len(opcoes) < 3:
        return None
    opcoes = opcoes[:5]
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

def _filtrar_para_integridade(texto, eh_entrada=False):
    """Remove metadados de e-mail, URLs, timestamps e caixas 'Leia também' antes de contar números."""
    texto = _RE_META_BLOCO.sub(" ", texto or "")
    texto = _RE_TAGS_DOC.sub(" ", texto)
    texto = _RE_URL_TEXTO.sub(" ", texto)
    texto = _RE_IMG_DESC.sub(" ", texto)
    if eh_entrada:
        texto = _RE_LEIA_TAMBEM.sub(" ", texto)
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
        normal = re.sub(r"[.,]", "", bruto).lstrip("0")
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
    """Garante H1 único na linha 1 com data reconhecível quando possível extrair a data."""
    alterou = False
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
            md = f"# {dt_str}\n\n{md}" if md else f"# {dt_str}"
            alterou = True
    else:
        data_ok = extrair_data(primeira) if extrair_data else True
        if not data_ok:
            dt_str = data_de(entrada) or data_de("\n".join(linhas[1:]))
            if dt_str:
                titulo_atual = primeira.lstrip("# ").strip()
                if not titulo_atual or re.fullmatch(r"(?:sem\s+data|edi[cç][aã]o(?:\s+de\s+hoje)?)", titulo_atual, re.I):
                    linhas[0] = f"# {dt_str}"
                else:
                    linhas[0] = f"# {titulo_atual} — {dt_str}"
                md = "\n".join(linhas)
                alterou = True

    linhas = md.split("\n")
    novas = []
    viu_h1 = False
    for linha in linhas:
        if re.match(r"^#\s+", linha):
            if not viu_h1:
                viu_h1 = True
                novas.append(linha)
            else:
                novas.append("#" + linha)
                alterou = True
        else:
            novas.append(linha)
    md = "\n".join(novas).strip()

    linhas_fin = md.split("\n")
    primeira_fin = linhas_fin[0].strip() if linhas_fin else ""
    if not primeira_fin.startswith("# "):
        return md, AVISO, "linha 1 não é um título # com a data da edição"
    if extrair_data and not extrair_data(primeira_fin):
        return md, AVISO, f"sem data reconhecível em '{primeira_fin[:60]}'"
    sufixo = " (ajustado automaticamente)" if alterou else ""
    return md, (CORRIGIDO if alterou else OK), primeira_fin.lstrip("# ").strip() + sufixo


def remover_ruido_residual(md):
    """Remove linhas de anúncio/paywall/CTA residuais e CTAs embutidos seguros. Devolve (md, removidos)."""
    removidos = 0
    novas = []
    for linha in md.split("\n"):
        if _RE_RUIDO_LINHA.match(linha):
            removidos += 1
            continue
        nova, n = _RE_RUIDO_INLINE.subn("", linha)
        if n:
            removidos += n
            nova = nova.rstrip()
            if not nova:
                continue
        novas.append(nova)
    return "\n".join(novas), removidos


def _promover_subtitulos_se_sem_noticias(md):
    """Se a IA gerou ### em vez de ## para as notícias, promove para ## automaticamente."""
    if contar_noticias(md) > 0:
        return md, 0
    promovidos = 0
    novas = []
    for linha in md.split("\n"):
        m = re.match(r"^###\s+(.+?)\s*$", linha)
        if m and not _RE_ENQUETE.match(m.group(1)) and not _RE_VOZ.match(m.group(1)):
            novas.append(f"## {m.group(1)}")
            promovidos += 1
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


def _higienizar_candidato(md, idioma):
    """Reaplica limpezas determinísticas sobre textos devolvidos por uma etapa de reparo da IA."""
    md, _ = remover_links_e_imagens(md)
    md, _ = converter_tabelas_em_topicos(md, idioma)
    md, _ = remover_ruido_residual(md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _reparar_idioma_com_ia(md, idioma, reparar):
    """Usa IA para eliminar mistura de idiomas nas seções afetadas sem perder fatos ou números."""
    pre, secoes = dividir_secoes(md)
    afetadas = [s for s in secoes if _diagnostico_idioma("\n".join(s["linhas"]), idioma)[0]]
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
        return md, False
    if not cand:
        return md, False

    if parcial:
        _, secs_rep = dividir_secoes(cand)
        mapa = {_norm_titulo(s["titulo"]): s for s in secs_rep}
        novas = [mapa.get(_norm_titulo(s["titulo"]), s) if s in afetadas else s for s in secoes]
        cand = juntar_secoes(pre, novas)

    prob, _ = _diagnostico_idioma(cand, idioma)
    if prob or contar_noticias(cand) < contar_noticias(md):
        return md, False
    if len(_numeros(cand)) < max(0, len(_numeros(md)) - 1):
        return md, False
    return cand, True


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
        idx = indice_corpos.get(chave)
        if idx is None:
            palavras_rec = set(chave.split())
            if palavras_rec:
                melhor = max(
                    range(len(corpos)),
                    key=lambda i: len(palavras_rec & set(_norm_titulo(corpos[i]["titulo"]).split())) / len(palavras_rec),
                    default=None,
                )
                if melhor is not None:
                    comum = len(palavras_rec & set(_norm_titulo(corpos[melhor]["titulo"]).split())) / len(palavras_rec)
                    if comum >= 0.65:
                        idx = melhor
        if idx is not None:
            txt_rec = "\n".join(s_rec["linhas"])
            txt_old = "\n".join(corpos[idx]["linhas"])
            if len(_numeros(txt_rec)) >= len(_numeros(txt_old)) and len(txt_rec) >= len(txt_old) * 0.85:
                corpos[idx] = s_rec
        else:
            indice_corpos[chave] = len(corpos)
            corpos.append(s_rec)
    return juntar_secoes(pre_orig, corpos + finais)


def _reparar_integridade_com_ia(md, entrada, idioma, reparar):
    """Tenta recuperar com IA notícias/números omitidos da fonte e valida antes de aceitar."""
    pct_antes, _, faltantes, _, entrada_limpa = _analisar_integridade(entrada, md)
    trechos = _trechos_para_integridade(entrada_limpa, faltantes)
    if not trechos:
        return md, pct_antes, pct_antes, False

    lista_ausentes = ", ".join(list(faltantes.values())[:10])
    full_doc = len(md) <= 8500
    if full_doc:
        instrucao = (
            "A dupla verificação detectou possível omissão de dados/números da fonte original "
            f"(ausentes: {lista_ausentes}). Compare o RASCUNHO ATUAL com os TRECHOS DA FONTE e devolva o "
            "Markdown COMPLETO restaurando todos os fatos e números faltantes, mantendo o formato limpo de roteiro "
            "(título #, seções ##, tópicos em vez de tabelas, sem URLs e preservando Enquete do Dia/Daily Poll ao final)."
        )
        conteudo = f"<rascunho_atual>\n{md}\n</rascunho_atual>\n\n<trechos_fonte>\n{trechos}\n</trechos_fonte>"
    else:
        titulos_atuais = "\n".join(f"- {s['titulo']}" for s in dividir_secoes(md)[1] if _eh_secao_noticia(s))
        instrucao = (
            "A dupla verificação detectou possível omissão de dados/números da fonte original "
            f"(ausentes: {lista_ausentes}). Com base nos TRECHOS DA FONTE, escreva SOMENTE as seções ## completas "
            "que contêm esses números omitidos (mantendo o mesmo título ## exato se já existir na pauta para "
            "completá-la, ou criando a seção ## omitida), em formato limpo para podcast (sem URLs nem tabelas)."
        )
        conteudo = (
            f"<titulos_ja_extraidos>\n{titulos_atuais}\n</titulos_ja_extraidos>\n\n"
            f"<trechos_fonte>\n{trechos}\n</trechos_fonte>"
        )

    try:
        resposta = reparar(conteudo, instrucao)
    except Exception:
        return md, pct_antes, pct_antes, False
    if not resposta:
        return md, pct_antes, pct_antes, False

    resposta = _higienizar_candidato(resposta, idioma)
    cand = _mesclar_secoes_recuperadas(md, resposta, full_doc=full_doc)
    cand = _higienizar_candidato(cand, idioma)
    if contar_noticias(cand) < contar_noticias(md) or len(cand) < len(md) * 0.85:
        return md, pct_antes, pct_antes, False

    pct_depois, _, _, _, _ = _analisar_integridade(entrada, cand)
    if pct_depois <= pct_antes:
        return md, pct_antes, pct_antes, False
    return cand, pct_antes, pct_depois, True


def _limpar_enquete_multipla(md, idioma="pt"):
    """Mantém apenas uma enquete (primeira pergunta completa com opções) dentro do bloco Enquete do Dia."""
    pre, secoes = dividir_secoes(md)
    canon = TITULO_ENQUETE[idioma]
    alterou = False
    for s in secoes:
        if not _eh_enquete(s):
            continue
        blocos = []
        pergunta = []
        opcoes = []
        for linha in s["linhas"][1:]:
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

        escolhido = next((b for b in blocos if len(b[1]) >= 2), None) or (blocos[0] if blocos else None)
        if escolhido and escolhido[1]:
            p_txt = escolhido[0]
            s["linhas"] = [f"## {canon}", ""] + p_txt + [""] + escolhido[1]
            alterou = True
    return juntar_secoes(pre, secoes), alterou


def _substituir_enquete_incompleta(md, idioma, gerar=None):
    """Substitui um bloco de enquete quebrado (< 2 opções) por uma enquete válida (IA ou fallback)."""
    pre, secoes = dividir_secoes(md)
    sem_enq = juntar_secoes(pre, [s for s in secoes if not _eh_enquete(s)])
    bloco, origem = None, ""
    if gerar:
        try:
            bloco = _montar_bloco_enquete(gerar(_contexto_para_enquete(sem_enq), PROMPT_ENQUETE[idioma]), idioma)
            origem = "recriada pela IA a partir da pauta"
        except Exception:
            bloco = None
    if not bloco:
        bloco = enquete_de_fallback(sem_enq, idioma)
        origem = "recriada com as manchetes do dia (IA indisponível)"
    if not bloco:
        return md, None, ""
    return (sem_enq.rstrip() + "\n\n" + bloco).strip(), bloco, origem


def aplicar_dupla_verificacao(md, entrada="", idioma="pt", gerar=None, extrair_data=None, reparar=None):
    """Executa todas as regras sobre o Markdown final. Devolve (md_corrigido, RelatorioValidacao).

    - `gerar(conteudo, prompt) -> str`: função de IA para criar/recriar a enquete quando faltar (opcional).
    - `extrair_data(texto) -> date|None`: parser de data para conferir/reparar o cabeçalho (opcional).
    - `reparar(conteudo, prompt) -> str`: função de IA para corrigir mistura de idioma ou omissões (opcional).
    """
    rel = RelatorioValidacao()
    md = (md or "").strip()

    # 1. Cabeçalho de data e H1 único
    md, status_cab, det_cab = _corrigir_cabecalho(md, entrada, idioma, extrair_data)
    rel.add("Cabeçalho de data", status_cab, det_cab)

    # 2. Links e imagens (correção segura)
    md, n_links = remover_links_e_imagens(md)
    if n_links:
        rel.add("Links e imagens", CORRIGIDO, f"{n_links} link(s)/imagem(ns) crus removidos (não narráveis)")
    else:
        rel.add("Links e imagens", OK, "nenhum link cru no texto")

    # 3. Tabelas → tópicos narrativos
    md, n_tab = converter_tabelas_em_topicos(md, idioma)
    if n_tab:
        rel.add("Tabelas → tópicos", CORRIGIDO, f"{n_tab} tabela(s) convertida(s) em tópicos de locução")
    else:
        rel.add("Tabelas → tópicos", OK, "nenhuma tabela crua")

    # 4. Ruído residual (remove linhas/CTAs promocionais e recorre à IA se sobrar trecho embutido)
    md, n_ruido = remover_ruido_residual(md)
    ruidos = sorted({m.group(0).strip() for m in _RE_RUIDO.finditer(md)})
    if ruidos and reparar:
        try:
            prompt_ruido = (
                "Remova apenas as chamadas promocionais, de assinatura ou paywall residuais do Markdown abaixo "
                "(ex.: " + ", ".join(ruidos[:3]) + "), sem apagar nenhuma notícia, dado numérico ou título ##."
            )
            cand_ruido = _higienizar_candidato(reparar(md, prompt_ruido), idioma)
            if cand_ruido and contar_noticias(cand_ruido) >= contar_noticias(md) and not _RE_RUIDO.search(cand_ruido):
                md = cand_ruido
                n_ruido += len(ruidos)
                ruidos = []
        except Exception:
            pass
    if ruidos:
        rel.add("Ruído (anúncio/paywall)", AVISO, "trechos suspeitos: " + ", ".join(f"'{r}'" for r in ruidos[:4]))
    elif n_ruido:
        rel.add("Ruído (anúncio/paywall)", CORRIGIDO, f"{n_ruido} trecho(s) de publicidade/paywall removidos")
    else:
        rel.add("Ruído (anúncio/paywall)", OK, "sem publicidade, paywall ou sumários")

    # 5. Promove ### → ## caso nenhuma seção ## de notícia exista ainda
    md, n_promovidos = _promover_subtitulos_se_sem_noticias(md)

    # 6. Idioma (corrige com IA quando detecta mistura)
    nome_regra_idioma = "Idioma (100% inglês)" if idioma == "en" else "Idioma (sem inglês misturado)"
    prob_idioma, det_idioma = _diagnostico_idioma(md, idioma)
    if prob_idioma and reparar:
        md, corrigiu_id = _reparar_idioma_com_ia(md, idioma, reparar)
        if corrigiu_id:
            rel.add(nome_regra_idioma, CORRIGIDO, "mistura de idioma corrigida automaticamente com IA")
        else:
            rel.add(nome_regra_idioma, AVISO, det_idioma)
    elif prob_idioma:
        rel.add(nome_regra_idioma, AVISO, det_idioma)
    else:
        rel.add(nome_regra_idioma, OK, det_idioma)

    # 7. Integridade (filtra falsos positivos de timestamps/metadados; recupera com IA se ainda faltar)
    if entrada:
        status_int, det_int = checar_integridade(entrada, md)
        if status_int == AVISO and reparar:
            md_rec, pct_antes, pct_depois, melhorou = _reparar_integridade_com_ia(md, entrada, idioma, reparar)
            if melhorou:
                md = md_rec
                status_int, det_int = checar_integridade(entrada, md)
                if status_int == OK:
                    status_int = CORRIGIDO
                    det_int = f"omissões restauradas com IA ({pct_antes}% → {pct_depois}% dos números)"
                else:
                    det_int += f" (parcialmente recuperado de {pct_antes}%)"
        rel.add("Integridade (números)", status_int, det_int)

    # 8. Bloco final: Voz dos Ouvintes + Enquete (reordena e padroniza)
    md, info = reorganizar_bloco_final(md, idioma)
    titulo_voz, titulo_enq = TITULO_VOZ[idioma], TITULO_ENQUETE[idioma]
    if info["voz"]:
        extra = f" · {info['trocas_ouvintes']} menção(ões) a leitores → ouvintes" if info["trocas_ouvintes"] else ""
        status = CORRIGIDO if (info["voz"] > 1 or info["trocas_ouvintes"]) else OK
        rel.add(titulo_voz, status, f"{info['voz']} bloco(s) de enquete anterior/comentários{extra}")
    else:
        rel.add(titulo_voz, OK, "fonte sem comentários/resultados anteriores (opcional)")

    # 9. Enquete do Dia (obrigatória; recria se faltar ou vier quebrada, unifica se múltipla)
    if info["tem_enquete"]:
        if info["opcoes_enquete"] < 2:
            md_novo, bloco, origem = _substituir_enquete_incompleta(md, idioma, gerar)
            if bloco:
                md = md_novo
                n_op = sum(1 for l in bloco.split("\n") if _RE_BULLET.match(l))
                rel.add(titulo_enq, CORRIGIDO, f"enquete incompleta → {origem} ({n_op} opções)")
            else:
                rel.add(titulo_enq, AVISO, f"enquete com só {info['opcoes_enquete']} opção(ões)")
        elif info["perguntas_enquete"] > 1:
            md, unificada = _limpar_enquete_multipla(md, idioma)
            if unificada:
                _, info_pos = reorganizar_bloco_final(md, idioma)
                rel.add(titulo_enq, CORRIGIDO, f"múltiplas enquetes unificadas ({info_pos['opcoes_enquete']} opções)")
            else:
                rel.add(titulo_enq, AVISO, f"{info['perguntas_enquete']} enquetes no mesmo bloco ({info['opcoes_enquete']} opções)")
        else:
            rel.add(titulo_enq, CORRIGIDO if info["enquete_renomeada"] else OK,
                    f"presente com {info['opcoes_enquete']} opções" + (" (título padronizado)" if info["enquete_renomeada"] else ""))
    else:
        bloco, origem = None, ""
        if gerar:
            try:
                bloco = _montar_bloco_enquete(gerar(_contexto_para_enquete(md), PROMPT_ENQUETE[idioma]), idioma)
                origem = "criada pela IA a partir da pauta"
            except Exception:
                bloco = None
        if not bloco:
            bloco = enquete_de_fallback(md, idioma)
            origem = "criada com as manchetes do dia (IA indisponível)"
        if bloco:
            md = md.rstrip() + "\n\n" + bloco
            n_op = sum(1 for l in bloco.split("\n") if _RE_BULLET.match(l))
            rel.add(titulo_enq, CORRIGIDO, f"fonte sem enquete → {origem} ({n_op} opções)")
        else:
            rel.add(titulo_enq, AVISO, "não foi possível criar a enquete (pauta muito curta)")

    # 10. Notícias extraídas + Ordem final
    _, secoes = dividir_secoes(md)
    n_noticias = sum(1 for s in secoes if _eh_secao_noticia(s))
    if n_noticias:
        det_not = f"{n_noticias} seção(ões) ## de conteúdo" + (f" ({n_promovidos} título(s) ajustado(s))" if n_promovidos else "")
        rel.add("Notícias extraídas", CORRIGIDO if n_promovidos else OK, det_not)
    else:
        rel.add("Notícias extraídas", FALHA, "nenhuma seção ## de notícia encontrada")

    ultima = secoes[-1]["titulo"] if secoes else ""
    if _RE_ENQUETE.match(ultima):
        rel.add("Ordem do bloco final", CORRIGIDO if info["mudou_ordem"] else OK,
                ("reordenado: " if info["mudou_ordem"] else "") + f"notícias → {titulo_voz if info['voz'] else '…'} → {titulo_enq}")
    else:
        rel.add("Ordem do bloco final", AVISO, f"última seção é '{ultima}' (esperado: {titulo_enq})")

    ordem = ["Cabeçalho", "Notícias", "Integridade", "Tabelas", "Links", "Ruído", "Idioma",
             titulo_voz, titulo_enq, "Ordem"]
    rel.checagens.sort(key=lambda c: next((i for i, p in enumerate(ordem) if c.regra.startswith(p)), len(ordem)))
    return re.sub(r"\n{3,}", "\n\n", md).strip(), rel
