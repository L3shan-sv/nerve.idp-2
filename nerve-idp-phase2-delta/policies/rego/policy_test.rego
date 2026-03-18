# nerve/deploy/policy_test.rego
# Run with: opa test policies/rego/ -v

package nerve.deploy_test

import data.nerve.deploy

# ─────────────────────────────────────────────
# Test: fully passing service → score 100, passed = true
# ─────────────────────────────────────────────
test_fully_compliant_service if {
    result := deploy.score with input as {
        "service_id": "test-svc-001",
        "service_name": "payment-service",
        "version": "v2.0.0",
        "environment": "production",
        "health_check_passing": true,
        "slo_defined": true,
        "runbook_url": "https://nerve.internal/docs/payment-service",
        "runbook_updated_at": "2024-06-15T10:00:00Z",
        "runbook_last_deploy_at": "2024-06-10T09:00:00Z",
        "otel_traces_exporting": true,
        "otel_missing_endpoints": [],
        "has_vault_secrets": true,
        "has_plaintext_secrets": false,
        "critical_cves": 0,
        "high_cves": 0,
    }
    result == 100
}

test_fully_compliant_passes if {
    deploy.passed with input as {
        "service_id": "test-svc-001",
        "service_name": "payment-service",
        "version": "v2.0.0",
        "environment": "production",
        "health_check_passing": true,
        "slo_defined": true,
        "runbook_url": "https://nerve.internal/docs/payment-service",
        "runbook_updated_at": "2024-06-15T10:00:00Z",
        "runbook_last_deploy_at": "2024-06-10T09:00:00Z",
        "otel_traces_exporting": true,
        "otel_missing_endpoints": [],
        "has_vault_secrets": true,
        "has_plaintext_secrets": false,
        "critical_cves": 0,
        "high_cves": 0,
    }
}

# ─────────────────────────────────────────────
# Test: Critical CVE → hard block regardless of score
# ─────────────────────────────────────────────
test_critical_cve_hard_blocks if {
    not deploy.passed with input as {
        "service_id": "test-svc-002",
        "service_name": "api-gateway",
        "version": "v1.9.0",
        "environment": "production",
        "health_check_passing": true,
        "slo_defined": true,
        "runbook_url": "https://nerve.internal/docs/api-gateway",
        "runbook_updated_at": "2024-06-15T10:00:00Z",
        "runbook_last_deploy_at": "2024-06-10T09:00:00Z",
        "otel_traces_exporting": true,
        "otel_missing_endpoints": [],
        "has_vault_secrets": true,
        "has_plaintext_secrets": false,
        "critical_cves": 1,   # ← one critical CVE = hard block
        "high_cves": 0,
    }
}

test_critical_cve_zeroes_security_score if {
    result := deploy.checks with input as {
        "service_id": "test-svc-002",
        "service_name": "api-gateway",
        "version": "v1.9.0",
        "environment": "production",
        "health_check_passing": true,
        "slo_defined": true,
        "runbook_url": "https://nerve.internal/docs/api-gateway",
        "runbook_updated_at": "2024-06-15T10:00:00Z",
        "runbook_last_deploy_at": "2024-06-10T09:00:00Z",
        "otel_traces_exporting": true,
        "otel_missing_endpoints": [],
        "has_vault_secrets": true,
        "has_plaintext_secrets": false,
        "critical_cves": 1,
        "high_cves": 0,
    }
    result.security_posture.score == 0
    result.security_posture.status == "fail"
}

# ─────────────────────────────────────────────
# Test: stale runbook → fail (updated before last deploy)
# This prevents the placeholder-runbook gaming pattern
# ─────────────────────────────────────────────
test_stale_runbook_fails if {
    result := deploy.checks with input as {
        "service_id": "test-svc-003",
        "service_name": "order-service",
        "version": "v1.0.0",
        "environment": "staging",
        "health_check_passing": true,
        "slo_defined": true,
        "runbook_url": "https://nerve.internal/docs/order-service",
        "runbook_updated_at": "2024-01-01T00:00:00Z",   # ← old
        "runbook_last_deploy_at": "2024-06-01T00:00:00Z", # ← deploy after runbook update
        "otel_traces_exporting": true,
        "otel_missing_endpoints": [],
        "has_vault_secrets": true,
        "has_plaintext_secrets": false,
        "critical_cves": 0,
        "high_cves": 0,
    }
    result.runbook.status == "fail"
    result.runbook.score == 0
}

# ─────────────────────────────────────────────
# Test: score below 80 → not passed
# ─────────────────────────────────────────────
test_low_score_blocked if {
    not deploy.passed with input as {
        "service_id": "test-svc-004",
        "service_name": "legacy-service",
        "version": "v0.1.0",
        "environment": "production",
        "health_check_passing": false,  # -15
        "slo_defined": false,           # -20
        "runbook_url": null,
        "runbook_updated_at": null,
        "runbook_last_deploy_at": null,
        "otel_traces_exporting": true,
        "otel_missing_endpoints": [],
        "has_vault_secrets": true,
        "has_plaintext_secrets": false,
        "critical_cves": 0,
        "high_cves": 0,
    }
    # score = 15 + 15 + 20 + 15 = 65 — below 80, blocked
}

# ─────────────────────────────────────────────
# Test: OTel warn path — partial score
# ─────────────────────────────────────────────
test_otel_partial_warn if {
    result := deploy.checks with input as {
        "service_id": "test-svc-005",
        "service_name": "user-service",
        "version": "v1.5.0",
        "environment": "staging",
        "health_check_passing": true,
        "slo_defined": true,
        "runbook_url": "https://nerve.internal/docs/user-service",
        "runbook_updated_at": "2024-06-15T10:00:00Z",
        "runbook_last_deploy_at": "2024-06-10T09:00:00Z",
        "otel_traces_exporting": true,
        "otel_missing_endpoints": ["/checkout", "/refund"],  # ← 2 missing
        "has_vault_secrets": true,
        "has_plaintext_secrets": false,
        "critical_cves": 0,
        "high_cves": 0,
    }
    result.otel_instrumentation.status == "warn"
    result.otel_instrumentation.score == 7   # 50% of 15
}

# ─────────────────────────────────────────────
# Test: plaintext secrets → hard fail on secrets check
# ─────────────────────────────────────────────
test_plaintext_secrets_fail if {
    result := deploy.checks with input as {
        "service_id": "test-svc-006",
        "service_name": "auth-service",
        "version": "v3.1.0",
        "environment": "production",
        "health_check_passing": true,
        "slo_defined": true,
        "runbook_url": "https://nerve.internal/docs/auth-service",
        "runbook_updated_at": "2024-06-15T10:00:00Z",
        "runbook_last_deploy_at": "2024-06-10T09:00:00Z",
        "otel_traces_exporting": true,
        "otel_missing_endpoints": [],
        "has_vault_secrets": true,
        "has_plaintext_secrets": true,  # ← plaintext secrets detected
        "critical_cves": 0,
        "high_cves": 0,
    }
    result.secrets_via_vault.status == "fail"
    result.secrets_via_vault.score == 0
}
