# Prompts para Geração de Podcasts (Google NotebookLM)

Esta pasta reúne os prompts oficiais e otimizados para gerar os episódios da rede **Central do Caos** no **Google NotebookLM** (Audio Overview) a partir dos arquivos `.md` extraídos pelo **Espelho Mágico**.

## Como funciona

Todos os arquivos `.md` gerados pelo **Espelho Mágico** trazem automaticamente na **linha 1 (`#`)** o nome do programa, o número da edição e a data:

```markdown
# Controle Quebrado #275 — 11 de novembro de 2026
# The Out of Order Dispatch #67 — November 11, 2026
```

Graças a isso, **você não precisa editar o nome do programa nem o número da edição no prompt a cada episódio**: basta copiar e colar o prompt correspondente ao idioma do programa.

## Arquivos disponíveis

| Arquivo | Programas atendidos |
| :--- | :--- |
| [`podcast_pt_br_universal.md`](./podcast_pt_br_universal.md) | **Todos os programas em Português:**<br>• *Boletim Fora de Ordem*<br>• *Fechamento de Caixa*<br>• *Diagnóstico Sincero*<br>• *Gol Contra*<br>• *Ata de Reunião*<br>• *Alucinação Sintética*<br>• *Hackeando o Problema*<br>• *Pipoca Vencida*<br>• *Controle Quebrado* |
| [`podcast_en_the_out_of_order_dispatch.md`](./podcast_en_the_out_of_order_dispatch.md) | **Programa em Inglês:**<br>• *The Out of Order Dispatch* |
