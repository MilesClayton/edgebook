# Edgebook Chrome extension

Manifest V3, no bundler. Read [the complete setup and operating guide](../docs/LIVE_EXECUTION.md) first.

## On Kalshi market pages

On `kalshi.com` BTC market pages (KXBTC15M, KXBTCD) a small Edgebook panel appears bottom-right. It reads **only the page URL** to work out which event or strike you are viewing, then shows Edgebook's live call for those markets:

- the call (BUY UP / BUY DOWN / NO TRADE / waiting) and the engine's reason;
- Model, Kalshi and fair-value probabilities, current prices and the "buy at or below" limits;
- any resting orders of the long-shot seller paper strategy on that event;
- an **Ask Jev** button per market.

The panel lives in a closed shadow root and never reads or clicks Kalshi's own page elements. Ask Jev creates an unsubmitted proposal at most; it is handed to the toolbar popup, where the exact-terms checkbox and Confirm are still required. Real-money entries are additionally refused by the server until the paper-proof gate passes.

To turn it on: open the popup, enter the dashboard address and passcode, tick **Show Edgebook on Kalshi pages**, and Connect. The passcode is then held in Chrome session storage (memory only, cleared when Chrome closes) for the background worker; untick and Connect again to remove it. Without the opt-in the panel shows a connect hint and makes no requests.

## Popup

Connect, remaining purchase budget, exposure/loss caps, positions, Pause, Reconcile, explicit Resume, Jev review, and exact-terms confirmation. A proposal is unsubmitted. Broker acknowledgment, partial fills, rejection and unknown outcomes have separate displays. Unknown outcomes retain reservations.

Only the dashboard origin is saved to local storage. No broker or Gateway secrets, DOM trading, or unattended trading are present. Defaults are disabled until the user configures and activates the server path.

Validation: run `npm test` and `npm run check` from `../dashboard`. Tests use fixture brokers/models, a simulated popup, a simulated background worker and a fake page DOM. Real Chrome installation and account/model connectivity remain user-operated steps.
