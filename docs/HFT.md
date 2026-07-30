# Robô HFT para conta real — opções, limites e como usar

## Antes de tudo: o que é possível e o que não é

Você pediu um robô HFT para conta real "que gere lucros legais". Preciso ser
direto em duas coisas, porque elas mudam o projeto inteiro:

**1. Ninguém pode te garantir lucro — nem eu, nem código nenhum.** O que dá para
construir é um robô com vantagem *mensurável*, custos *explícitos* e risco
*controlado*. Se a vantagem não superar o custo, o robô perde dinheiro mais
rápido quanto mais rápido ele operar. Por isso este projeto inclui o comando
`validate`, que mostra o ponto exato em que a estratégia deixa de pagar os
custos. É a peça mais importante do pacote.

**2. "HFT de verdade" não existe em conta retail.** HFT institucional é
colocation no datacenter da bolsa, feed direto, FIX, FPGA e latência de
microssegundos. Custa seis dígitos por mês. Sua conta, via MT5 ou API REST, tem
latência de 20 a 200 ms. Nessa faixa você **não** compete em velocidade — o que
sobra é operar padrão de curto prazo com custo baixo, que é o que este robô faz.

Com isso claro, aqui estão as opções reais.

## As três opções

| | **A. Scalping automatizado em corretora forex** | **B. Market making em cripto** | **C. HFT institucional** |
| --- | --- | --- | --- |
| Latência | 20–200 ms | 5–50 ms (VPS perto da exchange) | µs |
| Custo por operação | spread 0,2–1,0 pip + comissão ~US$7/lote | taxa maker 0,00%–0,02%, algumas com rebate | rebates de bolsa |
| Capital mínimo | US$ 500–2.000 | US$ 500 | US$ 1M+ |
| Dados de tick | pagos ou da própria corretora | **grátis via WebSocket**, livro completo | direto da bolsa |
| Barreira | corretora precisa permitir scalping/robô | risco de custódia e de exchange | infraestrutura |
| Implementado aqui | **sim** (OANDA + MT5) | arquitetura pronta, falta o adaptador | fora de alcance |

**Minha recomendação, na ordem:**

1. **Comece pela opção A em conta demo** com o que já está implementado. Os
   adaptadores de OANDA e MT5 funcionam, o risco tem kill-switch e o relatório
   já mostra a decomposição bruto/custo/líquido.
2. **Se quiser frequência de verdade, a opção B é a única viável com pouco
   capital**: dado de livro é grátis, a taxa maker é ordens de magnitude menor
   que o spread do forex e o mercado roda 24/7. O motor deste projeto (features,
   estratégia, risco, engine) é o mesmo — só falta escrever
   `hft/brokers/binance.py` seguindo o protocolo `Broker`. Se quiser, eu escrevo.
3. **Opção C**: não vale citar como possibilidade. Não é questão de código.

## O que está implementado

```
hft/
  models.py      Tick (com microprice e desequilíbrio de livro), posição, trade, conta
  features.py    janela deslizante: z-score, volatilidade, momento, fluxo, ticks/s
  strategies.py  mean_reversion e imbalance_momentum
  risk.py        travas pré-trade e kill-switch
  engine.py      tick -> features -> estratégia -> risco -> corretora (com latência)
  brokers/       paper (simulado), oanda (REST v20), mt5 (MetaTrader 5)
  ticks.py       ticks sintéticos, CSV de ticks, conversão de candles
  report.py      decomposição bruto/comissão/líquido e veredito
  cli.py         backtest, validate, paper, live, init-config
```

### As duas estratégias

**`mean_reversion`** (padrão) — o preço se afasta `entry_z` desvios da média da
janela **sem** que o livro confirme a direção → aposta na volta. É a que melhor
tolera latência: você não disputa velocidade, espera o exagero.

**`imbalance_momentum`** — desequilíbrio persistente do livro na mesma direção do
movimento → segue o fluxo por poucos segundos. Depende de dado de livro de
qualidade; no forex retail o "volume" é sintético, então prefira a primeira.

### A trava que mais importa

Toda entrada passa por:

```
vantagem_estimada_bps  >=  custo_round_turn_bps  ×  min_edge_multiple
```

Com spread de 0,6 pip e comissão de US$3,50 por perna, o custo do round-turn é
**1,50 pip**. A estratégia precisa capturar mais que isso, em média, por
operação. Se não capturar, o robô simplesmente não opera — e isso é uma
funcionalidade, não um defeito.

### Kill-switch

O robô para sozinho pelo resto do dia quando qualquer um destes estoura:
perda diária (`max_daily_loss_percent`, padrão 2%), drawdown
(`max_drawdown_percent`, 5%) ou sequência de perdas
(`max_consecutive_losses`, 5). No dia seguinte ele volta com o limite diário
zerado. Além disso: spread máximo, teto de operações por hora e por dia,
intervalo mínimo entre operações, janela de sessão e bloqueio por notícia
(reaproveitando o calendário do robô de análise).

## Como usar

```bash
# 1) Backtest com ticks sintéticos — testa o motor e mostra os custos
python3 -m hft backtest --count 40000 --no-news

# 2) O comando que importa: onde a estratégia deixa de pagar os custos
python3 -m hft validate --spreads 0.0,0.2,0.4,0.6,1.0 --no-news

# 3) Backtest com os seus ticks de verdade (time,bid,ask[,bid_size,ask_size])
python3 -m hft backtest --ticks-csv data/EURUSD_ticks.csv

# 4) Papel: preços ao vivo da corretora, ordens simuladas
python3 -m hft paper --seconds 600 -v

# 5) Conta demo
python3 -m hft -c config/hft.yaml live --seconds 3600

# 6) Conta real (exige confirmação explícita)
python3 -m hft -c config/hft.yaml live --confirmo-conta-real
```

Saída real do `validate` (ticks sintéticos com reversão de 0,05 e sem comissão):

```
  spread   custo/op    ops  pips médios    líquido  veredito
    0.00       0.20    120       +0.813    +106.52  paga
    0.20       0.40    120       +0.615     +80.29  paga
    0.40       0.60    117       +0.692     +88.24  paga
    0.60       0.80     39       +0.746     +31.17  paga
    1.00       1.20      0       +0.000      +0.00  não paga
```

Leia assim: com spread de 1 pip, a trava de vantagem barra todas as entradas —
o robô fica parado em vez de doar dinheiro. **É esse tipo de tabela que você
precisa gerar com dados reais do seu ativo antes de operar.**

## Configuração

```bash
python3 -m hft init-config --out config/hft.yaml
```

Os campos que decidem o resultado:

| Chave | Padrão | Por quê |
| --- | --- | --- |
| `costs.commission_per_lot_per_side` | `3.5` | ajuste para a **sua** corretora |
| `costs.extra_slippage_pips` | `0.1` | derrapagem média por perna |
| `costs.latency_ms` | `60` | latência real até o servidor |
| `strategy.min_edge_multiple` | `1.5` | quanto a vantagem deve superar o custo |
| `strategy.take_profit_pips` / `stop_pips` | `1.2` / `2.4` | alvo e stop do scalp |
| `risk.max_spread_pips` | `1.0` | não opera com spread alargado |
| `risk.risk_percent_per_trade` | `0.25` | risco por operação |
| `risk.max_daily_loss_percent` | `2.0` | kill-switch diário |
| `broker.provider` / `broker.mode` | `paper` / `demo` | corretora e ambiente |

**Segredos nunca vão para o arquivo.** O token da OANDA vem de
`OANDA_TOKEN` e a senha do MT5 de `MT5_PASSWORD` (nomes configuráveis em
`broker.token_env` / `broker.mt5_password_env`).

```bash
export OANDA_TOKEN="seu-token"
python3 -m hft -c config/hft.yaml live --seconds 3600
```

## Ligando na OANDA

1. Crie a conta demo em `fxpractice.oanda.com` e gere o token na área de API.
2. `export OANDA_TOKEN="..."`.
3. No `config/hft.yaml`: `broker.provider: oanda`, `broker.mode: demo`,
   `broker.account_id: "101-001-XXXXXXX-001"`.
4. `python3 -m hft -c config/hft.yaml paper --seconds 300 -v` — confere se os
   preços chegam antes de qualquer ordem.
5. `... live --seconds 3600` para a demo. Só depois de semanas de estatística
   própria troque `mode` para `live` e use `--confirmo-conta-real`.

## Ligando no MetaTrader 5

Só Windows: `pip install MetaTrader5`, terminal aberto, "Algo Trading" ligado.
No config: `broker.provider: mt5` e `instrument.broker_symbol` com o nome exato
do símbolo na sua corretora (varia: `EURUSD`, `EURUSD.a`, `EURUSDm`...).

## Checklist antes de arriscar dinheiro real

1. `validate` com **ticks reais** do seu ativo, no seu horário de operação.
2. Custos do config iguais aos da sua corretora (spread médio real, comissão,
   swap) — não os padrões daqui.
3. Pelo menos 200 operações em conta demo, com o mesmo VPS/latência da real.
4. Confirme com a corretora que scalping e robô são permitidos (algumas punem
   operação de segundos com requote ou encerramento de conta).
5. Comece com o mínimo de lote e `max_daily_loss_percent` baixo.
6. VPS perto do servidor da corretora — 100 ms de latência já corrói o alvo de
   1,2 pip.

## Limitações honestas deste robô

- O backtest de ticks sintéticos **não prova lucro**: prova que o código funciona
  e mostra a matemática dos custos.
- `ticks_from_candles` aproxima o caminho dentro da barra; serve para depurar,
  não para validar.
- O modelo de custo é linear: não simula alargamento de spread em notícia,
  requote nem execução parcial — na prática isso piora o resultado.
- Sem reconexão automática em queda de rede. Se a sessão cair com posição
  aberta, o TP/SL registrados na corretora protegem, mas o robô não reassume.
- Swap/overnight não entra na conta (`swap_per_lot_per_day` existe mas não é
  aplicado, porque a estratégia fecha em segundos).
