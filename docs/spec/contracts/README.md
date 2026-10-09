# Designed contracts, version 0.1.0

These are implementation contracts, not a running API or an assertion that generated client/server code exists. Schema validation enforces shape, not permissions, arithmetic, balanced postings, portfolio freshness, evidence correctness or economic suitability. Those invariants require domain transactions and tests.

The API uses server-derived tenant scope, cookie sessions, CSRF/origin checks for writes, bounded pagination, persisted jobs and optimistic revisions. Body account identifiers must agree with the authorized path. Idempotency keys are scoped by authenticated principal, route and payload hash. Reuse with a different payload is a conflict. An asynchronous retry must not reset original triggers or spend reservations.

List endpoints have domain-typed page envelopes; T008 must bind generated client/server behavior and versioned error handling before SDK generation. The OpenAPI surface covers all required domains, but is not a substitute for integration-specific authorization protocols. Never expose a broker order mutation route. The only order route here belongs to the isolated simulator.

Examples are synthetic. Financial decimal strings have up to 60 characters; domain constructors additionally enforce scale, positive quantities, valid prices and overflow bounds. A schema allowing zero syntactically does not authorize zero-quantity trades. Unknown values use null plus an explicit status, never a misleading zero. Format checks are enabled by the artifact validator.

Required semantic checks: balanced ledger postings per commodity; unique event fingerprints; authoritative account/source mapping; expiry after trigger; server-side proposal qualification and signature validity; distinct evaluator run/context; current revisions at final publish; no unknown adjusted option terms used for live sizing; protected experiment assessor; signature-authorized promotion and user adoption when applicable.
