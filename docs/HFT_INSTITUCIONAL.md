# Opção C — a camada institucional

Você pediu o C. Aqui está, com a separação que importa: **o que é código eu
entreguei; o que é dinheiro, contrato e papelada eu não tenho como entregar.**
Este documento diz exatamente onde fica cada linha dessa divisa.

## O que foi implementado

| Peça | Arquivo | O que é |
| --- | --- | --- |
| Motor FIX 4.4 | `hft/fix/message.py` | codificação/decodificação, BodyLength, CheckSum, grupos repetitivos, framing de stream TCP |
| Sessão FIX | `hft/fix/session.py` | logon, heartbeat, TestRequest, números de sequência, ResendRequest, GapFill, logout |
| Aplicação FIX | `hft/fix/broker.py` | assinatura de market data, snapshot/incremental, NewOrderSingle, ExecutionReport |
| Livro L2 | `hft/orderbook.py` | níveis agregados, microprice, desequilíbrio, custo de varrer o livro, fila |
| Market making | `hft/marketmaker.py` | cotação com preço de reserva e skew por inventário + simulador pessimista |
| Latência | `hft/latency.py` | histograma com p50/p99/p99.9 por estágio do caminho crítico |
| Runtime | `hft/lowlat.py` | ring buffer, pool de objetos, controle de GC, afinidade de CPU, busy-spin |

Tudo testado sem rede: 85 testes cobrem protocolo, sessão, livro, cotação e
runtime (`python3 -m unittest tests.test_fix tests.test_institucional`).

## O que **não** foi implementado — e por quê

Nada disso é limitação de código:

| Item | Custo real | Por que só você pode providenciar |
| --- | --- | --- |
| Colocation (LD4, NY4, TY3) | US$ 1–5 mil/mês por rack | contrato com Equinix ou o datacenter da bolsa |
| Cross-connect ao ECN | US$ 300–1.000/mês por linha | contrato bilateral |
| Membership / acordo de maker | US$ 5–50 mil/mês + garantias | negociação com a bolsa ou ECN |
| Licença de market data | US$ 1–20 mil/mês por feed | contrato de licenciamento |
| Prime broker | patrimônio mínimo, geralmente US$ 1M+ | análise de crédito |
| Certificação FIX | semanas de testes com a contraparte | processo com o suporte deles |
| Placa FPGA / kernel bypass | US$ 5–15 mil por placa | compra de hardware |

**Total realista de entrada: seis dígitos por mês, mais capital.** É esse o
motivo de eu ter dito que a opção C não era acessível — não é o software.

## O limite duro: Python não faz µs

Medição real deste projeto, no comando `python3 -m hft latency`:

```
* coletor de lixo desligado durante a sessão
estágio                n       p50       p99     p99.9       máx  (µs)
tick_to_trade      20000    309.22    544.79    625.78    736.15
```

Com o coletor de lixo ligado, o pico do mesmo teste sobe para **62.000 µs**
(62 ms). Ou seja: só desligar o GC reduziu a pior amostra em ~85×. Ainda assim,
**300 µs de mediana** é três ordens de grandeza acima do que HFT institucional
exige (single-digit µs, e nanossegundos no caso de FPGA).

Conclusão honesta: este código serve como **plataforma de pesquisa e
implementação de referência**. Para competir de fato, o caminho crítico
(decodificação do feed → livro → estratégia → wire) precisa ser reescrito em
C++ ou Rust. Os módulos que teriam que ser portados são exatamente
`fix/message.py`, `orderbook.py` e o laço de `engine.py` — a lógica de negócio,
o risco e a análise podem continuar em Python.

## O que o market making revela

Rode `python3 -m hft mm --count 20000 --rebate 0` e depois com `--rebate 2.5`:

```
sem rebate:   spread capturado -135.82 | rebates   +0.00 | LÍQUIDO -135.82
com rebate:   spread capturado -135.82 | rebates +143.50 | LÍQUIDO   +7.68
seleção adversa: -0.391 bps por execução
```

Essa tabela é o argumento inteiro da opção C em três linhas. O simulador só
executa a cotação quando o preço **atravessa** o nível — ou seja, quando o
mercado está indo contra você. Sem prioridade de fila (colocation) e sem rebate
de maker (membership), o market making perde dinheiro de forma estrutural. A
infraestrutura institucional não é luxo: é o que transforma esse número em
positivo.

## Como usar a camada FIX

```python
from hft.config import HftSettings
from hft.fix import FixBroker, FixConfig
from hft.engine import Engine
from hft.strategies import build_strategy

settings = HftSettings()
fix_config = FixConfig(
    host="fix.seu-ecn.com",
    port=443,
    sender_comp_id="SEU_ID",
    target_comp_id="ECN",
    username="usuario",
    password_env="FIX_PASSWORD",   # a senha vem do ambiente, nunca do arquivo
    heartbeat_interval=30,
    use_ssl=True,
)
broker = FixBroker(settings, fix_config)
engine = Engine(settings, broker, build_strategy(settings))
engine.run_live(max_seconds=3600)
```

O `FixBroker` implementa o mesmo protocolo `Broker` do adaptador de papel: a
estratégia não sabe se está falando com um simulador ou com um ECN.

Para testar a sessão sem rede, use o `LoopbackTransport`:

```python
from hft.fix import FixSession, LoopbackTransport

transport = LoopbackTransport()
session = FixSession(fix_config, transport)
session.connect()
transport.feed(resposta_de_logon_em_bytes)
session.poll()          # trata a sessão, devolve só mensagens de aplicação
```

## Caminho realista, em fases

1. **Fase 0 — agora**: `mm` e `latency` com dados sintéticos; entenda a
   matemática da seleção adversa. Custo: zero.
2. **Fase 1**: ECN com FIX acessível a cliente profissional (LMAX, Integral,
   CFH). VPS no mesmo datacenter (LD4). Latência de 1–5 ms. Custo: centenas de
   dólares por mês. É aqui que a maioria realista para.
3. **Fase 2**: reescrever o caminho crítico em C++/Rust mantendo este código
   como referência e como camada de pesquisa/risco. Latência de dezenas de µs.
4. **Fase 3**: colocation própria, feed direto, kernel bypass (Solarflare/
   Onload), FPGA. Latência de µs a ns. Custo: seis dígitos por mês.

Cada fase só faz sentido se a anterior tiver mostrado vantagem positiva depois
de custos. Pular fase é a forma mais cara de descobrir que a estratégia não
tinha vantagem nenhuma.

## Aviso final

Nenhuma linha deste repositório garante lucro. O que ela garante é que você vai
**ver o custo** antes de pagar por ele — no `validate`, no `mm` e no `latency`.
Um robô que mostra que não deve operar vale mais que um que promete retorno.
