#!/usr/bin/env bash
# Recompute chart-read scores for historical hourly audit_verdicts (no LLM re-run).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/eth-trading-agent}"
cd "$APP_DIR"

PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
exec "$PYTHON" - <<'PY'
from __future__ import annotations

import json

import audit
from critic import AuditFinding, AuditVerdict, compute_chart_read_score

# Add score / llm_verified_json columns on existing VPS databases.
audit.init_db()

with audit._connect() as conn:
    rows = conn.execute(
        """
        SELECT id, deterministic_json, llm_json, score
        FROM audit_verdicts
        WHERE source = 'hourly'
        ORDER BY id
        """
    ).fetchall()

    updated = 0
    for row in rows:
        if row["score"] is not None:
            continue
        det = json.loads(row["deterministic_json"] or "[]")
        llm = json.loads(row["llm_json"] or "[]")
        findings_det = [
            AuditFinding(
                code=str(f["code"]),
                message=str(f["message"]),
                severity=f.get("severity", "critical"),  # type: ignore[arg-type]
            )
            for f in det
        ]
        findings_llm = [
            AuditFinding(code=str(f["code"]), message=str(f["message"]))
            for f in llm
        ]
        sanitized = any(
            "sanitized" in str(f.get("message", "")).lower() for f in det
        )
        verdict = AuditVerdict(
            source="hourly",
            deterministic=findings_det,
            llm_hallucinations=findings_llm,
            sanitized=sanitized,
        )
        score, breakdown = compute_chart_read_score(verdict)
        conn.execute(
            """
            UPDATE audit_verdicts
            SET score = ?, score_breakdown_json = ?, llm_verified_json = '[]'
            WHERE id = ?
            """,
            (score, json.dumps(breakdown), row["id"]),
        )
        updated += 1

    conn.commit()

print(f"Backfilled scores for {updated} hourly verdict rows.")
PY
