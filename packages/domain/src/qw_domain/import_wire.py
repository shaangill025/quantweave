"""Import preview to `import_preview.schema.json` (T014). Row quarantine reasons and
conflicts become blocking `Reason` issues next to the file-level issues."""

from qw_domain.csv_import import ImportPreview, RowStatus


def preview_to_wire(p: ImportPreview) -> dict[str, object]:
    issues = [(i.code, i.message, i.blocking) for i in p.issues]
    for r in p.rows:
        if r.status is RowStatus.CONFLICT:
            issues.append(("conflict", f"row {r.row_number}: record differs", True))
        elif r.reason is not None:
            issues.append((r.reason, f"row {r.row_number}: {r.message}", True))
    accounts = sorted(p.expected_revisions)
    return {
        "id": p.import_id,
        "state": p.state,
        "file_hash": p.file_hash,
        "source_kind": "canonical",
        # accepted rows always resolve to an existing mapped account
        "classification": "existing_account_view",
        "target_account_id": accounts[0] if len(accounts) == 1 else None,
        "mapping_version_id": p.mapping_version_id,
        "row_count": len(p.rows),
        "accepted_rows": p.count(RowStatus.ACCEPTED),
        "rejected_rows": p.rejected_rows,
        "duplicate_rows": p.count(RowStatus.DUPLICATE),
        "issues": [
            {"code": c, "message": m, "affected_ids": [], "blocking": b}
            for c, m, b in issues
        ],
        "commit_token": None,
    }
