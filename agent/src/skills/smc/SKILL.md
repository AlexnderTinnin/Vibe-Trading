---
name: smc
description: Smart Money Concepts (ICT) signal engine with point-in-time-safe swing confirmation for BOS, ChoCH, and FVG analysis.
category: strategy
---
# Smart Money Concepts

## Purpose

The modern institutional trading school (ICT / Inner Circle Trader), built around the core idea of tracking the behavior of "smart money" (institutional capital):

| Concept | English | Meaning |
|------|------|------|
| Break of structure | BOS (Break of Structure) | Trend continuation signal |
| Change of character | ChoCH (Change of Character) | Trend reversal signal |
| Fair value gap | FVG (Fair Value Gap) | Price refill target |
| Order blocks | Order Blocks | Institutional order concentration zones |
| Liquidity | Liquidity | Stop-hunt target zones |

## Signal Logic

Direction from confirmed swing breaks (BOS/ChoCH) with causal FVG filtering:
- **Long**: bullish ChoCH/BOS with no conflicting bearish FVG
- **Short**: bearish ChoCH/BOS with no conflicting bullish FVG
- **Stand aside**: no structural signal or conflicting directions

## Dependencies

```bash
pip install pandas numpy requests
```

## Parameters

| Parameter | Default | Description |
|------|--------|------|
| swing_length | 10 | Window for swing high/low detection |
| close_break | True | Whether BOS/ChoCH requires a closing-price break |

Swing points are confirmed only after `swing_length` later bars have closed.
The signal is emitted on the confirmation/break bar and is never written back
to the earlier pivot bar, preventing lookahead bias in backtests.

## Signal Convention

- `1` = long, `-1` = short, `0` = stand aside
