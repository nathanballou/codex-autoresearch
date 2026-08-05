# Autoresearch

[English](../../README.md) | **Português**

Um ciclo autônomo e mensurável de experimentação para Codex.

Diga ao Codex qual meta numérica deseja alcançar. Ele inspeciona o repositório, confirma o experimento, altera uma coisa, verifica, mantém melhorias, reverte falhas e repete até atingir a meta.

É adequado para falhas de teste, cobertura, erros de tipo, avisos, latência, tamanho de binário e achados de segurança reproduzíveis.

## Início rápido

Instale no Codex:

```text
$skill-installer install https://github.com/leo-lilinxiao/autoresearch
```

Abra um repositório Git limpo com Full Access:

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

Depois invoque:

```text
$autoresearch reduza error_count de `python3 scripts/score.py` para 0
```

Antes da primeira escrita, Codex confirma objetivo, escopo, linha de base, meta, comando de métrica, guard opcional e a concorrência.

## Ciclo

```text
examinar -> alterar uma hipótese -> commit e medição
                                       |
                          melhora + guard aprovado: manter
                          caso contrário: git revert
                                       |
                                registrar e repetir
```

Codex cuida das hipóteses e mudanças de código. O script de controle cuida dos limites Git, medição, rollback e estado.

## Candidatos em paralelo

| | |
|---|---|
| Isolamento | Uma worktree Git de longa duração por slot |
| Alocação | Divisão adaptativa entre aprofundar o melhor resultado e tentar ideias novas |
| Computação | Um banco declarado de núcleos e máquinas inteiras; cada candidato recebe uma concessão |
| Admissão | Serializada; um candidato com base desatualizada é rebaseado e remedido |
| Vitalidade | Leases, pois o plano de controlo não possui os processos dos workers |

Cada worker recebe o mesmo objetivo global e as mesmas decisões curadas, além do seu objetivo individual. Um host que não consiga lançar subagentes concorrentes reclama um slot de cada vez e degrada para execução sequencial com o mesmo modelo de estado.

## Resultados

Arquivos não commitados ficam em `autoresearch-results/`:

| Caminho | Finalidade |
|---|---|
| `run.json` | Configuração confirmada e imutável |
| `events.jsonl` | Histórico de estado somente por anexação |
| `logs/` | Saída completa de métricas, guards e workers |
| `slots.json` | Estado dos slots, leases e concessões de computação |
| `docs/` | Instantâneos dos documentos curados |

`events.jsonl` é a única fonte de estado. Dados ausentes, corrompidos ou contraditórios causam erro explícito e nunca são reconstruídos por suposição.

## Histórico e relatório

```text
$autoresearch show experiment history
$autoresearch export experiment history as TSV
$autoresearch generate an HTML report
```

A tabela e o relatório HTML são gerados a partir de eventos validados. O snapshot HTML fica em `autoresearch-results/report.html` e não participa do estado nem da recuperação.

## Garantias

- Uma nova execução exige uma branch Git limpa e nomeada.
- Cada execução gerencia um repositório, uma métrica e uma meta.
- Cada experimento vira commit; falhas são revertidas com `git revert`.
- Alterações fora do escopo, desvio Git, métricas inválidas, falhas de comando, timeout e erro de rollback interrompem com caminho de log.
- O estado só vira `complete` quando a métrica mantida atinge a meta.

## Requisitos

- Codex CLI atual com Skills e Goals
- Python 3.11+
- Git

Veja [Instalação](../INSTALL.md), [Guia do usuário](../GUIDE.md) e [Exemplos](../EXAMPLES.md).

Licença MIT. Inspirado no [autoresearch de Karpathy](https://github.com/karpathy/autoresearch).
