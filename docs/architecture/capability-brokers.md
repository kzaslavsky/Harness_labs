# Capability Brokers

Status: implemented

Browser, network, and external-effect execution share one controller-owned
request/receipt boundary:

```text
model task -> typed request -> deterministic policy
           -> injected controller handler -> typed receipt -> audit event
```

Policies enumerate browser operations and origins, network methods and hosts,
and external-effect operations and destinations. URL credentials are rejected.
External effects require an explicit authorization reference.

Every request has an idempotency key. Repeating the same authorized effect
returns its existing receipt; reusing a key for another effect is rejected.
Handlers are injected by the production runtime, keeping Playwright, HTTP,
email, and other provider details outside the controller contract.

Receipts record status, target, duration, authorization, result, and replay
state. An attached audit journal persists a content-addressed receipt and a
`capability_broker_completed` event.
