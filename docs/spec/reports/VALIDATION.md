# Specification validation report

Result: **PASSED**. Executed: 2026-10-09T06:27:23.550241+00:00. Python 3.13.16.

These checks concern the generated design artifacts only. Application development and release gates remain unverified.

| Check | Result | Observed evidence |
|---|---|---|
| Structured files | passed | 54 JSON and 1 YAML files parse. |
| Requirements traceability | passed | 79 decisions; 99 requirements covered by 66 tasks and 99 acceptance specifications. |
| Task dependencies | passed | 66 tasks form an acyclic dependency graph; task files exist. |
| JSON Schema metaschemas | passed | 26 Draft 2020-12 schema documents pass metaschema checks. |
| Local contract references | passed | 250 local schema/OpenAPI references resolve without network. |
| Positive and negative examples | passed | 7 valid/invalid synthetic examples meet expected schema outcomes; semantic application checks are separate. |
| Designed API invariants | passed | 72 unique designed operations; path parameters and write headers match; no live broker-order route. Not a full OpenAPI conformance certification. |
| Numerical oracle checks | passed | 20 independent arithmetic/scenario expectations recomputed; NOT application tests or empirical investment results. |
| Mandatory scope and honest status | passed | All six strategies and improvement targets required; USD100/20 caps retained; application/release claims disabled. |
| Local document links | passed | 35 local Markdown links resolve. |
| License and source references | passed | Apache-2.0 terms present; 26 dated source entries and spec citations resolve. |
| Cross-file semantic consistency | passed | Six canonical strategy IDs, 21 onboarding IDs, 24 qualification references and 18 risk/gate mappings are consistent. |

## Reproduce

From an authorized environment with the tooling dependencies installed:

```text
python tools/validate_spec.py
```

Dependencies are listed in tools/requirements-validation.txt. Installing them may require network permission; the validator itself does not use the network.

## Not established

- No application implementation exercised.

- No broker/model/provider API called.

- No legal/commercial/data rights cleared.

- No strategy investment edge, resource SLO, sandbox security or full recursive implementation demonstrated.

- OpenAPI checked for references, shapes and declared invariants; no dedicated full OpenAPI conformance validator installed.
