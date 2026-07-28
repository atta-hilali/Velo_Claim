const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL;
const API_DISABLED = configuredApiBaseUrl === "fallback" || configuredApiBaseUrl === "off";
const API_BASE_URL = API_DISABLED
  ? ""
  : (configuredApiBaseUrl || "http://127.0.0.1:8002").replace(/\/$/, "");

const statusMap = {
  READY_TO_SUBMIT: "ready",
  CLAIM_READY_TO_SUBMIT: "ready",
  NEEDS_REVIEW: "review",
  CLAIM_NEEDS_REVIEW: "review",
  HOLD_CRITICAL: "hold",
  CLAIM_HOLD_CRITICAL: "hold",
  WAITING_FOR_PAYER: "waiting",
  CLAIM_WAITING_FOR_PAYER: "waiting",
  SUBMITTED: "submitted",
};

function asList(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function normalizeStatus(value) {
  const text = String(value || "").trim();
  if (!text) return "review";
  const mapped = statusMap[text] || statusMap[text.toUpperCase()];
  if (mapped) return mapped;
  const lower = text.toLowerCase();
  if (["ready", "review", "hold", "waiting", "submitted"].includes(lower)) return lower;
  if (lower.includes("wait") || lower.includes("queue") || lower.includes("pend")) return "waiting";
  if (lower.includes("submit") || lower.includes("accept")) return "submitted";
  if (lower.includes("hold") || lower.includes("critical") || lower.includes("fail") || lower.includes("reject")) return "hold";
  if (lower.includes("ready") || lower.includes("valid")) return "ready";
  return "review";
}

function normalizeSeverity(value) {
  const text = String(value || "INFO").toUpperCase();
  if (text === "FAIL" || text === "FAILED") return "ERROR";
  if (text === "WARN") return "WARNING";
  if (["CRITICAL", "ERROR", "WARNING", "INFO"].includes(text)) return text;
  return "INFO";
}

function normalizeIssue(issue) {
  return {
    code: issue.code || issue.type || "VALIDATION_ISSUE",
    severity: normalizeSeverity(issue.severity),
    layer: issue.layer || issue.source || "Deterministic",
    field: issue.field || issue.path || "-",
    msg: issue.msg || issue.message || "Validation issue detected.",
    suggestion: issue.suggestion || issue.fix || "Review this item before submission.",
  };
}

function normalizePayload(claim, raw) {
  const payload = raw.payload || raw.claim_payload || raw.built_claim?.payload || raw.builtClaim?.payload;
  const canonical = raw.canonical_claim || raw.claim || {};
  const lineItems = asList(canonical.line_items || raw.line_items || raw.payload?.lineItems);

  if (raw.payload?.lineItems || raw.payload?.totals) {
    return raw.payload;
  }

  return {
    version: raw.payload_version || raw.version || "v1",
    format: raw.claim_payload_type || raw.payload_type || claim.format || "Payload",
    hash: raw.payload_hash || raw.built_claim?.hash || raw.builtClaim?.hash || "-",
    generated: raw.generated_at || raw.built_claim?.generatedAt || raw.builtClaim?.generatedAt || "-",
    raw: typeof payload === "string" ? payload : JSON.stringify(payload || {}, null, 2),
    lineItems: lineItems.map((item) => ({
      code: item.code || item.activity_code || "-",
      desc: item.description || item.desc || item.text || "-",
      tooth: item.tooth || "-",
      surface: item.surface || "-",
      fee: Number(item.fee || item.net || item.amount || 0),
    })),
    totals: {
      billed: Number(canonical.amount?.billed || raw.total_amount || 0),
      insurance: Number(canonical.amount?.net || raw.insurance_amount || 0),
      patient: Number(canonical.amount?.patient_share || raw.patient_share || 0),
    },
  };
}

function normalizeBackendClaim(raw) {
  if (!raw) return null;

  const canonical = raw.canonical_claim || raw.claim || raw.raw_claim || {};
  const report = raw.report || raw.validation_report || raw.validation || {};
  const route = raw.route || raw.routing_context || {};
  const payer = canonical.payer || raw.payer || {};
  const patient = canonical.patient || raw.patient || {};
  const provider = canonical.provider || raw.provider || {};
  const issues = asList(report.issues || raw.issues).map(normalizeIssue);
  const checks = report.checks || {};

  const claim = {
    id: raw.id || raw.claim_id || canonical.claim_id || canonical.id || "UNKNOWN_CLAIM",
    patient: raw.patient_name || patient.name || patient.text || raw.patient || "Unknown Patient",
    mrn: raw.mrn || patient.id || raw.patient_id || "-",
    payer: raw.payer_name || payer.name || payer.id || raw.payer || "Unknown Payer",
    plan: raw.plan || payer.plan || payer.class || "-",
    jurisdiction: raw.jurisdiction || route.jurisdiction || canonical.jurisdiction || "-",
    format: raw.format || raw.claim_format || route.claim_standard || route.claimStandard || "Canonical",
    serviceDate: raw.service_date || raw.date_of_service || canonical.service_date || canonical.date_of_service || "-",
    score: Number(raw.score ?? report.score ?? 0),
    status: normalizeStatus(raw.status || report.routing),
    updated: raw.updated || raw.updated_at || raw.generated_at || "just now",
    scoreBreakdown: raw.scoreBreakdown || raw.score_breakdown || [
      { layer: "Deterministic", deduction: 0, causes: [] },
      { layer: "Knowledge Graph", deduction: 0, causes: [] },
      { layer: "LLM", deduction: 0, causes: [] },
    ],
    issues: raw.issues?.structural || raw.issues?.business ? raw.issues : {
      structural: issues.filter((issue) => String(issue.code).startsWith("STRUCT")),
      business: issues.filter((issue) => !String(issue.code).startsWith("STRUCT")),
    },
    priorAuth: raw.priorAuth || raw.prior_auth || raw.prior_auth_result || { exists: false },
    eligibility: raw.eligibility || raw.eligibility_result || {
      status: "Unknown",
      memberId: payer.member_id || payer.memberId || "-",
      coverageRef: payer.coverage_id || raw.coverage_id || "-",
      checkedAgo: "-",
      validUntil: "-",
      voiFlag: null,
      benefits: [],
    },
    audit: raw.audit || raw.audit_events || raw.validation_events || [],
  };

  claim.payload = normalizePayload(claim, raw);

  if (!claim.issues.business.length && Object.keys(checks).length) {
    claim.issues.business = Object.entries(checks).flatMap(([name, check]) =>
      asList(check.issues).map((issue) => ({ ...normalizeIssue(issue), layer: issue.layer || name })),
    );
  }

  if (provider.specialty && !claim.audit.length) {
    claim.audit = [
      {
        ts: "now",
        agent: "Backend",
        node: "ClaimLoaded",
        type: "Exit",
        color: "#15883E",
      },
    ];
  }

  return claim;
}

async function requestJson(path, options = {}) {
  if (!API_BASE_URL) {
    throw new Error("VITE_API_BASE_URL is not configured.");
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
    throw new Error(`API ${response.status}: ${await response.text()}`);
  }

  return response.json();
}

export async function fetchDesignClaims() {
  if (!API_BASE_URL) {
    return { source: "fallback", claims: [] };
  }

  const data = await requestJson("/claims");
  const claims = Array.isArray(data) ? data : data.claims || [];
  return {
    source: "backend",
    claims: claims.map(normalizeBackendClaim).filter(Boolean),
  };
}

export async function updateDesignClaimStatus(claimId, status, metadata = {}) {
  if (!API_BASE_URL) {
    return { ok: true, source: "fallback" };
  }

  return requestJson(`/claims/${encodeURIComponent(claimId)}/status`, {
    method: "PATCH",
    body: JSON.stringify({ status, ...metadata }),
  });
}

export async function runDesignClaimAction(claimId, action, metadata = {}) {
  if (!API_BASE_URL) {
    return { ok: true, source: "fallback" };
  }

  return requestJson(`/claims/${encodeURIComponent(claimId)}/actions/${action}`, {
    method: "POST",
    body: JSON.stringify(metadata),
  });
}
