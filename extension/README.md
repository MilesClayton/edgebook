# Edgebook Chrome extension

Manifest V3, no bundler. Read [the complete setup and operating guide](../docs/LIVE_EXECUTION.md) first.

## On Kalshi market pages

On `kalshi.com` BTC market pages (KXBTC15M, KXBTCD) a small Edgebook panel appears bottom-right. It reads **only the page URL** to work out which event or strike you are viewing, then shows Edgebook's live call for those markets:

- the call (BUY UP / BUY DOWN / NO TRADE / waiting) and the engine's reason;
- Model, Kalshi and fair-value probabilities, current prices and the "buy at or below" limits;
- any resting orders of the long-shot seller paper strategy on that event;
- an **Ask Jev** button per market.

The panel lives in a closed shadow root and never reads or clicks Kalshi's own page elements. Ask Jev creates an unsubmitted proposal at most; it is handed to the toolbar popup, where it is only sent when you click Buy. The server's paper-proof gate (if not waived) and money caps still apply.

To turn it on: open the popup once, enter the dashboard address and password, and Connect. Both are remembered on this machine (chrome.storage.local) and used by the popup and the page panel; **Forget saved password** removes the password. The server still requires it, because the dashboard URL is public.

## Popup

Auto-connects with the saved password; shows remaining purchase budget, exposure/loss caps and positions; Resume, Pause, Reconcile; Ask Jev; and a Buy button on each proposal. A proposal is unsubmitted. Broker acknowledgment, partial fills, rejection and unknown outcomes have separate displays. Unknown outcomes retain reservations.

The dashboard address and password are saved to local storage on this machine. No broker or Gateway secrets, DOM trading, or unattended trading are present. Defaults are disabled until the user configures and activates the server path.

Validation: run `npm test` and `npm run check` from `../dashboard`. Tests use fixture brokers/models, a simulated popup, a simulated background worker and a fake page DOM. Real Chrome installation and account/model connectivity remain user-operated steps.
