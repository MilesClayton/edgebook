# Edgebook Chrome extension

Manifest V3 controls for the persistent confirmed-execution backend. No bundler is required.

Read [the complete setup and operating guide](../docs/LIVE_EXECUTION.md). Configure the existing recorder, Vercel proxy/model credentials, exact owner host, durable volume and risk limits before loading this directory through Chrome's Load unpacked control. Set Vercel EXTENSION_ORIGIN to the extension's actual origin.

The popup has Connect, remaining purchase budget, exposure/loss caps, positions, Pause, Reconcile, explicit Resume, Jev review, and exact-terms confirmation. A proposal is unsubmitted. Broker acknowledgment, partial fills, rejection and unknown outcomes have separate displays. Unknown outcomes retain reservations.

Only the dashboard origin is saved locally. Passwords remain in popup memory; pending confirmation tokens and idempotency keys use Chrome session storage. No broker or Gateway secrets, DOM trading, or unattended trading are present. Defaults are disabled until the user configures and activates the server path.

Validation: run npm test and npm run check from ../dashboard. Tests use fixture brokers/models and a simulated popup. Real Chrome installation and account/model connectivity remain user-operated verification steps.
