# Resultados dos testes — leia antes de operar

Este documento existe porque a conclusão dos testes é **negativa**, e esconder
isso seria o pior serviço possível. Todos os números abaixo saem de comandos
que você pode repetir.

## Resumo em uma linha

**A estratégia de linha de oferta/demanda + order block, como está implementada
aqui, não demonstrou vantagem em 2 anos de dados. Nem ela, nem as três variações
testadas para consertá-la. Não opere com dinheiro real.**

## O que foi testado

Dados reais (Yahoo Finance), 10 pares de câmbio, custos de transação incluídos
(spread típico de conta retail + derrapagem de 0,2 pip).

```bash
python3 -m robo_forex backtest \
  -s EURUSD,GBPUSD,AUDUSD,NZDUSD,USDCAD,USDJPY,EURGBP,GBPJPY,GBPNZD,AUDJPY \
  --bars 13000 --warmup 400 --step 2 --no-news --no-session
```

| Teste | Período | Operações | Bruto | Custos | Líquido |
| --- | --- | --- | --- | --- | --- |
| H1, 50 dias | jun–jul 2026 | 175 | +27,8R | −20,5R | **+7,3R** |
| H1, 2 anos | jul 2024–jul 2026 | 2.132 | +38,5R | −218,7R | **−180,1R** |
| H4, 2 anos | jul 2024–jul 2026 | 553 | −55,2R | −30,8R | **−85,9R** |

Numa conta de US$ 1.000 arriscando 1% por operação, o teste de 2 anos em H1
termina com **US$ 155** — perda de 84%, com queda máxima de 89% no caminho.

O recorte de 50 dias parecia positivo. Era ruído: o lucro dependia de 3
operações; removendo apenas a melhor, já ficava negativo. **Amostra pequena não
é evidência.**

## Por que falha — o diagnóstico

| | H1 (2 anos) | H4 (2 anos) |
| --- | --- | --- |
| Acerto real | 20,8% | 18,6% |
| RR médio obtido nos ganhos | 3,88 | 3,83 |
| **Acerto necessário para empatar** | **20,5%** | **20,7%** |
| Veredito | empate exato | abaixo do empate |

Este é o ponto central. Um sistema que mira 3,9:1 empata acertando 1/(1+3,9) =
**20,5%** das vezes. O robô acerta 20,8%. Ou seja: **a taxa de acerto é
exatamente a que o acaso produziria com aquele alvo.** As zonas de oferta, os
order blocks e as linhas de tendência não adicionaram nada mensurável — o
resultado é indistinguível de entrar em posições aleatórias com alvo distante.

Em cima desse empate vêm os custos, e é aí que a conta morre: **1.066 operações
por ano × ~US$ 0,74 de pedágio = US$ 786 em dois anos**, numa conta de mil
dólares.

No H4 os custos pesam bem menos (−30,8R contra −218,7R), o que confirma a
matemática do custo. Mas o resultado bruto também é negativo, o que refuta a
hipótese de que "era só o custo": **não havia vantagem escondida sob os custos.**

## O que isso não significa

- Não significa que oferta/demanda e order block "não funcionam" em geral.
  Significa que **esta implementação, com estes parâmetros, nestes 10 pares,
  nestes 2 anos**, não teve vantagem.
- Não significa que o código está errado. Os 275 testes cobrem a mecânica; o
  backtest é conservador (stop na frente do alvo em empate, sem look-ahead).
  O que faltou foi vantagem na estratégia, não corretude no software.

## As três tentativas de conserto — todas reprovadas

As hipóteses listadas abaixo foram testadas **uma vez cada**, com separação
dentro/fora da amostra (`--oos 60`): os primeiros 60% do histórico para ajustar,
os 40% finais — nunca vistos por aquela configuração — para conferir. Só o
resultado de fora conta.

| Configuração | Operações | Acerto | Resultado | Por operação |
| --- | --- | --- | --- | --- |
| Linha de base (alvo 3:1+) | 833 | 20% | −95,8R | −0,115R |
| **H1 · alvo mais perto (1,5:1)** | 1.205 | **43%** | **−34,2R** | **−0,028R** |
| H2 · muito mais seletivo | 79 | 16% | −27,6R | −0,349R |
| H3 · vela de rejeição | 353 | 20% | −38,3R | −0,108R |

Cada uma falhou de um jeito diferente, e as três juntas fecham o diagnóstico:

**H1 (alvo mais perto) foi a única a produzir vantagem mensurável.** Com alvo em
1,5:1 o ponto de equilíbrio é 40% de acerto e o robô entregou 43% — cerca de
+0,075R de vantagem bruta por operação. O custo de transação é ~0,10R por
operação. **A vantagem existe e é menor que o pedágio.** Foi a melhor
configuração de todas (perda 4× menor por operação que a base), e ainda assim
negativa.

**H3 (vela de rejeição) cortou metade das operações e manteve os mesmos 20% de
acerto.** As operações eliminadas eram estatisticamente iguais às mantidas: o
filtro não distingue nada.

**H2 (seletividade extrema) é o exemplo didático de sobreajuste**: +7,7R dentro
da amostra, −27,6R fora. Sem a separação `--oos`, esta configuração teria sido
declarada "a solução" — e perderia dinheiro em conta real. É exatamente para
pegar esse erro que o `--oos` existe.

## O que seria honesto tentar a seguir

Cada item é uma **hipótese com razão a priori**, não pesca de parâmetro:

1. **Alvo mais próximo.** O `target_mode` atual busca a liquidez que entregue
   3:1 ou mais, o que empurra o alvo para longe e derruba a taxa de acerto ao
   nível do acaso. Testar alvo fixo em 1,5:1 é uma hipótese diferente e
   testável.
2. **Muito mais seletividade.** 3.471 sinais em 2 anos é operar demais. Exigir
   nota mínima bem maior, order block não mitigado e alinhamento com o tempo
   gráfico maior reduziria o número de operações — e o pedágio é proporcional a
   elas.
3. **Confirmação de rejeição** (`require_rejection_candle: true`), que já existe
   e não foi usada nestes testes.

**Regra para não se enganar:** teste cada hipótese **uma vez**, em dados
separados dos que você usou para formular a ideia (`--oos 60`). Se você testar
vinte variações e escolher a melhor, encontrou o passado, não uma vantagem. Uma
variação positiva depois de vinte tentativas é o resultado esperado do acaso.

As três hipóteses acima foram testadas e reprovadas. **Este documento não será
atualizado com uma quarta, quinta e sexta tentativa até alguma dar positivo** —
seria justamente a pesca que a regra proíbe. O caminho honesto a partir daqui
não é ajustar parâmetro: é procurar um sinal de entrada com poder de previsão
real, medido antes de virar estratégia. As zonas e os order blocks, medidos,
não têm esse poder.

## Como repetir

```bash
python3 -m robo_forex backtest -s EURUSD,GBPUSD --bars 13000 --warmup 400 --step 2
python3 -m robo_forex backtest -s EURUSD --timeframe 4h --htf 1d --bars 3200
```

Os custos entram por padrão (`costs` na configuração). Para ver o efeito, rode
com `costs.enabled: false` e compare — a diferença é o que a corretora ganha.
