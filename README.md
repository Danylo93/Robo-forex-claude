# Robô Forex — Oferta/Demanda + Order Block

Robô que analisa cada ativo, identifica o padrão dos gráficos de referência
(linha de oferta/demanda respeitada várias vezes **+** order block em
confluência) e desenha **entrada, stop e alvo** com relação risco-retorno
mínima de 3:1 — respeitando calendário econômico e janela de sessão.

Núcleo em **Python puro (stdlib)**: sem numpy, sem pandas, sem instalar nada.

```
GBPNZD | 1h | VENDA 📉  (nota 68)
  VENDA  2.28640
  STOP   2.29152   (risco 1R = 0.00512)
  ALVO   2.26909   (RR 3.4:1)
  preço atual 2.28437 | viés HTF: up
  risco 50.00 | stop 51.2 pips | 0.04 lote(s)
  Racional:
    • Linha de oferta com 3 toques (3 rejeições) em 2.28640-2.29043
    • Order block de venda em 2.28194-2.29006 com deslocamento de 7.2x ATR e BOS
    • Linha de tendência de baixa com 6 toques passando pela zona
    • Alvo: fundo anterior em 2.26843
```

## Uso rápido

```bash
python3 -m robo_forex scan                          # varre a lista padrão (dados do Yahoo)
python3 -m robo_forex scan -s GBPNZD,AUDUSD -v      # ativos específicos + diagnóstico
python3 -m robo_forex scan --charts --json out.json # gera SVG e JSON dos setups
python3 -m robo_forex watch --interval 15           # varre a cada 15 minutos
python3 -m robo_forex calendar --hours 24           # o que trava a operação hoje
python3 -m robo_forex backtest -s GBPNZD --bars 1500
python3 -m robo_forex init-config --out config/config.yaml
```

Sem rede? `--provider synthetic` roda tudo offline com um padrão de exemplo, e
`--provider csv --csv-dir data` lê seus próprios arquivos
(`EURUSD_1h.csv` com colunas `time,open,high,low,close,volume`; entende também o
formato exportado pelo MT5).

## O padrão

Detalhes completos em [docs/ESTRATEGIA.md](docs/ESTRATEGIA.md).

1. **Linha de oferta/demanda** — topos (ou fundos) agrupados na mesma faixa, com
   toques e rejeições contados.
2. **Order block** — última vela contrária antes de um deslocamento ≥ 1,4× ATR
   que rompe estrutura (BOS).
3. **Confluência** — o order block precisa cair dentro da linha; a interseção é
   a região operacional. Linha de tendência na mesma região soma nota.
4. **Níveis** — entrada na borda proximal, stop atrás da borda distal + 0,25 ATR,
   alvo na liquidez oposta mais próxima que entregue ≥ 3:1.
5. **Filtros** — notícia de alto impacto, sessão, distância até a entrada,
   tamanho do stop, viés do tempo gráfico maior.

## Notícias

O filtro usa o calendário semanal do Forex Factory
(`https://nfs.faireconomy.media/ff_calendar_thisweek.json`) e bloqueia sinais de
60 min antes até 30 min depois de evento de alto impacto de **qualquer uma das
moedas do par**. Exemplo real de uma varredura:

```
EURUSD  · VENDA 1.14626-1.14718: notícia de alto impacto:
          USD Advance GDP q/q (30/07 12:30 UTC), USD Core PCE Price Index m/m
```

Configurável em `news`: `impacts`, `minutes_before/after`, `provider`
(`faireconomy` | `file` | `none`) e `fail_open` (o que fazer se o calendário
cair).

## Configuração

`python3 -m robo_forex init-config` grava um YAML com **todos** os parâmetros.
Os que mais importam:

| Chave | Padrão | Efeito |
| --- | --- | --- |
| `strategy.timeframe` / `htf_timeframe` | `1h` / `4h` | gráfico operacional e o do viés |
| `strategy.min_score` | `55` | nota mínima do setup |
| `strategy.max_distance_atr` | `3.0` | quão longe da entrada o preço pode estar |
| `strategy.zone.min_touches` | `2` | toques exigidos na linha |
| `strategy.order_block.displacement_atr` | `1.4` | força mínima do deslocamento |
| `strategy.confluence.require_order_block` | `true` | exige o order block |
| `risk.min_rr` | `3.0` | RR mínimo |
| `risk.risk_percent` | `0.5` | % do saldo arriscado por operação |
| `risk.target_mode` | `structure_then_rr` | como escolher o alvo |
| `news.minutes_before/after` | `60` / `30` | janela de bloqueio |
| `session.start_hour_utc` / `end_hour_utc` | `6` / `20` | janela de operação |

## Saídas

- **Console** — bloco `VENDA/COMPRA`, `STOP`, `ALVO`, racional e avisos.
- **JSON** (`--json`) — sinais + diagnóstico de cada ativo (por que não operou).
- **Markdown** (`--markdown`) — tabela pronta para colar em relatório.
- **SVG** (`--charts`) — gráfico com a zona, o order block e as três linhas,
  no mesmo layout dos prints.
- **Telegram** — preencha `output.telegram_token` e `output.telegram_chat_id`.

## Backtest

```bash
python3 -m robo_forex backtest -s GBPNZD --bars 1500 --out bt.json
```

Reexecuta a estratégia barra a barra (sem look-ahead), transforma cada sinal em
ordem pendente e mede: operações, acerto, resultado em R, expectativa, fator de
lucro e pior sequência. Quando stop e alvo caem na mesma barra, assume-se o
stop. Rode isso antes de operar dinheiro real — o 3:1 é o filtro de entrada, não
uma promessa de resultado.

## Robô HFT (conta real)

O diretório [`hft/`](hft/) traz o segundo robô, de alta frequência, com
adaptadores de corretora, gestão de risco com kill-switch e simulador realista
de custos. Leia [docs/HFT.md](docs/HFT.md) — inclusive a parte sobre o que é e o
que **não** é possível para uma conta retail.

```bash
python3 -m hft validate --spreads 0.0,0.2,0.4,0.6,1.0   # onde deixa de pagar os custos
python3 -m hft backtest --ticks-csv data/EURUSD_ticks.csv
python3 -m hft paper --seconds 600 -v                    # preços reais, ordens simuladas
```

### Camada institucional

Para quem vai além da corretora retail, o mesmo pacote traz FIX 4.4, livro L2,
market making com controle de inventário e medição de latência tick-to-trade:

```bash
python3 -m hft mm --count 20000 --rebate 2.5   # market making + seleção adversa
python3 -m hft latency --count 50000           # p50/p99/p99.9 do caminho crítico
```

[docs/HFT_INSTITUCIONAL.md](docs/HFT_INSTITUCIONAL.md) separa o que é código
(entregue) do que é colocation, membership e licença de dados (não é código).

## Testes

```bash
python3 -m unittest discover -s tests -t .
```

## Estrutura

```
robo_forex/
  analysis/     pivots, zonas, order blocks, linhas de tendência, confluência
  feeds/        Yahoo (stdlib), CSV, sintético offline
  strategy.py   monta o setup e aplica os filtros
  risk.py       entrada, stop, alvo, RR e tamanho de posição
  news.py       calendário econômico e janela de bloqueio
  backtest.py   validação barra a barra
  plot.py       gráfico SVG
  cli.py        linha de comando

hft/
  engine.py     tick -> features -> estratégia -> risco -> corretora
  strategies.py mean_reversion e imbalance_momentum
  risk.py       kill-switch, limites diários e trava de vantagem
  brokers/      paper, OANDA (REST) e MetaTrader 5
  fix/          FIX 4.4: mensagens, sessão, market data e ordens
  orderbook.py  livro L2 com microprice e custo de varredura
  marketmaker.py cotação de dois lados com controle de inventário
  latency.py    histogramas p50/p99/p99.9 por estágio
  lowlat.py     ring buffer, pool, controle de GC e afinidade de CPU
```

## Aviso

Ferramenta de análise, para fins educacionais. Não é recomendação de
investimento. Opere em conta demo até ter estatística própria suficiente.
