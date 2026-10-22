# Owner decisions, 2026-10-22

The repository owner made these decisions on 2026-10-22 in a recorded quiz. The
owner selected an option label; each option also carried a description, written by
the orchestrator and shown to the owner. Both are quoted verbatim below and kept
separate: the label is what was selected, the option text is what was presented.
"Implemented by" names the task that carries, or will carry, the decision. Nothing
below waives a test, gate or spec requirement. Numbering continues from
`2026-10-21-owner-decisions.md`; decisions 1 to 44 are in the earlier files. In the
quoted option text, "decision 5" is decision 5 of `2026-10-20-owner-decisions.md`.

## Release history

45. **Release and install history before its backing exists.**
    Question: "The approved release-history read can't be fully backed yet: nothing
    records the server's own deployment, there's no trusted-key store to verify
    signatures, and release receipts aren't stored. What should happen?"
    Selected label: "Build operator setup first (Recommended)".
    Option text as presented: "First add an operator command that records the
    deployment and its trusted public keys (with revocation), then serve the read
    with full signature checks; receipts shown as 'unavailable' until stored. Why:
    decision 5 says reads go live only when real storage backs them, and without a
    recorded deployment every caller would just get 404. Not 'Serve keyless now': it
    ships a read that always 404s and shows unverified signatures."
    Implemented by: T051 (operator setup), then T055 (the read).
