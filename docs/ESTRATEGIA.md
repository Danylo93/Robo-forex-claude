# A estratégia identificada nos gráficos

Os três gráficos de referência (GBP/NZD H1, HK50 H1 e AUD/USD H4) são o **mesmo
setup**, espelhado entre venda e compra.

## Nome

**Reteste de linha de oferta/demanda em confluência com order block**
(família *supply & demand* / *smart money*, com order block e rompimento de
estrutura).

## Os cinco componentes

| # | Componente | O que é | Como o robô mede |
| - | ---------- | ------- | ---------------- |
| 1 | **Linha de oferta/demanda** | Faixa horizontal de preço que já foi respeitada várias vezes | Agrupa topos (ou fundos) fractais em preços próximos (tolerância em ATR) e conta *toques* e *rejeições* |
| 2 | **Order block** | Última vela contrária antes do deslocamento institucional | Vela de alta seguida de queda ≥ 1,4× ATR que rompe o fundo anterior (BOS) — e o espelho para compra |
| 3 | **Confluência** | Order block **dentro** da linha de oferta/demanda | Interseção das duas faixas; a região comum é a zona operacional |
| 4 | **Linha de tendência** (opcional) | LTB/LTA chegando na mesma região — visível no GBP/NZD | Reta ajustada em 2+ pivots, validada por toques e ausência de fechamentos além dela |
| 5 | **Reteste** | O preço volta à região e encontra fluxo contrário | Ordem pendente na borda proximal quando o preço se aproxima (≤ 3× ATR) |

## Como os níveis são desenhados

```
                        ┌───────────────────────────┐
  STOP  ────────────────│  borda distal + 0,25 ATR  │   ← acima do topo da zona/OB
  VENDA ────────────────│  borda proximal da zona   │   ← entrada (limite)
                        └───────────────────────────┘
                                    │
                                    │  movimento esperado
                                    ▼
  ALVO  ─────────────────  liquidez oposta (fundo anterior / linha de demanda)
```

- **Entrada**: borda proximal da região de confluência (parte de baixo da zona
  de oferta; parte de cima da zona de demanda). Se o preço já estiver dentro da
  região, o robô converte para entrada a mercado.
- **Stop**: atrás da borda distal (topo da oferta / fundo da demanda) mais um
  respiro de 0,25× ATR — configurável em `risk.stop_buffer_atr`.
- **Alvo**: a liquidez oposta mais próxima (fundo/topo anterior ou zona
  contrária) **que já entregue o RR mínimo**. Se nenhuma referência estrutural
  servir, cai no alvo por RR fixo (modo `structure_then_rr`, padrão). É assim
  que se chega ao 3:1 do exemplo sem inventar um alvo no vazio.

## Nota do setup (0–100)

```
nota = 0,45 × força_da_zona + 0,35 × força_do_order_block
     + 0,10 × linha_de_tendência + 0,10 × viés_do_H4
```

- **força_da_zona**: toques (até 5), rejeições (até 4) e compactação da faixa.
- **força_do_order_block**: deslocamento em ATR (até 3×), BOS confirmado e se o
  bloco ainda não foi testado.
- Setups sem order block só passam se `confluence.require_order_block: false`, e
  ainda levam penalidade de 25%.

O corte padrão é `strategy.min_score: 55`.

## Filtros que impedem a operação

1. **Notícias** — evento de alto impacto de qualquer uma das moedas do par entre
   60 min antes e 30 min depois bloqueia o sinal (calendário Forex Factory).
2. **Sessão** — 6h–20h UTC, sem fim de semana, sem sexta após 19h UTC.
3. **Distância** — entrada a mais de 3× ATR do preço atual é apenas monitorada.
4. **Stop grande demais** — acima de 3,5× ATR o setup é descartado.
5. **RR** — abaixo de 3:1 não vira sinal.
6. **Estrutura rompida** — zona com fechamento além da borda distal é invalidada.

## O que este robô **não** faz

- Não envia ordens para corretora: ele analisa e entrega os níveis. A execução
  é sua (ou do robô HFT, que tem adaptadores de corretora).
- Não prevê notícia: ele evita o horário dela.
- Não garante o 3:1 do exemplo. O 3:1 é o critério de seleção, não o resultado.
  Use `robo-forex backtest` para medir a expectativa real no seu ativo antes de
  operar dinheiro de verdade.
