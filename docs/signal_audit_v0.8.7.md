# Signal Audit — v0.8.7 / v0.9.0 cut-over (2026-04-23)

```
============================================================
Signal Audit - C:\Users\ADMIN\Saved Games\Polymarket_bot\data\analyses.jsonl
Total analyses: 227
============================================================

Decision distribution:
  SKIP        208   91.6%
  NO           10    4.4%
  YES           9    4.0%

Confidence distribution:
  Medium      136   59.9%
  Low          53   23.3%
  High         24   10.6%
  UNSET        14    6.2%

Top 10 reasoning strings:
  [ 22x] base rate 50% and no relevant news
  [ 21x] lack of relevant news and high market volume
  [  8x] base rate 50%, no relevant news, high volume
  [  6x] base rate 50%, no relevant news, high volume implies efficient price
  [  5x] no relevant news, high volume market implies efficient pricing
  [  4x] no relevant news, high volume market
  [  4x] no relevant news, high volume market, no edge
  [  4x] no relevant news, high volume implies efficient price
  [  3x] lack of relevant news and high volume
  [  3x] base rate 50%, no relevant news

Average edge by confidence:
  High       avg_edge=0.0825  n=24
  Low        avg_edge=0.0493  n=53
  Medium     avg_edge=0.0068  n=136

Average probability by decision:
  NO         avg_prob=0.1220  n=5
  SKIP       avg_prob=0.1171  n=208

Rejection reasons (from risk_log.jsonl, total 225):
  [ 154]  Claude returned SKIP
  [  69]  Confidence below threshold
  [   2]  Insufficient edge

Markets that passed risk gate (trades.jsonl): 11
  real:      5
  synthetic: 6
  2026-04-19T14:57:44  1733817  YES  $8.1962
  2026-04-19T15:05:13  1733817  YES  $8.1962
  2026-04-20T03:01:49  1540766  YES  $2.8654
  2026-04-20T03:04:06  1540766  YES  $1.604
  2026-04-20T03:21:53  1611267  YES  $2.8213

Confidence rate by category:
  category       n    Low%    Med%   High%
  other         88    1.1%   98.9%    0.0%
  unknown      139   37.4%   35.3%   17.3%

Top 5 days by analysis count:
  2026-04-19   57
  2026-04-20   40
  2026-04-23   40
  2026-04-22   38
  2026-04-21   28

Distinct days: 8
```
