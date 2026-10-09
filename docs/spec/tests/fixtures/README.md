# Synthetic fixtures only

All files here are fabricated analytical test cases, not real prices, user trades or authentic broker export templates. Symbols beginning SYN are fictional. Do not advertise CIBC/Yahoo/Wealthsimple format support by passing these generic examples. T005 must obtain sanitized actual user-authorized samples and pin export versions before T015/T016 qualification.

Numerical oracles state deterministic economic expectations separately from the application under test. Options examples are idealized terminal payoff arithmetic, not assurance against assignment/liquidity/expiry-path losses. The offline specification validator checks these expected identities; it does not execute future application code.

Use overlapping holdings to verify one account shown twice stays 50 shares, while an actually different account adds another 50: consolidated 100, not 150 or 50. Use canonical transaction files to verify acquisition/disposition fee convention and idempotent re-import.
