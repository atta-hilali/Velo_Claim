import { demoClaims } from "./demoClaims.js";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") || "";

function normalizeClaim(claim) {
  if (!claim) {
    return null;
  }

  const report = claim.report || claim.validation_report || {};
  const route = claim.route || claim.routing_context || {};
  const builtClaim = claim.builtClaim || claim.built_claim || {
    payloadType: claim.claim_payload_type || claim.payload_type,
    payload: claim.claim_payload || claim.payload,
    objectUri: claim.object_uri,
    schemaStatus: claim.schema_status,
  };

  return {
    id: claim.id || claim.claim_id || claim.canonical_claim?.claim_id || "UNKNOWN_CLAIM",
    patient: claim.patient || claim.patient_name || claim.canonical_claim?.patient?.name || "Unknown patient",
    mrn: claim.mrn || claim.patient_id || claim.canonical_claim?.patient?.id || "-",
    payer: claim.payer || claim.payer_name || claim.canonical_claim?.payer?.name || "Unknown payer",
    payerId: claim.payerId || claim.payer_id || claim.canonical_claim?.payer?.id,
    plan: claim.plan || claim.canonical_claim?.payer?.plan || "-",
    jurisdiction: claim.jurisdiction || route.jurisdiction || "Unknown",
    standard: claim.standard || route.claim_standard || claim.claim_format || "Canonical",
    serviceDate: claim.serviceDate || claim.service_date || claim.canonical_claim?.service_date,
    updated: claim.updated || claim.updated_at || "just now",
    status: claim.status || report.routing || "needs_review",
    score: Number(claim.score ?? report.score ?? 0),
    amount: claim.amount || claim.total_amount || claim.canonical_claim?.amount?.net || "-",
    builtClaim,
    route: {
      claimStandard: route.claim_standard || route.claimStandard || claim.claim_format,
      priorAuthStandard: route.prior_auth_standard || route.priorAuthStandard,
      eligibilityStandard: route.eligibility_standard || route.eligibilityStandard,
      payerPortal: route.payer_portal || route.payerPortal,
      confidence: route.confidence,
      ...route,
    },
    validation: claim.validation || report.validation || {
      summary: {
        critical: report.summary?.critical_issues || 0,
        warnings: report.summary?.warnings || 0,
        info: report.summary?.information || 0,
      },
      checks: Object.entries(report.checks || {}).map(([name, result]) => ({
        name,
        status: result.passes ? "pass" : "fail",
        issues: result.issues || [],
      })),
    },
    eligibility: claim.eligibility || claim.eligibility_result || {},
    priorAuth: claim.priorAuth || claim.prior_auth || claim.prior_auth_result || {},
    audit: claim.audit || claim.audit_events || claim.validation_events || [],
  };
}

async function requestJson(path, options = {}) {
  if (!API_BASE_URL) {
    throw new Error("No VITE_API_BASE_URL configured.");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`API ${response.status}: ${body || response.statusText}`);
  }

  return response.json();
}

export async function fetchClaims() {
  try {
    const data = await requestJson("/claims");
    const claims = Array.isArray(data) ? data : data.claims || [];
    return {
      claims: claims.map(normalizeClaim).filter(Boolean),
      source: "backend",
      error: null,
    };
  } catch (error) {
    return {
      claims: demoClaims,
      source: "demo",
      error: API_BASE_URL ? error.message : null,
    };
  }
}

export async function updateClaimStatus(claimId, status, note) {
  if (!API_BASE_URL) {
    return { ok: true, source: "demo" };
  }

  return requestJson(`/claims/${encodeURIComponent(claimId)}/status`, {
    method: "PATCH",
    body: JSON.stringify({ status, note }),
  });
}

export async function runClaimAction(claimId, action) {
  if (!API_BASE_URL) {
    return { ok: true, source: "demo" };
  }

  return requestJson(`/claims/${encodeURIComponent(claimId)}/actions/${action}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
