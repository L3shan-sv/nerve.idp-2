# nerve/deploy/policy.rego
# Main policy — aggregates all 6 checks into a 0-100 compliance score.
# Unit tests in nerve/deploy/policy_test.rego

package nerve.deploy

import future.keywords.if
import future.keywords.in

# ─────────────────────────────────────────────
# Policy weights — must sum to 100
# ─────────────────────────────────────────────
weights := {
    "health_endpoints":     15,
    "slo_defined":          20,
    "runbook":              15,
    "otel_instrumentation": 15,
    "secrets_via_vault":    20,
    "security_posture":     15,
}

# ─────────────────────────────────────────────
# Individual check results
# Each check returns: {status, score, detail, fix_url}
# status: "pass" | "warn" | "fail"
# score: 0 to weight (full points for pass, partial for warn, 0 for fail)
# ─────────────────────────────────────────────

# ── Health endpoints ──────────────────────────
check_health_endpoints := result if {
    input.health_check_passing == true
    result := {
        "status": "pass",
        "score": weights.health_endpoints,
        "detail": "Health endpoints /health and /ready are responding correctly.",
    }
}

check_health_endpoints := result if {
    input.health_check_passing != true
    result := {
        "status": "fail",
        "score": 0,
        "detail": "Health endpoints /health or /ready are not returning 200. Service is not production-ready.",
        "fix_url": sprintf("https://nerve.internal/docs/%v#health-endpoints", [input.service_name]),
    }
}

# ── SLO defined ───────────────────────────────
check_slo_defined := result if {
    input.slo_defined == true
    result := {
        "status": "pass",
        "score": weights.slo_defined,
        "detail": "SLO definition found with latency and uptime targets.",
    }
}

check_slo_defined := result if {
    input.slo_defined != true
    result := {
        "status": "fail",
        "score": 0,
        "detail": "No SLO definition found. Create a service.yaml with latency and uptime targets.",
        "fix_url": sprintf("https://nerve.internal/docs/%v#slo-setup", [input.service_name]),
    }
}

# ── Runbook ───────────────────────────────────
# Strict check: runbook must exist AND be updated after last deploy.
# This prevents gaming with a stale placeholder runbook.
check_runbook := result if {
    input.runbook_url != null
    input.runbook_updated_at != null
    input.runbook_last_deploy_at != null
    input.runbook_updated_at >= input.runbook_last_deploy_at
    result := {
        "status": "pass",
        "score": weights.runbook,
        "detail": "Runbook found and updated after last deploy.",
    }
}

check_runbook := result if {
    input.runbook_url != null
    input.runbook_updated_at != null
    input.runbook_last_deploy_at != null
    input.runbook_updated_at < input.runbook_last_deploy_at
    result := {
        "status": "fail",
        "score": 0,
        "detail": "Runbook exists but has not been updated since the last deploy. Update the runbook to reflect current service behaviour.",
        "fix_url": input.runbook_url,
    }
}

check_runbook := result if {
    input.runbook_url == null
    result := {
        "status": "fail",
        "score": 0,
        "detail": "No TechDocs runbook found for this service. Create /docs/runbook.md in the service repo.",
        "fix_url": sprintf("https://nerve.internal/docs/%v#runbook-template", [input.service_name]),
    }
}

# ── OTel instrumentation ──────────────────────
check_otel := result if {
    input.otel_traces_exporting == true
    count(input.otel_missing_endpoints) == 0
    result := {
        "status": "pass",
        "score": weights.otel_instrumentation,
        "detail": "OpenTelemetry traces are exporting correctly from all endpoints.",
    }
}

check_otel := result if {
    input.otel_traces_exporting == true
    count(input.otel_missing_endpoints) > 0
    missing := concat(", ", input.otel_missing_endpoints)
    result := {
        "status": "warn",
        "score": round(weights.otel_instrumentation * 0.5),
        "detail": sprintf("OTel traces exporting but %d endpoints missing instrumentation: %v", [count(input.otel_missing_endpoints), missing]),
        "fix_url": sprintf("https://nerve.internal/docs/%v#otel-setup", [input.service_name]),
    }
}

check_otel := result if {
    input.otel_traces_exporting != true
    result := {
        "status": "fail",
        "score": 0,
        "detail": "No OpenTelemetry traces detected. Wire the OTel SDK using the golden path template.",
        "fix_url": sprintf("https://nerve.internal/docs/%v#otel-setup", [input.service_name]),
    }
}

# ── Secrets via Vault ─────────────────────────
check_secrets := result if {
    input.has_vault_secrets == true
    input.has_plaintext_secrets == false
    result := {
        "status": "pass",
        "score": weights.secrets_via_vault,
        "detail": "Secrets are sourced from Vault. No plaintext secrets detected in manifests.",
    }
}

check_secrets := result if {
    input.has_plaintext_secrets == true
    result := {
        "status": "fail",
        "score": 0,
        "detail": "Plaintext secrets detected in environment variables or Kubernetes Secret objects. Move all secrets to Vault.",
        "fix_url": sprintf("https://nerve.internal/docs/%v#vault-secrets", [input.service_name]),
    }
}

check_secrets := result if {
    input.has_vault_secrets != true
    input.has_plaintext_secrets != true
    result := {
        "status": "fail",
        "score": 0,
        "detail": "No Vault secret configuration found. Configure Vault agent sidecar injection.",
        "fix_url": sprintf("https://nerve.internal/docs/%v#vault-secrets", [input.service_name]),
    }
}

# ── Security posture ──────────────────────────
# HARD BLOCK: A single Critical CVE → score = 0 regardless of other checks.
check_security := result if {
    input.critical_cves > 0
    result := {
        "status": "fail",
        "score": 0,
        "detail": sprintf("HARD BLOCK: %d Critical CVE(s) detected. Patch before deploying to any environment.", [input.critical_cves]),
        "fix_url": sprintf("https://nerve.internal/services/%v/security", [input.service_id]),
    }
}

check_security := result if {
    input.critical_cves == 0
    input.high_cves > 3
    result := {
        "status": "warn",
        "score": round(weights.security_posture * 0.5),
        "detail": sprintf("No Critical CVEs but %d High CVEs detected. Review and patch.", [input.high_cves]),
        "fix_url": sprintf("https://nerve.internal/services/%v/security", [input.service_id]),
    }
}

check_security := result if {
    input.critical_cves == 0
    input.high_cves <= 3
    result := {
        "status": "pass",
        "score": weights.security_posture,
        "detail": sprintf("Security posture: 0 Critical CVEs, %d High CVEs.", [input.high_cves]),
    }
}

# ─────────────────────────────────────────────
# Aggregate result
# ─────────────────────────────────────────────
checks := {
    "health_endpoints":     check_health_endpoints,
    "slo_defined":          check_slo_defined,
    "runbook":              check_runbook,
    "otel_instrumentation": check_otel,
    "secrets_via_vault":    check_secrets,
    "security_posture":     check_security,
}

score := total if {
    scores := [v.score | v := checks[_]]
    total := sum(scores)
}

# Hard block on critical CVE — score can be 80+ but still blocked
critical_cve_block if {
    input.critical_cves > 0
}

passed if {
    score >= 80
    not critical_cve_block
}
