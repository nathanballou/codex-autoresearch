# Autoresearch

[English](../../README.md) | **Español**

Un bucle autónomo y medible de experimentación para Codex.

Indica a Codex un objetivo numérico. Codex inspecciona el repositorio, confirma el experimento, cambia una cosa, verifica, conserva las mejoras, revierte los fallos y repite hasta alcanzar el objetivo.

Sirve para fallos de pruebas, cobertura, errores de tipos, avisos, latencia, tamaño de binarios y hallazgos de seguridad reproducibles.

## Inicio rápido

Instala desde Codex:

```text
$skill-installer install https://github.com/leo-lilinxiao/autoresearch
```

Abre un repositorio Git limpio con Full Access:

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

Después invoca:

```text
$autoresearch reduce error_count de `python3 scripts/score.py` a 0
```

Antes de escribir, Codex confirma el objetivo, el alcance, la línea base, la meta, el comando de medida, el guard opcional y la concurrencia.

## Bucle

```text
examinar -> cambiar una hipótesis -> commit y medida
                                      |
                         mejora + guard correcto: conservar
                         si no: git revert
                                      |
                              registrar y repetir
```

Codex decide las hipótesis y modifica el código. El script de control posee los límites Git, la medida, el rollback y el estado.

## Candidatos en paralelo

| | |
|---|---|
| Aislamiento | Un worktree de Git de larga vida por slot |
| Asignación | Reparto adaptativo entre profundizar en el mejor resultado y probar ideas nuevas |
| Cómputo | Un banco declarado de núcleos y máquinas completas; cada candidato recibe una concesión |
| Admisión | Serializada; un candidato con base obsoleta se rebasa y se vuelve a medir |
| Vigencia | Leases, porque el plano de control no posee los procesos de los workers |

Cada worker recibe el mismo objetivo general y las mismas decisiones curadas, además de su propio objetivo individual. Un host que no pueda lanzar subagentes concurrentes reclama un slot cada vez y degrada a ejecución secuencial con el mismo modelo de estado.

## Resultados

Los archivos no confirmados viven en `autoresearch-results/`:

| Ruta | Función |
|---|---|
| `run.json` | Configuración confirmada e inmutable |
| `events.jsonl` | Historial de estado de solo anexado |
| `logs/` | Salida completa de métricas, guards y workers |
| `slots.json` | Estado de slots, leases y concesiones de cómputo |
| `docs/` | Instantáneas de los documentos curados |

`events.jsonl` es la única fuente del estado. Los datos ausentes, dañados o contradictorios producen un error explícito; nunca se reconstruyen por aproximación.

## Historial e informe

```text
$autoresearch show experiment history
$autoresearch export experiment history as TSV
$autoresearch generate an HTML report
```

La tabla y el informe HTML se generan desde eventos validados. La instantánea HTML se guarda en `autoresearch-results/report.html` y no forma parte del estado ni de la recuperación.

## Garantías

- Una ejecución nueva exige una rama Git limpia y con nombre.
- Cada ejecución gestiona un repositorio, una métrica y una meta.
- Cada experimento se confirma; los fallos se revierten con `git revert`.
- Cambios fuera de alcance, deriva Git, métricas inválidas, fallos de comandos, tiempos agotados y errores de rollback detienen el proceso con una ruta de log.
- Solo se marca `complete` cuando la métrica conservada alcanza la meta.

## Requisitos

- Codex CLI actual con Skills y Goals
- Python 3.11+
- Git

Consulta [Instalación](../INSTALL.md), [Guía de usuario](../GUIDE.md) y [Ejemplos](../EXAMPLES.md).

Licencia MIT. Inspirado por [autoresearch de Karpathy](https://github.com/karpathy/autoresearch).
