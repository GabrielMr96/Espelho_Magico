# Prompts para Geração de Podcasts e Vídeos Curtos (Google NotebookLM)

Esta pasta reúne os prompts oficiais e otimizados para gerar os episódios completos (**Audio Overview**) e os cortes verticais de divulgação (**Video Overview**) da rede **Central do Caos** no **Google NotebookLM** a partir dos arquivos `.md` extraídos pelo **Espelho Mágico**.

## Como funciona

Todos os arquivos `.md` gerados pelo **Espelho Mágico** trazem automaticamente na **linha 1 (`#`)** o nome do programa, o número da edição e a data:

```markdown
# Controle Quebrado #275 — 11 de novembro de 2026
# The Out of Order Dispatch #67 — November 11, 2026
```

Graças a isso, **você não precisa editar o nome do programa nem o número da edição no prompt a cada episódio**: basta copiar e colar o prompt correspondente ao formato e idioma do programa.

## Arquivos disponíveis

| Arquivo | Formato | Programas atendidos |
| :--- | :--- | :--- |
| [`podcast_pt_br_universal.md`](./podcast_pt_br_universal.md) | **Áudio completo** (Audio Overview) | **Todos os programas em Português:**<br>• *Boletim Fora de Ordem*<br>• *Fechamento de Caixa*<br>• *Diagnóstico Sincero*<br>• *Gol Contra*<br>• *Ata de Reunião*<br>• *Alucinação Sintética*<br>• *Hackeando o Problema*<br>• *Pipoca Vencida*<br>• *Controle Quebrado* |
| [`podcast_en_the_out_of_order_dispatch.md`](./podcast_en_the_out_of_order_dispatch.md) | **Áudio completo** (Audio Overview) | **Programa em Inglês:**<br>• *The Out of Order Dispatch* |
| [`video_curto_pt_br_universal.md`](./video_curto_pt_br_universal.md) | **Vídeo curto ~1 min vertical 9:16** (Video Overview — Instagram Stories e Status do WhatsApp) | **Todos os programas em Português** (narração feminina única com chamada dinâmica para o episódio completo) |
| [`legenda_instagram_pt_br_universal.md`](./legenda_instagram_pt_br_universal.md) | **Legenda para Instagram Reels** (Chat do NotebookLM) | **Todos os programas em Português** (copy pronta anti-shadowban, polarização adaptada ao tema e 4 hashtags) |

## Duração recomendada no NotebookLM

| Tamanho da pauta | Configuração no NotebookLM | Resultado esperado |
| :--- | :--- | :--- |
| **Curta (3–5 matérias)** — ex.: *Controle Quebrado* | **Mais curto (Shorter)** (~10 min) | Ritmo direto sem enchimento de linguiça nem loops ideológicos |
| **Média/Longa (7–14 matérias)** — ex.: *Boletim Fora de Ordem*, *Pipoca Vencida* | **Padrão (Default)** (~14–15 min) | Cobre todas as seções na ordem sem correr e fecha o bloco final sem corte abrupto |

