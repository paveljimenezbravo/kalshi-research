# Edge report, 2026-10-13 03:36

Recorded so far: 2.4 hours. Research history and leads: RESEARCH_LOG.md.

## 1. Speed: does Kalshi lag BTC?

- **Coinbase + Kraken spot**, 9 windows: Kalshi reflects 59% of a move in the same quarter second, 93% within 0.5 s, 95% within 1 s. Share already moved BEFORE this feed: 48%.

```
 threshold_c  latency_s  signals  fills       mark10       mark30       mark60       settled
         3.0       0.25      433    375 -1.28 ± 0.81 -0.71 ± 0.93 -0.44 ± 1.58 -2.37 ± 15.30
         3.0       1.00      433    320 -1.11 ± 0.83 -0.81 ± 1.04 -0.45 ± 1.43 -2.73 ± 14.76
         5.0       0.25      298    251 -0.84 ± 0.72 -0.44 ± 1.44 -0.12 ± 2.81 -0.42 ± 19.15
         5.0       1.00      298    209 -0.96 ± 0.92 -0.66 ± 1.97 -0.55 ± 4.21 -1.22 ± 19.37
```

- **Binance futures (fastest)**, 6 windows: Kalshi reflects 58% of a move in the same quarter second, 83% within 0.5 s, 84% within 1 s. Share already moved BEFORE this feed: 19%.

```
 threshold_c  latency_s  signals  fills       mark10       mark30        mark60        settled
         3.0       0.25      177    142 -0.54 ± 2.07 +1.79 ± 2.58  +3.11 ± 2.78 +11.43 ± 10.72
         3.0       1.00      177    116 -0.79 ± 1.04 +0.79 ± 1.45  +2.06 ± 2.73   +9.12 ± 8.92
         5.0       0.25       75     60 -0.13 ± 3.44 +0.82 ± 2.92  +3.03 ± 7.76 +14.11 ± 13.24
         5.0       1.00       75     49 -0.59 ± 1.90 -0.72 ± 9.55 +1.53 ± 17.27 +13.01 ± 23.87
```

## 2. Hourly vs 15-minute (same settlement number)

2 top-of-hour windows. Nearest strike to each target. best_c > 0 means a locked-in profit was available after both fees. maker_* = the same pair with the 15-minute leg as a free resting order (it still has to get filled, and the hourly price can move before we hedge):

```
                 window   strike  vs_target  share_profitable  best_c  median_c  maker_share  maker_best_c
KXBTC15M-26OCT130200-00 83499.99      -3.92               0.0    0.08     -5.14          0.0          1.59
KXBTC15M-26OCT130300-00 83599.99     -28.92               0.0   -0.78    -16.98          0.0         -0.57
```

## 3. Who loses money (maker markouts, cents per contract)

365,242 BTC trades over 11 windows.

```
       all  trades  contracts          10s          30s          60s
all trades  365242   30781342 +0.23 ± 0.11 +0.31 ± 0.23 +0.36 ± 0.39
```

```
     taker  trades  contracts          10s          30s          60s
 bought NO  174168   15211047 +0.44 ± 0.29 +0.58 ± 0.75 +1.44 ± 1.42
bought YES  191074   15570296 +0.04 ± 0.28 +0.05 ± 0.78 -0.61 ± 1.44
```

## 4. Maker test with a real place in line (cents per contract, maker fee 0)

`back` = joined the back of the line (realistic for us), `front` = first in line (upper bound). mark60 = Kalshi's own price 60 s after the fill minus what we paid.

**KXBTC15M**

```
      line side             zone  posts  filled  windows     mark10     mark30     mark60      settled
      back  YES        all 5-95c    378     316        9 -0.53±0.62 -0.40±2.12 -0.51±3.41  +4.05±24.11
      back  YES favorites 70-95c     89      64        6 -0.47±1.79 -1.24±2.79 -0.69±6.09 -11.12±31.86
      back  YES long shots 5-30c    160     148        6 -0.14±1.76 -0.19±4.95 -1.29±6.17  -3.85±37.12
      back   NO        all 5-95c    382     336        9 -0.78±0.88 -1.08±1.82 -1.78±3.13  -5.01±24.28
      back   NO favorites 70-95c    120      99        6 -0.11±1.10 +0.60±2.99 +1.73±5.58  +2.22±37.08
      back   NO long shots 5-30c    127     123        6 -0.51±0.96 -1.14±2.35 -1.59±2.74  +6.89±31.50
     front  YES        all 5-95c    556     547        9 +0.49±0.51 +0.91±1.56 +1.32±3.11  +6.83±24.79
     front  YES favorites 70-95c    149     146        6 +0.78±1.04 +1.23±1.57 +1.48±4.25  -9.04±31.62
     front  YES long shots 5-30c    209     209        6 +0.28±1.06 +0.21±3.68 -0.02±6.42  -1.95±37.35
     front   NO        all 5-95c    572     568        9 +0.21±0.54 -0.05±1.59 -0.53±3.00  -6.20±24.21
     front   NO favorites 70-95c    200     199        6 +0.58±0.95 +0.42±4.08 +1.04±6.61  +3.63±37.26
     front   NO long shots 5-30c    158     157        6 -0.22±0.60 -0.84±2.46 -1.31±2.93  +5.26±31.98
back+guard  YES        all 5-95c    285     237        6 -0.52±0.85 -0.15±2.83 -0.11±4.33  +5.36±34.81
back+guard  YES favorites 70-95c     52      40        4 -0.47±1.34 -2.68±6.19 -2.19±8.94 -32.74±47.81
back+guard  YES long shots 5-30c    131     116        5 +0.06±2.45 +0.33±6.23 -0.39±7.32  -1.75±43.08
back+guard   NO        all 5-95c    277     231        6 -0.76±1.29 -0.06±2.21 -0.57±3.59  -0.25±34.96
back+guard   NO favorites 70-95c    102      83        5 -0.55±1.18 +0.49±3.76 +0.47±5.34  -0.29±43.61
back+guard   NO long shots 5-30c     71      66        4 -0.25±1.70 -0.38±4.01 +0.00±7.75 +22.85±47.78
```

**KXETH15M**

```
      line side             zone  posts  filled  windows      mark10      mark30      mark60      settled
      back  YES        all 5-95c    291     243        8  -1.01±0.77  -1.54±2.23  -1.99±4.12  +3.25±25.67
      back  YES favorites 70-95c     79      56        5  -1.90±3.05  -3.19±6.42  -1.41±6.88  -5.42±39.21
      back  YES long shots 5-30c     98      88        6  -0.85±1.95  -1.11±7.56 -2.26±11.49 -10.02±35.56
      back   NO        all 5-95c    277     230        8  -1.08±0.60  -1.72±1.60  -1.41±3.14  -3.74±24.45
      back   NO favorites 70-95c     84      68        6  -0.36±8.47 +0.06±22.79  +1.93±9.99 +11.46±35.46
      back   NO long shots 5-30c     92      78        5  -1.12±1.25  -1.06±5.48  -2.37±4.63  +4.88±39.39
     front  YES        all 5-95c    352     333        8  -0.14±0.70  -0.07±1.82  -0.65±3.79  +3.64±25.87
     front  YES favorites 70-95c    102      94        5  -0.00±2.25  +0.11±4.85  +1.33±5.89  -2.62±38.81
     front  YES long shots 5-30c    118     116        6  -0.15±4.12  -0.33±8.10 -1.52±10.46 -10.02±35.49
     front   NO        all 5-95c    348     332        8  -0.24±0.50  -0.71±1.45  -0.43±2.92  -3.43±24.90
     front   NO favorites 70-95c    111     105        6  +0.22±8.60 +0.45±22.76 +2.40±10.12 +11.10±35.35
     front   NO long shots 5-30c    109     106        5  +0.11±2.05  +0.01±5.80  -1.57±4.76  +3.33±39.18
back+guard  YES        all 5-95c    248     184        6  -0.73±1.15  -2.05±2.60  -2.37±4.79  +2.37±31.03
back+guard  YES favorites 70-95c     66      38        4  -0.99±3.08  -3.15±7.32  -1.55±6.94 -11.69±48.38
back+guard  YES long shots 5-30c     79      68        5 -0.09±13.19 -0.72±23.55 -2.43±10.22  -9.52±41.14
back+guard   NO        all 5-95c    239     181        6  -0.71±0.77  -0.71±1.77  +0.05±3.26  -0.47±31.40
back+guard   NO favorites 70-95c     66      50        4  +0.17±2.82  +0.74±7.31 +1.39±12.29 +11.35±42.30
back+guard   NO long shots 5-30c     71      53        4  -1.04±1.10  +0.18±5.89  -0.08±6.17 +13.00±50.36
```

## 5. Checks

```
        check              result                                                                                      note
 no-lookahead                pass                  signals use only data at or before the decision time; results only score
        costs                pass                 taker fee 0.07×P×(1−P); makers pay 0 on these series (fee_type quadratic)
    liquidity                pass BTC median 4,500 contracts at the best price; $20 fills at the best price 99% of the time
  sample-size             not yet              10 BTC windows recorded; markout verdicts need 100+, settled verdicts 1,000+
out-of-sample see RESEARCH_LOG.md                           a lead counts only after it holds on data it was never tuned on
    bear-case                 n/a                        written for each lead in RESEARCH_LOG.md before anything is traded
```

## 6. Decision

**No trade.** Nothing has met the acceptance criteria in RESEARCH_LOG.md (an edge whose 95% range is above +0.5¢ per contract after costs, on data it wasn't tuned on, then two weeks of paper trading). This report recommends; it never places orders.
