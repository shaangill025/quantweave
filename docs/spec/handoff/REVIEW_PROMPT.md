# Independent review prompt

Review the assigned task diff against its approved requirements and the exact input/contract versions. Do not merely repeat the implementer's summary. Check arithmetic and rounding, effective timestamps, missing-data behavior, tenant/account identity, idempotency, atomicity, permission boundaries, revised-proposal invalidation, retry budgets, schema migrations and compatibility.

For AI/quant changes, inspect evidence support, point-in-time leakage, failed trials, evaluator separation and negative cases. For improvement code, verify protected assessment and human release authority remain outside candidate control. For options, inspect unknown terms and pathwise assignment/collateral cases rather than only terminal payoff.

Report findings by severity with file/line, concrete failure scenario and required correction. Separate observed bugs, untested risks and design preferences. Re-run or independently verify meaningful checks where authorized. Do not give approval based only on model agreement or a green linter.
