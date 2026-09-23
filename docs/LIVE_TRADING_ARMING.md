# Live Trading Arming & Operational Playbook

> [!CAUTION]
> **HIGH RISK OPERATION**: Enabling live trading routes orders directly to Upstox with real capital. Ensure all pre-requisites are strictly satisfied before proceeding.

---

## 1. Pre-Requisites Checklist

Before arming live execution (`live_trading.py`), verify that:
- [ ] Paper dryrun (`hft-dryrun.service`) has run continuously for at least 30 trading days.
- [ ] Paper dryrun shows a positive net P&L and acceptable drawdown.
- [ ] Upstox API credentials (`UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_USERNAME`, `UPSTOX_PIN`, `UPSTOX_TOTP_SECRET`) are valid and automated daily token generation is operational.
- [ ] Telegram notifications (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are active and delivering real-time alerts.
- [ ] `TRADING_CAPITAL` and risk parameters (`DRYRUN_RISK_PCT`, `MAX_DAILY_LOSS_PCT`, `MAX_PORTFOLIO_HEAT_PCT`) are correctly configured in `.env`.

---

## 2. The 3-Gate Safety System

Live order placement is protected by three independent safety gates. **ALL THREE GATES MUST BE disarmed simultaneously** for live orders to be submitted to Upstox:

| Gate | Location / Mechanism | Default State | Action to Disarm |
| :--- | :--- | :--- | :--- |
| **Gate 1: Hardcoded Source Code Switch** | `backend/safety_gate.py` (`KILL_SWITCH_ENGAGED`) | `True` | Change `KILL_SWITCH_ENGAGED = False` in `safety_gate.py` |
| **Gate 2: Environment Variable** | `backend/.env` (`ALLOW_LIVE_TRADING`) | `false` | Set `ALLOW_LIVE_TRADING=true` in `backend/.env` |
| **Gate 3: Arming CLI Token File** | `backend/cache/live_trading_armed.flag` | Non-existent | Run `python3 cli.py arm-live-trading --confirm` |

---

## 3. Step-by-Step Live Arming Procedure

### Step 1: Disarm Gate 1 (Source Code)
Edit `backend/safety_gate.py` and change line:
```python
KILL_SWITCH_ENGAGED = False
```

### Step 2: Disarm Gate 2 (Environment)
Edit `backend/.env` and update:
```env
ALLOW_LIVE_TRADING=true
```

### Step 3: Disarm Gate 3 (Arming CLI)
Execute the interactive arming command:
```bash
python3 /var/www/html/hft/backend/cli.py arm-live-trading --confirm
```

---

## 4. Operational & Monitoring Commands

### Check Live Service Status
```bash
systemctl --user status hft-live.service
```

### View Live Execution Logs
```bash
tail -f /var/www/html/hft/backend/logs/live_trading.log
```

---

## 5. Emergency Disarming & Kill Switch Procedure

If market conditions deteriorate rapidly or an unhandled anomaly occurs:

### Immediate System Stop (Recommended)
Stop the systemd live service immediately:
```bash
systemctl --user stop hft-live.service
```

### Trigger Software Kill Switch
Run the instant disarm command (removes Gate 3 flag):
```bash
python3 /var/www/html/hft/backend/cli.py disarm-live-trading
```
Or edit `backend/.env`:
```env
ALLOW_LIVE_TRADING=false
```
Then restart or stop services as required.
