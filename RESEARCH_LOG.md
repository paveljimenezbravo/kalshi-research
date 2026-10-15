# Research log

Every idea gets a line, including the ones that failed. If you try enough ideas,
one will look good by luck, so the log shows how many were tried. A lead only
counts once it holds up on data it was never tuned on.

Results are per $1 contract after fees. Brackets are the 95% range. "Older/newer"
compares the first 70% of the data with the last 30%.

| # | Date | Idea | Data | Result | Status |
|---|---|---|---|---|---|
| 1 | Oct 13 | Buying the side priced 55–65¢ wins more than 61% of the time (the 60% / bet-20% thesis) | BTC, 10–14k windows | won 59.8%, needed 61.3%: −1.5¢ | Dead: priced in |
| 2 | Oct 13 | Same at 65–75¢ and 70–80¢ | BTC | −1.4¢ and −0.8¢ | Dead |
| 3 | Oct 13 | A fair-value model (btc_mc.py replay, walk-forward) finds mispriced minutes | BTC Jun–Oct, minute data | −0.9¢. Kalshi's prices were more accurate than the model (Brier 0.1536 vs 0.1565) | Dead at minute speed |
| 4 | Oct 13 | Bid instead of paying the ask (maker), counting only fills where the price fell through you | BTC, 14k windows | −1.0 to −2.1¢; favorites −0.1¢ | Inconclusive: fill rule too pessimistic |
| 5 | Oct 13 | Thinner copies (ETH/SOL/XRP/DOGE 15-min) are priced worse | ~5.7k windows each | Long shots overpriced everywhere; nothing beats costs reliably | Dead for taking; maker within ±1¢ |
| 6 | Oct 13 | Kalshi lags BTC, so a fast bot can buy before it catches up | Recorder, 4 windows (1 with Binance) | Against Coinbase+Kraken, ~52% of each move shows up in Kalshi before our feed sees it. Against Binance futures (the fastest feed we can get), 60% shows up in the same quarter second, 93% within 0.5 s, and a third before our Binance feed sees it. | Dead for a home PC |
| 7 | Oct 13 | Some kinds of takers are reliably uninformed (markouts) | Recorder, 172k trades, 3 windows | Makers earn about +0.2¢ per contract overall; no clear losing group yet | Needs 1–2 days of recording |
| 8 | Oct 13 | Pricing is sloppier overnight or on weekends | BTC May–Oct, then Dec–May out of sample | In sample: model +0.65¢ on weeknights 0–8 ET [−1.0, +2.3]; the other 11 cells were negative. **Out of sample (13,810 windows): +0.44¢ [−1.41, +2.28]**, again the only positive cell. | Not proven: same direction, but far from the +0.5¢-with-the-range-above-zero bar. Watch, don't trade. |
| 9 | Oct 13 | Traders overbet YES (Bartlett & O'Hara 2026) | All 5 markets, then BTC Dec–May out of sample | In sample: BTC YES takers lose ~1.2¢ more than NO takers in every zone. **Out of sample: reversed. YES takers did better in all 4 zones**, and maker buying in every zone lost (−0.2 to −1.9¢). | Dead. It looks regime-dependent, not a YES bias. |
| 14 | Oct 13 | (Post-hoc, from #9) Traders overpay for the side of the recent BTC trend | — | Not tested. Even if true, counter-trend maker buying lost money in both periods (minute data), so it's unlikely to pay. | Parked |
| 10 | Oct 13 | Hourly "above $X" vs the 15-min window ending on the hour (same settlement) | Recorder, 1 window so far | Nearest strike ($3.92 from target): never a locked-in profit; best moment −0.76¢ after both fees. Before fees the prices disagreed by up to ~2.7¢. | Running; likely no taker arbitrage |
| 11 | Oct 13 | Kalshi's ETH market lags ETH or BTC prices | Recorder | pending | Running |
| 12 | Oct 13 | A $20 order can't actually fill at the best price (depth) | Recorder books | BTC: median 4,500 contracts at the best ask; 99% of $20 orders fill at the best price. ETH: median 159; 99th-percentile slippage 1.2¢. | Not a problem at our size |
| 13 | Oct 13 | Queue-aware maker test (back vs front of the line), recorder trades | 2 windows | Too little data | Running nightly in the morning report |
| 15 | Oct 15 | Buy the heavy favorite (80-99c) late in the window: "87% win rate, +14% per win" | BTC Dec-Oct, 28,095 windows, first qualifying minute per window, taker at the ask | Win rates match the prices almost exactly (85-90c bucket: 86-88% wins). Last 3 minutes, 85-90c: **-1.61c per contract [-2.77, -0.45]**; 80-85c: -1.84c; 90-95c: -0.95c. No cell of 12 reaches +0.5c with a range above zero. | Dead. The price already is the probability; the fee makes it negative, and in the last 3 minutes it's reliably negative. |
| 16 | Oct 15 | Weather: a forecast-based probability (NBM+GFS+ECMWF average, walk-forward 90-day error model per city) beats Kalshi's daily-high brackets | 7 cities (NY, CHI, MIA, AUS, LAX, DEN, PHIL), Jan 2025-Oct 2026; in sample before 2026-04-01, out of sample after; taker at the hourly quote; decision A = 12:00 UTC on the day (forecast issued ~1 day earlier), B = 18:00 UTC the day before (~2 days earlier); margins 3/5/10c after the fee, chosen in sample | pending | Running |
| 17 | Oct 15 | Chart indicators call BTC's next move: TTM/LazyBear squeeze (BB 20,2 inside KC 20,1.5), ADX>25 with +DI/-DI, plain momentum | Coinbase 1-min BTC Dec 2025-Oct 2026; 5-min, 15-min and 1-hour bars; next 15 min to 4 h; split into halves (`indicator_test.py`) | Squeeze direction right 43-51%, no consistent edge in either half; the move after it fires is x0.95-1.11 of normal size (no reliable expansion). ADX: 46-52% right, moves within noise. Momentum: right 47-49%, i.e. a slight reversal. | Dead as direction calls |
| 18 | Oct 15 | (From #17) Bet that each 15-min window reverses the previous one | Kalshi KXBTC15M, 28,557 back-to-back windows; taker at minute 1, 2 or 5 | Real: windows reverse 51.8% (52.4% / 51.1% by half). But Kalshi already prices it: the reversal side costs ~51.3c at minute 1; after the ~2c fee, -0.6c [-1.6, +0.3] in the 1st half and -2.3c [-3.1, -1.5] in the 2nd. Minutes 2 and 5 also negative. | Dead: priced in, and the effect is shrinking |

## Acceptance criteria (written before seeing results)

A strategy is a candidate for paper trading only if all of these hold:
1. **The edge after all costs is above +0.5¢ per contract**, and the 95% range excludes zero.
2. **It holds on data it was never tuned on**: a later period, or another coin where that makes sense.
3. **Enough data.** At least 100 windows for markout tests and at least 1,000 windows for results-at-settlement tests.
4. **It survives realistic execution**: our latency, joining the back of the line, and $20 orders.
5. **The bear case below is answered in writing.**

Then two weeks of paper trading (logged before outcomes are known, settled afterwards) must agree before any real money is used. The human approves every order.

## Bear cases for the open leads

**#8, the model on weeknights 0–8 ET (+0.65¢):**
1. It was 1 positive cell out of 12. With that many cells, one positive result is roughly what luck alone would give.
2. Its 95% range includes zero.
3. Overnight books are thinner, so real fills may be worse than the minute snapshots suggest.

Test: Dec–Jun history, which it has never seen.

**#9, YES overbetting (BTC YES takers lose ~1.2¢ more than NO takers):**
1. A trend in BTC over the sample could favor one side. YES won 50.2% of BTC windows, so this is probably not the cause, but Dec–Jun will tell.
2. ETH and XRP don't show it, so it may not be a general rule.
3. Even if it's real, a maker buying NO gets about +0.1¢: the bias is roughly the size of the spread and adverse selection.

Test: Dec–Jun history, plus the queue-aware maker test (#13) restricted to the NO side.

## Blind-spot review (Oct 13, overnight)

| Blind spot | Why it mattered | Status |
|---|---|---|
| Speed was judged against slow price feeds (Coinbase/Kraken spot) | Kalshi moves about half of each BTC move *before* those feeds see it, so its bots watch something faster | Fixed: Binance futures are now recorded. Re-test in the morning report. |
| Fees were assumed, not checked | Maker results depend on them | Checked: these series use the standard fee type, where makers pay 0 and takers pay 0.07×P×(1−P) |
| The maker test posted blindly and never cancelled | Real makers quote around fair value and cancel when BTC moves | Partly fixed: queue-aware test (#13). Next: quote only when the bid is below fair value. |
| Depth at the best price was ignored | $20 orders might not fill at the quoted price | Checked: not a problem at our size (#12) |
| No acceptance criteria and no count of tries | With many tries, something looks good by luck | Fixed: the criteria above, and this log |
| Battlefield: BTC 15-min is Kalshi's most automated market | Edges are most likely where analysis beats speed (thinner or slower markets) | Open: worth a separate look if BTC stays dead |
| Kalshi's settlement index is a weighted median of several exchanges; we use 2–3 of them | Final-minute pricing depends on it | Open |
| Execution facts (queue priority, resting orders before the open, real latency) can't be measured without an account | The maker edge depends on them | Open: needs paper trading on Kalshi's demo exchange |

## Next

- A realistic maker test on the recorder's trade-by-trade data: a place in line at
  the price you joined, filled by the actual trades at that price. Focus on #9
  (sell YES / buy NO).
- Re-test #8 on the Dec–Jun windows once they're downloaded.
- Re-run `live_edge.py` and `flow.py` after a day or two of recording.
