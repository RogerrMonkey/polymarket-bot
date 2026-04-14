# USDC On/Off-Ramp Operational Runbook (INR -> USDC -> Polygon)

This runbook is required before any live deployment.

## Scope
- Audience: solo operator running the Polymarket-first bot.
- Objective: safe and repeatable INR to USDC to Polygon funding, plus reverse off-ramp.

## Gate Criteria
1. `usdc-check` passes with no failing mandatory controls.
2. Wallet address and provider references are configured in environment.
3. RPC endpoint is reachable from runtime host.
4. Daily transfer limit and max single transfer are defined.
5. A completed dry-run checklist is documented.

## Required Environment Variables
- `POLYGON_WALLET_ADDRESS`: 0x-prefixed wallet address (42 chars).
- `POLYGON_RPC_URL`: Polygon RPC URL.
- `USDC_ONRAMP_PROVIDER`: provider label (for example `manual-p2p`, `exchange-a`, `desk-b`).
- `USDC_OFFRAMP_PROVIDER`: reverse provider label.
- `USDC_DAILY_TRANSFER_LIMIT`: numeric daily transfer ceiling in USDC.
- `USDC_MAX_SINGLE_TRANSFER`: numeric per-transfer ceiling in USDC.
- `USDC_MIN_BUFFER`: minimum USDC reserve to keep in wallet.

## On-Ramp Procedure (INR -> USDC)
1. Confirm INR source account has available funds.
2. Acquire USDC through the configured provider.
3. Transfer USDC to `POLYGON_WALLET_ADDRESS`.
4. Wait for transfer confirmation and verify wallet balance.
5. Record transfer reference, fee paid, timestamp, and resulting balance.

## Off-Ramp Procedure (USDC -> INR)
1. Confirm current wallet balance and active open positions.
2. Ensure no critical trades are pending.
3. Transfer USDC from wallet to configured off-ramp provider.
4. Convert to INR and settle to bank account.
5. Record conversion rate, fees, settlement time, and final INR credited.

## Risk Controls
- Never exceed `USDC_DAILY_TRANSFER_LIMIT`.
- Never exceed `USDC_MAX_SINGLE_TRANSFER` per movement.
- Keep at least `USDC_MIN_BUFFER` in wallet for operations and gas safety.
- If any transfer fails or is delayed beyond expected SLA, pause live trading and switch to paper mode.

## Incident Handling
- Mismatch between expected and observed wallet balance: stop trading immediately.
- Provider outage or settlement delay: set risk kill switch and use paper mode.
- RPC instability: do not execute live capital movement until RPC checks pass.

## Logging Requirements
- Every transfer event should be logged with:
  - timestamp
  - direction (`onramp` or `offramp`)
  - provider
  - amount
  - fees
  - tx reference
  - post-transfer wallet balance

## Review Cadence
- Daily: reconcile wallet balance and transfer logs.
- Weekly: review provider reliability and effective fee rate.
- Monthly: rehearse full off-ramp and emergency pause runbook.
