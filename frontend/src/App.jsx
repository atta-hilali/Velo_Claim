import React, { useState, useMemo } from "react";
import {
  Bell, Search, ChevronDown, ChevronRight, X, Check, AlertTriangle,
  AlertCircle, Info, Clock, FileText, Shield, Activity, ListChecks,
  RefreshCw, ArrowLeft, Hash, Filter
} from "lucide-react";
import {
  fetchDesignClaims,
  runDesignClaimAction,
  updateDesignClaimStatus,
} from "./designApi.js";

/* ---------------------------------------------------------------
   VELO CLAIM — RCM back-office console
   Palette pulled from Velodoc header: teal/cyan gradient
   teal-900 #0B5C6B / teal-700 #0E8298 / cyan-500 #1AB6C9 / cyan-400 #3FD0E0
   Status: green=ready amber=review red=hold blue=waiting gray=submitted
---------------------------------------------------------------- */

const STATUS = {
  ready:     { label: "Ready to Submit", color: "#15883E", bg: "#E7F6EC", border: "#15883E" },
  review:    { label: "Needs Review",    color: "#B25E00", bg: "#FDF1DF", border: "#E08A00" },
  hold:      { label: "Hold — Critical", color: "#C22B2B", bg: "#FCE9E9", border: "#C22B2B" },
  waiting:   { label: "Waiting on Payer",color: "#1864AB", bg: "#E8F2FC", border: "#1864AB" },
  submitted: { label: "Submitted",       color: "#5B6470", bg: "#EEF0F2", border: "#5B6470" },
};

const SEV = {
  CRITICAL: { color: "#C22B2B", icon: AlertCircle, label: "CRITICAL" },
  ERROR:    { color: "#E0671B", icon: AlertTriangle, label: "ERROR" },
  WARNING:  { color: "#C7900A", icon: AlertTriangle, label: "WARNING" },
  INFO:     { color: "#5B6470", icon: Info, label: "INFO" },
};

const LAYER_STYLE = {
  Deterministic:   { bg: "#EEF0F2", color: "#444B54" },
  "Knowledge Graph":{ bg: "#E8F2FC", color: "#1864AB" },
  LLM:             { bg: "#F1E9FB", color: "#7039B0" },
};

/* ---------------------------- MOCK DATA ---------------------------- */

const fallbackClaims = [
  {
    id: "CLM-10482", patient: "Ahmed Al-Mansouri", mrn: "a9fb0d0f-fb3c", payer: "Daman",
    plan: "Thiqa Gold", jurisdiction: "Abu Dhabi", format: "Shafafiya", serviceDate: "2026-06-18",
    score: 82, status: "review", updated: "12m ago",
    scoreBreakdown: [
      { layer: "Deterministic", deduction: -8, causes: [
        { code: "STRUCT_DEDUP_NEAR_MATCH", pts: -3, label: "Near-duplicate claim within 7 days" },
        { code: "CODING_TOOTH_SURFACE_MISMATCH", pts: -5, label: "Missing tooth number on D3330 line item" },
      ]},
      { layer: "Knowledge Graph", deduction: -6, causes: [
        { code: "DAMAN_VOI_MISSING", pts: -6, label: "Daman VOI reference absent — Thiqa plan requires it" },
      ]},
      { layer: "LLM", deduction: -4, causes: [
        { code: "DOC_NARRATIVE_THIN", pts: -4, label: "Clinical narrative below payer threshold for root canal" },
      ]},
    ],
    issues: {
      structural: [
        { code: "STRUCT_DEDUP_NEAR_MATCH", severity: "WARNING", layer: "Deterministic",
          field: "claim.lineItems", msg: "Similar claim submitted for this patient within 7 days.",
          suggestion: "Confirm this is not a duplicate of CLM-10311 before submitting." },
      ],
      business: [
        { code: "DAMAN_VOI_MISSING", severity: "CRITICAL", layer: "Knowledge Graph",
          field: "coverage.voiRef", msg: "Daman Verification of Insurance (VOI) reference is missing for Thiqa plan.",
          suggestion: "Run an eligibility check to obtain a current VOI reference before submission." },
        { code: "CODING_TOOTH_SURFACE_MISMATCH", severity: "ERROR", layer: "Deterministic",
          field: "lineItems[1].tooth", msg: "Tooth number missing for endodontic therapy line item (D3330).",
          suggestion: "Add the tooth number on line item 2 — payer will reject without it." },
        { code: "DOC_NARRATIVE_THIN", severity: "WARNING", layer: "LLM",
          field: "encounter.narrative", msg: "Clinical narrative is shorter than payer's typical accepted threshold for root canal procedures.",
          suggestion: "Add detail on pulp diagnosis and canal count to strengthen medical necessity." },
        { code: "PAYER_RULE_FREQ_LIMIT", severity: "INFO", layer: "Knowledge Graph",
          field: "lineItems[0]", msg: "Composite restoration frequency is within Daman's annual limit.",
          suggestion: "No action needed." },
      ],
    },
    payload: {
      version: "v2.3", format: "XML", hash: "8f2a1c9e4b7d…",
      generated: "2026-06-18 14:32", lineItems: [
        { code: "D2330", desc: "Resin-based composite — one surface, anterior", tooth: "46", surface: "M", fee: 488.0 },
        { code: "D3330", desc: "Endodontic therapy, molar tooth (excl. final restoration)", tooth: "—", surface: "—", fee: 137.0 },
      ],
      totals: { billed: 1113.0, insurance: 684.2, patient: 428.8 },
    },
    priorAuth: { exists: false },
    eligibility: {
      status: "Active", memberId: "TH-2284910", coverageRef: "COV-77281",
      checkedAgo: "2h ago", validUntil: "2026-06-18 20:32",
      voiFlag: "missing", benefits: [
        { k: "Plan", v: "Thiqa Gold" }, { k: "Dental Coverage", v: "80% restorative" },
        { k: "Annual Max", v: "AED 8,000" }, { k: "Deductible Met", v: "AED 1,200 / 1,500" },
      ],
    },
    audit: [
      { ts: "14:31:02", agent: "Claim Prep", node: "IngestEncounter", type: "Enter", color: "#1864AB" },
      { ts: "14:31:04", agent: "Claim Prep", node: "BuildPayload", type: "Exit", color: "#15883E" },
      { ts: "14:31:09", agent: "Validation", node: "StructuralGate", type: "Enter", color: "#1864AB" },
      { ts: "14:31:11", agent: "Validation", node: "DedupCheck", type: "Error", color: "#C22B2B" },
      { ts: "14:32:00", agent: "Validation", node: "BusinessLogic", type: "Exit", color: "#15883E" },
    ],
  },
  {
    id: "CLM-10483", patient: "Fatima Al-Suwaidi", mrn: "b71e44ab-921c", payer: "NAS",
    plan: "Enhanced", jurisdiction: "Dubai", format: "eClaimLink", serviceDate: "2026-06-19",
    score: 96, status: "ready", updated: "3m ago",
    scoreBreakdown: [
      { layer: "Deterministic", deduction: 0, causes: [] },
      { layer: "Knowledge Graph", deduction: -2, causes: [
        { code: "PAYER_RULE_PRICE_VARIANCE", pts: -2, label: "Fee within 2% of payer schedule (info only)" },
      ]},
      { layer: "LLM", deduction: -2, causes: [
        { code: "DOC_BRIEF_NARRATIVE", pts: -2, label: "Preventive procedure narrative is minimal but acceptable" },
      ]},
    ],
    issues: {
      structural: [],
      business: [
        { code: "PAYER_RULE_PRICE_VARIANCE", severity: "INFO", layer: "Knowledge Graph",
          field: "lineItems[0].fee", msg: "Submitted fee is within 2% of payer fee schedule.", suggestion: "No action needed." },
      ],
    },
    payload: {
      version: "v2.3", format: "FHIR", hash: "1ad9e0f3c2a8…", generated: "2026-06-19 09:14",
      lineItems: [{ code: "D1110", desc: "Prophylaxis — adult", tooth: "—", surface: "—", fee: 220.0 }],
      totals: { billed: 220.0, insurance: 176.0, patient: 44.0 },
    },
    priorAuth: { exists: false },
    eligibility: {
      status: "Active", memberId: "NAS-558213", coverageRef: "COV-90112",
      checkedAgo: "40m ago", validUntil: "2026-06-19 18:00", voiFlag: null,
      benefits: [{ k: "Plan", v: "Enhanced" }, { k: "Preventive Coverage", v: "100%" }, { k: "Annual Max", v: "AED 5,000" }],
    },
    audit: [
      { ts: "09:13:40", agent: "Claim Prep", node: "IngestEncounter", type: "Enter", color: "#1864AB" },
      { ts: "09:14:02", agent: "Validation", node: "StructuralGate", type: "Exit", color: "#15883E" },
      { ts: "09:14:20", agent: "Validation", node: "BusinessLogic", type: "Exit", color: "#15883E" },
    ],
  },
  {
    id: "CLM-10470", patient: "Khalid Al-Maktoum", mrn: "c029f871-aa31", payer: "Daman",
    plan: "Basic", jurisdiction: "Abu Dhabi", format: "Shafafiya", serviceDate: "2026-06-15",
    score: 41, status: "hold", updated: "1h ago",
    scoreBreakdown: [
      { layer: "Deterministic", deduction: -32, causes: [
        { code: "STRUCT_PAYLOAD_NONCONFORMANT", pts: -15, label: "Payload fails Shafafiya XSD — missing encounter type" },
        { code: "STRUCT_ROUTE_MISMATCH", pts: -10, label: "Claim routed to Shafafiya but payer expects NPHIES" },
        { code: "CODING_INVALID_COMBO", pts: -7, label: "D7140 + D7210 not payable same-day without modifier" },
      ]},
      { layer: "Knowledge Graph", deduction: -18, causes: [
        { code: "ELIGIBILITY_EXPIRED", pts: -18, label: "Member coverage lapsed 5 days before service date" },
      ]},
      { layer: "LLM", deduction: -9, causes: [
        { code: "DOC_MISSING_INDICATION", pts: -9, label: "No surgical indication documented for D7210" },
      ]},
    ],
    issues: {
      structural: [
        { code: "STRUCT_PAYLOAD_NONCONFORMANT", severity: "CRITICAL", layer: "Deterministic",
          field: "claim.schema", msg: "Payload fails Shafafiya XSD validation — missing required encounter type element.",
          suggestion: "Regenerate the payload; encounter.type must be populated from the source EHR record." },
        { code: "STRUCT_ROUTE_MISMATCH", severity: "CRITICAL", layer: "Deterministic",
          field: "claim.routing", msg: "Claim routed to Shafafiya but payer plan is configured for NPHIES.",
          suggestion: "Correct the payer routing configuration before resubmitting." },
      ],
      business: [
        { code: "ELIGIBILITY_EXPIRED", severity: "CRITICAL", layer: "Knowledge Graph",
          field: "coverage.status", msg: "Member eligibility lapsed as of 2026-06-10, prior to service date.",
          suggestion: "Confirm active coverage with payer or contact patient for updated insurance." },
        { code: "CODING_INVALID_COMBO", severity: "ERROR", layer: "Deterministic",
          field: "lineItems", msg: "Procedure code combination D7140 + D7210 is not payable together same-day.",
          suggestion: "Bundle under D7210 only, or add modifier documentation." },
      ],
    },
    payload: {
      version: "v2.3", format: "XML", hash: "44b1f0a9d3c1…", generated: "2026-06-15 11:02",
      lineItems: [
        { code: "D7140", desc: "Extraction, erupted tooth", tooth: "18", surface: "—", fee: 350.0 },
        { code: "D7210", desc: "Surgical removal, erupted tooth", tooth: "18", surface: "—", fee: 620.0 },
      ],
      totals: { billed: 970.0, insurance: 0.0, patient: 970.0 },
    },
    priorAuth: { exists: true, status: "Denied", ref: "PA-22918", submitted: "2026-06-14", response: "Denied — eligibility lapsed prior to service." },
    eligibility: {
      status: "Inactive", memberId: "DM-110824", coverageRef: "COV-40221",
      checkedAgo: "1h ago", validUntil: "Expired 2026-06-10", voiFlag: "expired",
      benefits: [{ k: "Plan", v: "Basic" }, { k: "Status", v: "Lapsed" }],
    },
    audit: [
      { ts: "11:01:55", agent: "Claim Prep", node: "IngestEncounter", type: "Enter", color: "#1864AB" },
      { ts: "11:02:10", agent: "Validation", node: "StructuralGate", type: "Error", color: "#C22B2B" },
      { ts: "11:02:40", agent: "Prior Auth", node: "SubmitPA", type: "Exit", color: "#15883E" },
      { ts: "11:48:02", agent: "Prior Auth", node: "PayerResponse", type: "Error", color: "#C22B2B" },
    ],
  },
  {
    id: "CLM-10465", patient: "Mariam Al-Hashimi", mrn: "0c8e2b13-557d", payer: "Bupa",
    plan: "Gold", jurisdiction: "Dubai", format: "eClaimLink", serviceDate: "2026-06-14",
    score: 74, status: "waiting", updated: "26m ago",
    scoreBreakdown: [
      { layer: "Deterministic", deduction: -4, causes: [
        { code: "STRUCT_FINANCIAL_ROUNDING", pts: -4, label: "Rounding discrepancy AED 0.40 in patient responsibility" },
      ]},
      { layer: "Knowledge Graph", deduction: -10, causes: [
        { code: "PRIOR_AUTH_PENDING", pts: -10, label: "Submission blocked — PA still awaiting payer response" },
      ]},
      { layer: "LLM", deduction: -2, causes: [
        { code: "DOC_IMPLANT_RATIONALE_THIN", pts: -2, label: "Implant site rationale could be strengthened" },
      ]},
    ],
    issues: {
      structural: [],
      business: [
        { code: "PRIOR_AUTH_PENDING", severity: "WARNING", layer: "Knowledge Graph",
          field: "priorAuth.status", msg: "Claim has a pending prior authorization; submission is blocked until resolved.",
          suggestion: "Monitor payer response or escalate if SLA window is exceeded." },
      ],
    },
    payload: {
      version: "v2.3", format: "FHIR", hash: "9c2f7e10a4b6…", generated: "2026-06-14 16:20",
      lineItems: [{ code: "D6010", desc: "Surgical placement of implant body", tooth: "24", surface: "—", fee: 4200.0 }],
      totals: { billed: 4200.0, insurance: 3360.0, patient: 840.0 },
    },
    priorAuth: {
      exists: true, status: "Pending", ref: "PA-23004", submitted: "2026-06-14",
      waitingSince: "2026-06-14 16:25", nextPoll: "2026-06-19 16:25", attempts: 3,
      history: [{ ts: "2026-06-14 16:25", note: "Submitted to Bupa portal" }, { ts: "2026-06-16 09:00", note: "Auto re-poll — still pending" }],
    },
    eligibility: {
      status: "Active", memberId: "BP-991823", coverageRef: "COV-66102",
      checkedAgo: "5h ago", validUntil: "2026-06-19 21:00", voiFlag: null,
      benefits: [{ k: "Plan", v: "Gold" }, { k: "Implant Coverage", v: "80% with PA" }, { k: "Annual Max", v: "AED 15,000" }],
    },
    audit: [
      { ts: "16:19:50", agent: "Claim Prep", node: "IngestEncounter", type: "Enter", color: "#1864AB" },
      { ts: "16:20:30", agent: "Validation", node: "BusinessLogic", type: "Exit", color: "#15883E" },
      { ts: "16:25:00", agent: "Prior Auth", node: "SubmitPA", type: "Exit", color: "#15883E" },
    ],
  },
  {
    id: "CLM-10440", patient: "Saeed Al-Nuaimi", mrn: "7e1a0934-fc28", payer: "Daman",
    plan: "Thiqa Gold", jurisdiction: "Abu Dhabi", format: "NPHIES", serviceDate: "2026-06-10",
    score: 100, status: "submitted", updated: "2d ago",
    scoreBreakdown: [
      { layer: "Deterministic", deduction: 0, causes: [] },
      { layer: "Knowledge Graph", deduction: 0, causes: [] },
      { layer: "LLM", deduction: 0, causes: [] },
    ],
    issues: { structural: [], business: [] },
    payload: {
      version: "v2.3", format: "XML", hash: "2b6f9c41d8a0…", generated: "2026-06-10 10:00",
      lineItems: [{ code: "D0150", desc: "Comprehensive oral evaluation", tooth: "—", surface: "—", fee: 180.0 }],
      totals: { billed: 180.0, insurance: 144.0, patient: 36.0 },
    },
    priorAuth: { exists: false },
    eligibility: {
      status: "Active", memberId: "TH-220015", coverageRef: "COV-10082",
      checkedAgo: "2d ago", validUntil: "2026-06-10 23:59", voiFlag: "verified",
      benefits: [{ k: "Plan", v: "Thiqa Gold" }, { k: "Diagnostic Coverage", v: "100%" }],
    },
    audit: [
      { ts: "09:59:10", agent: "Claim Prep", node: "IngestEncounter", type: "Enter", color: "#1864AB" },
      { ts: "10:00:01", agent: "Validation", node: "BusinessLogic", type: "Exit", color: "#15883E" },
      { ts: "10:01:00", agent: "Validation", node: "SubmissionReady", type: "Exit", color: "#15883E" },
    ],
  },
  {
    id: "CLM-10455", patient: "Layla Hassan", mrn: "44a7c1e2-bb09", payer: "MetLife",
    plan: "Premier", jurisdiction: "KSA", format: "NPHIES", serviceDate: "2026-06-12",
    score: 88, status: "review", updated: "55m ago",
    scoreBreakdown: [
      { layer: "Deterministic", deduction: -2, causes: [
        { code: "STRUCT_FINANCIAL_ROUNDING", pts: -2, label: "Patient total rounding mismatch of AED 0.40" },
      ]},
      { layer: "Knowledge Graph", deduction: -6, causes: [
        { code: "DOC_ATTACHMENT_MISSING", pts: -6, label: "Periapical X-ray referenced in note but not attached" },
      ]},
      { layer: "LLM", deduction: -4, causes: [
        { code: "DOC_CROWN_RATIONALE_WEAK", pts: -4, label: "Crown medical necessity not clearly differentiated from lesser restorations" },
      ]},
    ],
    issues: {
      structural: [
        { code: "STRUCT_FINANCIAL_ROUNDING", severity: "WARNING", layer: "Deterministic",
          field: "totals.patientPays", msg: "Patient responsibility total has a rounding discrepancy of AED 0.40.",
          suggestion: "Recalculate totals from line items before submission." },
      ],
      business: [
        { code: "DOC_ATTACHMENT_MISSING", severity: "ERROR", layer: "LLM",
          field: "attachments", msg: "Periapical X-ray referenced in clinical note is not attached to the claim.",
          suggestion: "Attach the radiograph or remove the reference from the narrative." },
      ],
    },
    payload: {
      version: "v2.3", format: "XML", hash: "ac81f320e9b4…", generated: "2026-06-12 13:10",
      lineItems: [{ code: "D2740", desc: "Crown — porcelain/ceramic", tooth: "14", surface: "—", fee: 1450.0 }],
      totals: { billed: 1450.0, insurance: 1160.0, patient: 290.4 },
    },
    priorAuth: { exists: true, status: "Approved", ref: "PA-21887", submitted: "2026-06-11", response: "Approved for full amount." },
    eligibility: {
      status: "Active", memberId: "ML-330912", coverageRef: "COV-50931",
      checkedAgo: "55m ago", validUntil: "2026-06-12 22:00", voiFlag: null,
      benefits: [{ k: "Plan", v: "Premier" }, { k: "Major Restorative", v: "80% with PA" }],
    },
    audit: [
      { ts: "13:09:20", agent: "Claim Prep", node: "IngestEncounter", type: "Enter", color: "#1864AB" },
      { ts: "13:10:11", agent: "Validation", node: "BusinessLogic", type: "Error", color: "#C22B2B" },
    ],
  },
];

/* ---------------------------- UTIL ---------------------------- */

function ScoreBar({ score }) {
  const value = Number(score || 0);
  const color = value >= 85 ? "#15883E" : value >= 60 ? "#C7900A" : "#C22B2B";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 110 }}>
      <div style={{ width: 60, height: 6, borderRadius: 3, background: "#E4E7EB", overflow: "hidden" }}>
        <div style={{ width: `${value}%`, height: "100%", background: color, borderRadius: 3 }} />
      </div>
      <span style={{ fontSize: 12, fontWeight: 600, color: "#3A4048", fontVariantNumeric: "tabular-nums" }}>{value}/100</span>
    </div>
  );
}

function StatusBadge({ status, size = "sm" }) {
  const s = STATUS[status] || STATUS.review;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: size === "lg" ? "6px 14px" : "3px 10px",
      borderRadius: 999, fontSize: size === "lg" ? 13 : 11.5, fontWeight: 700,
      letterSpacing: 0.3, textTransform: "uppercase",
      color: s.color, background: s.bg, border: `1px solid ${s.border}33`,
      whiteSpace: "nowrap",
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: s.color }} />
      {s.label}
    </span>
  );
}

function Pill({ active, color, label, count, onClick }) {
  return (
    <button onClick={onClick} style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "6px 12px", borderRadius: 999, fontSize: 13, fontWeight: 600, cursor: "pointer",
      border: active ? `1.5px solid ${color}` : "1.5px solid #E4E7EB",
      background: active ? `${color}14` : "#fff",
      color: active ? color : "#5B6470",
      transition: "all .12s",
    }}>
      {label}
      <span style={{
        fontSize: 11, fontWeight: 700, padding: "1px 6px", borderRadius: 999,
        background: active ? color : "#EEF0F2", color: active ? "#fff" : "#8A9099",
      }}>{count}</span>
    </button>
  );
}

const COLOR_MAP = { ready: "#15883E", review: "#C7900A", hold: "#C22B2B", waiting: "#1864AB", submitted: "#5B6470" };

/* ---------------------------- TOP BAR ---------------------------- */

function TopBar() {
  return (
    <div style={{
      height: 56, display: "flex", alignItems: "center", justifyContent: "space-between",
      padding: "0 24px", background: "#fff", borderBottom: "1px solid #E4E7EB", flexShrink: 0,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <svg width="42" height="22" viewBox="0 0 42 22" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M30.4404 0H22.1748C21.6403 0 21.1594 0.303773 20.95 0.774407L13.3568 17.6702L15.9889 20.2843V21.9957H19.6853C20.2197 21.9957 20.7007 21.6919 20.91 21.2213L26.3789 9.04473C26.4501 8.88643 26.6104 8.78802 26.7886 8.78802H29.5542C30.4761 8.78802 31.6473 9.648 31.6473 11C31.6473 12.352 30.4761 13.212 29.5542 13.212H28.3339C28.1558 13.212 27.9955 13.3147 27.9242 13.4687L24.3614 21.401C24.2367 21.6834 24.4505 22 24.7711 22H30.4449C35.8648 22 40.2559 17.0755 40.2559 11C40.2559 4.92454 35.8559 0 30.4404 0Z" fill="url(#logo_a)"/>
          <path d="M18.4513 21.2213L9.2637 0.774407C9.04993 0.303773 8.56895 0 8.03453 0H0.445782C0.125131 0 -0.0886371 0.316608 0.0360607 0.598989L9.30378 21.2213C9.51309 21.692 9.99852 21.9957 10.5285 21.9957H19.676C19.1415 21.9957 18.6606 21.692 18.4513 21.2213Z" fill="url(#logo_b)"/>
          <defs>
            <linearGradient id="logo_a" x1="32.6716" y1="1.4119" x2="25.5761" y2="19.9325" gradientUnits="userSpaceOnUse">
              <stop stopColor="#38CCF4"/><stop offset="1" stopColor="#49E8D1"/>
            </linearGradient>
            <linearGradient id="logo_b" x1="14.5812" y1="22.5904" x2="6.22157" y2="0.936081" gradientUnits="userSpaceOnUse">
              <stop stopColor="#23B0E2"/><stop offset="1" stopColor="#49E8D1"/>
            </linearGradient>
          </defs>
        </svg>
        <span style={{ fontWeight: 700, fontSize: 16, color: "#1A1D21" }}>Velodoc</span>
        <ChevronRight size={14} color="#C2C7CD" />
        <span style={{ fontWeight: 600, fontSize: 13.5, color: "#0E8298", background: "#E8F6F8", padding: "3px 9px", borderRadius: 6 }}>Claim</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <div style={{ position: "relative" }}>
          <Bell size={18} color="#5B6470" />
          <span style={{
            position: "absolute", top: -4, right: -5, background: "#C22B2B", color: "#fff",
            fontSize: 9.5, fontWeight: 700, borderRadius: 999, width: 15, height: 15,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>6</span>
        </div>
        <span style={{
          fontSize: 12, fontWeight: 700, color: "#0E8298", background: "#E8F6F8",
          padding: "4px 10px", borderRadius: 6, border: "1px solid #BEE7EC",
        }}>RCM Team</span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{
            width: 28, height: 28, borderRadius: "50%", background: "#0E8298",
            color: "#fff", fontSize: 12, fontWeight: 700, display: "flex",
            alignItems: "center", justifyContent: "center",
          }}>SA</div>
          <span style={{ fontSize: 13.5, fontWeight: 600, color: "#1A1D21" }}>Sara Al-Bloushi</span>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------- QUEUE SCREEN ---------------------------- */

function ClaimsQueue({ claims, onOpenClaim }) {
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [checked, setChecked] = useState([]);

  const counts = useMemo(() => {
    const c = { all: claims.length, ready: 0, review: 0, hold: 0, waiting: 0, submitted: 0 };
    claims.forEach(cl => {
      const status = STATUS[cl.status] ? cl.status : "review";
      c[status]++;
    });
    return c;
  }, [claims]);

  const filtered = claims.filter(cl => {
    if (filter !== "all" && cl.status !== filter) return false;
    if (search && !`${cl.patient} ${cl.id}`.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  const toggleCheck = (id) => setChecked(c => c.includes(id) ? c.filter(x => x !== id) : [...c, id]);

  return (
    <div style={{ padding: "24px 28px", maxWidth: 1400, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 4 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, color: "#1A1D21", margin: 0 }}>Claims Queue</h1>
        <span style={{ fontSize: 13.5, color: "#8A9099", fontWeight: 500 }}>{counts.all} claims</span>
      </div>
      <p style={{ fontSize: 13, color: "#8A9099", margin: "0 0 18px 0" }}>Validation → prior auth → submission, in one queue.</p>

      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        <Pill active={filter === "all"} color="#0E8298" label="All" count={counts.all} onClick={() => setFilter("all")} />
        <Pill active={filter === "ready"} color={COLOR_MAP.ready} label="Ready to Submit" count={counts.ready} onClick={() => setFilter("ready")} />
        <Pill active={filter === "review"} color={COLOR_MAP.review} label="Needs Review" count={counts.review} onClick={() => setFilter("review")} />
        <Pill active={filter === "hold"} color={COLOR_MAP.hold} label="Hold — Critical" count={counts.hold} onClick={() => setFilter("hold")} />
        <Pill active={filter === "waiting"} color={COLOR_MAP.waiting} label="Waiting on Payer" count={counts.waiting} onClick={() => setFilter("waiting")} />
        <Pill active={filter === "submitted"} color={COLOR_MAP.submitted} label="Submitted" count={counts.submitted} onClick={() => setFilter("submitted")} />
      </div>

      <div style={{
        display: "flex", gap: 10, alignItems: "center", marginBottom: 14, flexWrap: "wrap",
        background: "#fff", border: "1px solid #E4E7EB", borderRadius: 10, padding: "10px 12px",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, flex: "1 1 220px", background: "#F6F7F8", borderRadius: 8, padding: "7px 10px" }}>
          <Search size={14} color="#8A9099" />
          <input
            placeholder="Search patient name or claim ID…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            style={{ border: "none", outline: "none", background: "transparent", fontSize: 13, width: "100%", color: "#1A1D21" }}
          />
        </div>
        {["Payer", "Jurisdiction", "Claim Format", "Date Range"].map(f => (
          <div key={f} style={{
            display: "flex", alignItems: "center", gap: 4, fontSize: 12.5, fontWeight: 600,
            color: "#5B6470", padding: "7px 10px", border: "1px solid #E4E7EB", borderRadius: 8, cursor: "pointer",
          }}>
            <Filter size={12} /> {f} <ChevronDown size={12} />
          </div>
        ))}
      </div>

      {checked.length > 0 && (
        <div style={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          background: "#E8F6F8", border: "1px solid #BEE7EC", borderRadius: 8, padding: "8px 14px", marginBottom: 10,
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "#0E8298" }}>{checked.length} selected</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button style={btnGhost}>Assign to me</button>
            <button style={btnPrimary}>Approve & Submit Selected</button>
          </div>
        </div>
      )}

      <div style={{ background: "#fff", border: "1px solid #E4E7EB", borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#FAFBFC", borderBottom: "1px solid #E4E7EB" }}>
              {["", "Claim ID", "Patient Name", "Payer", "Service Date", "Format", "Validation Score", "Status", "Updated", ""].map((h, i) => (
                <th key={i} style={{
                  textAlign: "left", padding: "10px 14px", fontSize: 11, fontWeight: 700,
                  color: "#8A9099", textTransform: "uppercase", letterSpacing: 0.4,
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map(cl => {
              const accent = cl.status === "hold" ? "#C22B2B" : cl.status === "review" ? "#E08A00" : "transparent";
              return (
                <tr
                  key={cl.id}
                  onClick={() => onOpenClaim(cl)}
                  style={{
                    borderBottom: "1px solid #F0F1F3", cursor: "pointer",
                    borderLeft: `3px solid ${accent}`,
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = "#FAFBFC"}
                  onMouseLeave={e => e.currentTarget.style.background = "#fff"}
                >
                  <td style={{ padding: "10px 6px 10px 12px" }} onClick={e => e.stopPropagation()}>
                    <input type="checkbox" checked={checked.includes(cl.id)} onChange={() => toggleCheck(cl.id)} />
                  </td>
                  <td style={{ padding: "10px 14px", fontFamily: "monospace", fontSize: 12.5, color: "#3A4048", fontWeight: 600 }}>{cl.id}</td>
                  <td style={{ padding: "10px 14px", fontWeight: 600, color: "#1A1D21" }}>{cl.patient}</td>
                  <td style={{ padding: "10px 14px", color: "#5B6470" }}>{cl.payer}</td>
                  <td style={{ padding: "10px 14px", color: "#5B6470" }}>{cl.serviceDate}</td>
                  <td style={{ padding: "10px 14px" }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: "#0E8298", background: "#E8F6F8", padding: "2px 8px", borderRadius: 5 }}>{cl.format}</span>
                  </td>
                  <td style={{ padding: "10px 14px" }}><ScoreBar score={cl.score} /></td>
                  <td style={{ padding: "10px 14px" }}><StatusBadge status={cl.status} /></td>
                  <td style={{ padding: "10px 14px", color: "#8A9099", fontSize: 12.5 }}>{cl.updated}</td>
                  <td style={{ padding: "10px 14px", color: "#8A9099" }}>···</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const btnPrimary = {
  background: "#0E8298", color: "#fff", border: "none", borderRadius: 7,
  padding: "7px 14px", fontSize: 12.5, fontWeight: 700, cursor: "pointer",
};
const btnGhost = {
  background: "#fff", color: "#3A4048", border: "1px solid #D7DBE0", borderRadius: 7,
  padding: "7px 14px", fontSize: 12.5, fontWeight: 700, cursor: "pointer",
};
const btnDanger = {
  background: "#fff", color: "#C22B2B", border: "1px solid #E8B3B3", borderRadius: 7,
  padding: "7px 14px", fontSize: 12.5, fontWeight: 700, cursor: "pointer",
};

/* ---------------------------- CLAIM DETAIL ---------------------------- */

function IssueRow({ issue }) {
  const [open, setOpen] = useState(false);
  const sev = SEV[issue.severity] || SEV.INFO;
  const Icon = sev.icon;
  const layer = LAYER_STYLE[issue.layer] || LAYER_STYLE.Deterministic;
  return (
    <div style={{ borderBottom: "1px solid #F0F1F3" }}>
      <div onClick={() => setOpen(o => !o)} style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "10px 4px", cursor: "pointer" }}>
        <Icon size={15} color={sev.color} style={{ marginTop: 2, flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontFamily: "monospace", fontSize: 11.5, fontWeight: 700, color: sev.color }}>{issue.code}</span>
            <span style={{ fontSize: 10, fontWeight: 700, color: layer.color, background: layer.bg, padding: "1px 6px", borderRadius: 4 }}>{issue.layer}</span>
            <span style={{ fontSize: 10, fontWeight: 700, color: "#8A9099", textTransform: "uppercase" }}>{sev.label}</span>
          </div>
          <div style={{ fontSize: 13, color: "#1A1D21", marginTop: 3 }}>{issue.msg}</div>
          <div style={{ fontSize: 11.5, color: "#8A9099", marginTop: 2, fontFamily: "monospace" }}>{issue.field}</div>
          {open && <div style={{ fontSize: 12.5, color: "#5B6470", fontStyle: "italic", marginTop: 6, background: "#FAFBFC", padding: "8px 10px", borderRadius: 6 }}>{issue.suggestion}</div>}
        </div>
        <ChevronDown size={14} color="#C2C7CD" style={{ transform: open ? "rotate(180deg)" : "none", flexShrink: 0, marginTop: 3 }} />
      </div>
    </div>
  );
}

function ValidationTab({ claim }) {
  const issueGroups = claim.issues || { structural: [], business: [] };
  return (
    <div>
      {[
        { key: "structural", title: "Structural Gate", sub: "Dedup · route match · payload conformity · financial check" },
        { key: "business", title: "Business Logic", sub: "Eligibility · prior auth · coding consistency · documentation · payer rules · submission readiness" },
      ].map(section => (
        <CollapsibleSection key={section.key} title={section.title} sub={section.sub} count={((Array.isArray(issueGroups[section.key]) ? issueGroups[section.key] : [])).length}>
          {((Array.isArray(issueGroups[section.key]) ? issueGroups[section.key] : [])).length === 0
            ? <div style={{ padding: "14px 4px", fontSize: 13, color: "#8A9099" }}>No issues found at this stage.</div>
            : ((Array.isArray(issueGroups[section.key]) ? issueGroups[section.key] : [])).map((iss, i) => <IssueRow key={i} issue={iss} />)}
        </CollapsibleSection>
      ))}
    </div>
  );
}

function CollapsibleSection({ title, sub, count, children }) {
  const [open, setOpen] = useState(true);
  return (
    <div style={{ border: "1px solid #E4E7EB", borderRadius: 10, marginBottom: 14, overflow: "hidden", background: "#fff" }}>
      <div onClick={() => setOpen(o => !o)} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 16px", cursor: "pointer", background: "#FAFBFC" }}>
        <div>
          <div style={{ fontSize: 13.5, fontWeight: 700, color: "#1A1D21" }}>{title} <span style={{ fontSize: 11.5, fontWeight: 700, color: "#8A9099" }}>({count})</span></div>
          <div style={{ fontSize: 11, color: "#8A9099", marginTop: 1, textTransform: "uppercase", letterSpacing: 0.3 }}>{sub}</div>
        </div>
        <ChevronDown size={15} color="#8A9099" style={{ transform: open ? "rotate(180deg)" : "none" }} />
      </div>
      {open && <div style={{ padding: "4px 16px" }}>{children}</div>}
    </div>
  );
}

function PayloadTab({ claim }) {
  const [raw, setRaw] = useState(false);
  const p = claim.payload || {};
  const lineItems = Array.isArray(p.lineItems) ? p.lineItems : [];
  const totals = {
    billed: Number(p.totals?.billed || 0),
    insurance: Number(p.totals?.insurance || 0),
    patient: Number(p.totals?.patient || 0),
  };
  return (
    <div>
      <div style={{ display: "flex", gap: 18, flexWrap: "wrap", fontSize: 12, marginBottom: 18, background: "#FAFBFC", border: "1px solid #E4E7EB", borderRadius: 8, padding: "10px 14px" }}>
        <span style={{ color: "#8A9099" }}>VERSION <b style={{ color: "#1A1D21" }}>{p.version}</b></span>
        <span style={{ color: "#8A9099" }}>FORMAT <b style={{ color: "#1A1D21" }}>{p.format}</b></span>
        <span style={{ color: "#8A9099" }}>SHA256 <b style={{ fontFamily: "monospace", color: "#1A1D21" }}>{p.hash}</b></span>
        <span style={{ color: "#8A9099" }}>GENERATED <b style={{ color: "#1A1D21" }}>{p.generated}</b></span>
      </div>

      <div style={{ fontSize: 11, fontWeight: 700, color: "#8A9099", textTransform: "uppercase", marginBottom: 8 }}>Line Items</div>
      <div style={{ border: "1px solid #E4E7EB", borderRadius: 10, overflow: "hidden", marginBottom: 18 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#FAFBFC", borderBottom: "1px solid #E4E7EB" }}>
              {["Code", "Description", "Tooth/Surface", "Fee"].map(h => (
                <th key={h} style={{ textAlign: "left", padding: "8px 12px", fontSize: 11, color: "#8A9099", fontWeight: 700, textTransform: "uppercase" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {lineItems.map((li, i) => (
              <tr key={i} style={{ borderBottom: "1px solid #F0F1F3" }}>
                <td style={{ padding: "9px 12px", fontFamily: "monospace", fontWeight: 700, color: "#0E8298" }}>{li.code}</td>
                <td style={{ padding: "9px 12px", color: "#1A1D21" }}>{li.desc}</td>
                <td style={{ padding: "9px 12px", color: "#5B6470" }}>{li.tooth}{li.surface !== "—" ? ` / ${li.surface}` : ""}</td>
                <td style={{ padding: "9px 12px", fontWeight: 600, color: "#1A1D21" }}>AED {Number(li.fee || 0).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ display: "flex", gap: 24, fontSize: 13, marginBottom: 18 }}>
        <div><div style={{ fontSize: 11, color: "#8A9099", textTransform: "uppercase" }}>Total Billed</div><div style={{ fontWeight: 700, color: "#1A1D21" }}>AED {totals.billed.toFixed(2)}</div></div>
        <div><div style={{ fontSize: 11, color: "#8A9099", textTransform: "uppercase" }}>Insurance Pays</div><div style={{ fontWeight: 700, color: "#15883E" }}>AED {totals.insurance.toFixed(2)}</div></div>
        <div><div style={{ fontSize: 11, color: "#8A9099", textTransform: "uppercase" }}>Patient Pays</div><div style={{ fontWeight: 700, color: "#C22B2B" }}>AED {totals.patient.toFixed(2)}</div></div>
      </div>

      <div onClick={() => setRaw(r => !r)} style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12.5, fontWeight: 700, color: "#0E8298", marginBottom: 8 }}>
        <ChevronDown size={13} style={{ transform: raw ? "rotate(180deg)" : "none" }} /> View Raw Payload
      </div>
      {raw && (
        <pre style={{
          background: "#1A1D21", color: "#9FE6D8", fontSize: 11.5, padding: 14, borderRadius: 8,
          overflowX: "auto", fontFamily: "monospace", lineHeight: 1.6,
        }}>{p.raw || `<Claim version="${p.version || "-"}" format="${p.format || "-"}">\n  <Hash>${p.hash || "-"}</Hash>\n${lineItems.map(li => `  <LineItem code="${li.code}" tooth="${li.tooth}" fee="${li.fee}"/>`).join("\n")}\n  <Totals billed="${totals.billed}" insurance="${totals.insurance}" patient="${totals.patient}"/>\n</Claim>`}</pre>
      )}
    </div>
  );
}

function PriorAuthTab({ claim }) {
  const pa = claim.priorAuth || { exists: false };
  if (!pa.exists) {
    return <div style={{ padding: "30px 0", textAlign: "center", color: "#8A9099", fontSize: 13.5 }}>Not required for this claim.</div>;
  }
  const statusColor = { Pending: COLOR_MAP.waiting, Approved: COLOR_MAP.ready, Denied: COLOR_MAP.hold, Expired: COLOR_MAP.submitted }[pa.status] || COLOR_MAP.waiting;
  const history = Array.isArray(pa.history) ? pa.history : [];
  return (
    <div>
      <div style={{ border: "1px solid #E4E7EB", borderRadius: 10, padding: 16, marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <span style={{ fontSize: 13.5, fontWeight: 700, color: "#1A1D21" }}>{pa.ref}</span>
          <span style={{ fontSize: 11.5, fontWeight: 700, color: statusColor, background: `${statusColor}14`, padding: "3px 10px", borderRadius: 999, textTransform: "uppercase" }}>{pa.status}</span>
        </div>
        <div style={{ fontSize: 12.5, color: "#8A9099", marginBottom: 4 }}>Submitted {pa.submitted}</div>
        {pa.response && <div style={{ fontSize: 13, color: "#3A4048" }}>{pa.response}</div>}

        {pa.status === "Pending" && (
          <div style={{ marginTop: 14, background: "#E8F2FC", border: "1px solid #BFDBF7", borderRadius: 8, padding: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#1864AB", marginBottom: 6 }}>Waiting for Payer</div>
            <div style={{ fontSize: 12.5, color: "#3A4048", lineHeight: 1.7 }}>
              Waiting since {pa.waitingSince}<br />
              Next poll at {pa.nextPoll}<br />
              Attempts so far: {pa.attempts}
            </div>
            <button style={{ ...btnGhost, marginTop: 10, display: "inline-flex", alignItems: "center", gap: 6 }}>
              <RefreshCw size={12} /> Check Status Now
            </button>
          </div>
        )}
      </div>

      {history.length > 0 && (
        <CollapsibleSection title="Past Attempts" sub="History" count={history.length}>
          {history.map((h, i) => (
            <div key={i} style={{ display: "flex", gap: 10, padding: "8px 4px", borderBottom: "1px solid #F0F1F3", fontSize: 12.5 }}>
              <span style={{ fontFamily: "monospace", color: "#8A9099" }}>{h.ts}</span>
              <span style={{ color: "#3A4048" }}>{h.note}</span>
            </div>
          ))}
        </CollapsibleSection>
      )}
    </div>
  );
}

function EligibilityTab({ claim }) {
  const el = claim.eligibility || {
    status: "Unknown",
    checkedAgo: "-",
    validUntil: "-",
    memberId: "-",
    coverageRef: "-",
    voiFlag: null,
    benefits: [],
  };
  const benefits = Array.isArray(el.benefits) ? el.benefits : [];
  const voiFlag = el.voiFlag;
  const voiCopy = { missing: ["DAMAN VOI — Missing", COLOR_MAP.hold], expired: ["DAMAN VOI — Expired", COLOR_MAP.hold], verified: ["DAMAN VOI — Verified", COLOR_MAP.ready] };
  return (
    <div>
      <div style={{ border: "1px solid #E4E7EB", borderRadius: 10, padding: 16, marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <span style={{
            fontSize: 11.5, fontWeight: 700, textTransform: "uppercase", padding: "3px 10px", borderRadius: 999,
            color: el.status === "Active" ? COLOR_MAP.ready : COLOR_MAP.hold,
            background: el.status === "Active" ? "#E7F6EC" : "#FCE9E9",
          }}>{el.status}</span>
          <span style={{ fontSize: 12, color: "#8A9099" }}>Checked {el.checkedAgo} · valid until {el.validUntil}</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, fontSize: 13, marginBottom: 10 }}>
          <div><div style={{ fontSize: 11, color: "#8A9099", textTransform: "uppercase" }}>Member ID</div><div style={{ fontFamily: "monospace", color: "#1A1D21" }}>{el.memberId}</div></div>
          <div><div style={{ fontSize: 11, color: "#8A9099", textTransform: "uppercase" }}>Coverage Ref</div><div style={{ fontFamily: "monospace", color: "#1A1D21" }}>{el.coverageRef}</div></div>
        </div>
        {benefits.map((b, i) => (
          <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderTop: "1px solid #F0F1F3", fontSize: 13 }}>
            <span style={{ color: "#8A9099" }}>{b.k}</span><span style={{ fontWeight: 600, color: "#1A1D21" }}>{b.v}</span>
          </div>
        ))}
        <button style={{ ...btnGhost, marginTop: 12, display: "inline-flex", alignItems: "center", gap: 6 }}>
          <RefreshCw size={12} /> Re-check Eligibility
        </button>
      </div>

      {claim.payer === "Daman" && voiCopy[voiFlag] && (
        <div style={{
          display: "flex", alignItems: "center", gap: 8, padding: "10px 14px", borderRadius: 8,
          background: `${voiCopy[voiFlag][1]}14`, border: `1px solid ${voiCopy[voiFlag][1]}33`,
        }}>
          <Shield size={14} color={voiCopy[voiFlag][1]} />
          <span style={{ fontSize: 13, fontWeight: 700, color: voiCopy[voiFlag][1] }}>{voiCopy[voiFlag][0]}</span>
        </div>
      )}
    </div>
  );
}

function AuditTab({ claim }) {
  const [agentFilter, setAgentFilter] = useState("all");
  const audit = Array.isArray(claim.audit) ? claim.audit : [];
  const agents = ["all", ...Array.from(new Set(audit.map(a => a.agent)))];
  const rows = audit.filter(a => agentFilter === "all" || a.agent === agentFilter);
  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        {agents.map(a => (
          <button key={a} onClick={() => setAgentFilter(a)} style={{
            fontSize: 12, fontWeight: 600, padding: "5px 11px", borderRadius: 7, cursor: "pointer",
            border: agentFilter === a ? "1.5px solid #0E8298" : "1px solid #E4E7EB",
            background: agentFilter === a ? "#E8F6F8" : "#fff",
            color: agentFilter === a ? "#0E8298" : "#5B6470",
          }}>{a === "all" ? "All Agents" : a}</button>
        ))}
      </div>
      <div style={{ borderLeft: "2px solid #E4E7EB", marginLeft: 6 }}>
        {rows.map((a, i) => (
          <div key={i} style={{ display: "flex", gap: 12, padding: "10px 0 10px 18px", position: "relative" }}>
            <span style={{ position: "absolute", left: -7, top: 14, width: 10, height: 10, borderRadius: "50%", background: a.color, border: "2px solid #fff", boxShadow: "0 0 0 1px " + a.color }} />
            <span style={{ fontFamily: "monospace", fontSize: 11.5, color: "#8A9099", minWidth: 70 }}>{a.ts}</span>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#0E8298", minWidth: 90 }}>{a.agent}</span>
            <span style={{ fontSize: 12.5, color: "#1A1D21", fontWeight: 600 }}>{a.node}</span>
            <span style={{ fontSize: 11, fontWeight: 700, color: a.color, textTransform: "uppercase" }}>{a.type}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Toast({ message, onClose }) {
  React.useEffect(() => { const t = setTimeout(onClose, 2800); return () => clearTimeout(t); }, [onClose]);
  return (
    <div style={{
      position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)",
      background: "#1A1D21", color: "#fff", padding: "11px 20px", borderRadius: 9,
      display: "flex", alignItems: "center", gap: 8, fontSize: 13.5, fontWeight: 600,
      boxShadow: "0 8px 24px rgba(0,0,0,.2)", zIndex: 100,
    }}>
      <Check size={15} color="#3FD0E0" /> {message}
    </div>
  );
}

function Modal({ title, children, onClose }) {
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(20,24,28,.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 90 }}>
      <div style={{ background: "#fff", borderRadius: 12, width: 440, maxWidth: "90vw", padding: 22, boxShadow: "0 20px 60px rgba(0,0,0,.25)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: "#1A1D21" }}>{title}</span>
          <X size={17} color="#8A9099" style={{ cursor: "pointer" }} onClick={onClose} />
        </div>
        {children}
      </div>
    </div>
  );
}

function ClaimDetail({ claim, onBack, onUpdateStatus }) {
  const [tab, setTab] = useState("validation");
  const [toast, setToast] = useState(null);
  const [modal, setModal] = useState(null);
  const [reason, setReason] = useState("");

  const tabs = [
    { key: "validation", label: "Validation Report", icon: ListChecks },
    { key: "payload", label: "Claim Payload", icon: FileText },
    { key: "priorauth", label: "Prior Authorization", icon: Shield },
    { key: "eligibility", label: "Eligibility", icon: Activity },
    { key: "audit", label: "Audit Trail", icon: Clock },
  ];

  const approve = () => setModal("approve");
  const sendBack = () => setModal("sendback");
  const forceSubmit = () => setModal("force");
  const escalate = () => {
    runDesignClaimAction(claim.id, "escalate").catch((error) => {
      console.warn("Escalation API call failed.", error);
    });
    setToast("Claim escalated.");
  };

  const confirmApprove = () => { onUpdateStatus(claim.id, "submitted"); setModal(null); setToast("Claim approved and submitted."); };
  const confirmSendBack = () => {
    runDesignClaimAction(claim.id, "send_back", { reason }).catch((error) => {
      console.warn("Send-back API call failed.", error);
    });
    setModal(null); setReason(""); onBack(); setToast("Sent back for edit.");
  };
  const confirmForce = () => { if (!reason.trim()) return; onUpdateStatus(claim.id, "submitted", { reason, override: true }); setModal(null); setReason(""); setToast("Submitted via override."); };

  if (!claim) {
    return (
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "20px 28px 60px" }}>
        <div onClick={onBack} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: "#0E8298", cursor: "pointer", marginBottom: 14 }}>
          <ArrowLeft size={14} /> Back to Claims Queue
        </div>
        <div style={{ background: "#fff", border: "1px solid #E4E7EB", borderRadius: 10, padding: 22, color: "#5B6470" }}>
          This claim could not be opened from the current queue data. Refresh the queue and try again.
        </div>
      </div>
    );
  }

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: "20px 28px 60px" }}>
      <div onClick={onBack} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: "#0E8298", cursor: "pointer", marginBottom: 14 }}>
        <ArrowLeft size={14} /> Back to Claims Queue
      </div>

      <div style={{
        background: "linear-gradient(120deg, #0B5C6B, #14899C 55%, #1AB6C9)",
        borderRadius: 14, padding: "22px 26px", color: "#fff", marginBottom: 18,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 14 }}>
          <div>
            <div style={{ fontSize: 22, fontWeight: 800 }}>{claim.patient}</div>
            <div style={{ fontSize: 13, opacity: 0.9, marginTop: 4, display: "flex", gap: 14, flexWrap: "wrap" }}>
              <span style={{ fontFamily: "monospace" }}>{claim.id}</span>
              <span>{claim.payer} · {claim.plan}</span>
              <span>{claim.jurisdiction}</span>
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: 11, opacity: 0.85, textTransform: "uppercase" }}>Validation Score</div>
              <div style={{ fontSize: 24, fontWeight: 800 }}>{claim.score}<span style={{ fontSize: 14, fontWeight: 600, opacity: 0.85 }}>/100</span></div>
            </div>
            <StatusBadge status={claim.status} size="lg" />
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, marginTop: 18, flexWrap: "wrap" }}>
          <button onClick={approve} disabled={claim.status === "submitted"} style={{ ...btnPrimary, background: "#fff", color: "#0E8298", opacity: claim.status === "submitted" ? 0.5 : 1 }}>Approve & Submit</button>
          <button onClick={sendBack} style={{ ...btnGhost, background: "rgba(255,255,255,.12)", color: "#fff", border: "1px solid rgba(255,255,255,.4)" }}>Send Back for Edit</button>
          <button onClick={escalate} style={{ ...btnGhost, background: "rgba(255,255,255,.12)", color: "#fff", border: "1px solid rgba(255,255,255,.4)" }}>Escalate</button>
          <button onClick={forceSubmit} style={{ ...btnGhost, background: "rgba(194,43,43,.18)", color: "#FFD7D7", border: "1px solid rgba(255,255,255,.4)" }}>Override & Force Submit</button>
        </div>
      </div>

      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid #E4E7EB", marginBottom: 20 }}>
        {tabs.map(t => {
          const Icon = t.icon;
          const active = tab === t.key;
          return (
            <div key={t.key} onClick={() => setTab(t.key)} style={{
              display: "flex", alignItems: "center", gap: 6, padding: "10px 14px", cursor: "pointer",
              fontSize: 13, fontWeight: 600, color: active ? "#0E8298" : "#8A9099",
              borderBottom: active ? "2.5px solid #0E8298" : "2.5px solid transparent",
            }}>
              <Icon size={14} /> {t.label}
            </div>
          );
        })}
      </div>

      {tab === "validation" && <ValidationTab claim={claim} />}
      {tab === "payload" && <PayloadTab claim={claim} />}
      {tab === "priorauth" && <PriorAuthTab claim={claim} />}
      {tab === "eligibility" && <EligibilityTab claim={claim} />}
      {tab === "audit" && <AuditTab claim={claim} />}

      {modal === "approve" && (
        <Modal title="Approve & Submit Claim" onClose={() => setModal(null)}>
          <p style={{ fontSize: 13.5, color: "#3A4048", lineHeight: 1.6 }}>This will submit <b>{claim.id}</b> to {claim.payer} via {claim.format}. This action can't be undone.</p>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
            <button style={btnGhost} onClick={() => setModal(null)}>Cancel</button>
            <button style={btnPrimary} onClick={confirmApprove}>Confirm & Submit</button>
          </div>
        </Modal>
      )}

      {modal === "sendback" && (
        <Modal title="Send Back for Edit" onClose={() => setModal(null)}>
          <label style={{ fontSize: 11.5, fontWeight: 700, color: "#8A9099", textTransform: "uppercase" }}>Reason for sending back</label>
          <textarea
            value={reason} onChange={e => setReason(e.target.value)}
            placeholder="e.g. Missing tooth number on line item 2, add VOI reference…"
            style={{ width: "100%", minHeight: 80, marginTop: 6, padding: 10, fontSize: 13, border: "1px solid #E4E7EB", borderRadius: 8, resize: "vertical", fontFamily: "inherit" }}
          />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
            <button style={btnGhost} onClick={() => setModal(null)}>Cancel</button>
            <button style={btnPrimary} onClick={confirmSendBack}>Send Back</button>
          </div>
        </Modal>
      )}

      {modal === "force" && (
        <Modal title="Override & Force Submit" onClose={() => setModal(null)}>
          <div style={{ display: "flex", gap: 8, background: "#FCE9E9", border: "1px solid #F3C2C2", borderRadius: 8, padding: 10, marginBottom: 12 }}>
            <AlertTriangle size={15} color="#C22B2B" style={{ flexShrink: 0, marginTop: 1 }} />
            <span style={{ fontSize: 12.5, color: "#7A1F1F" }}>This bypasses outstanding validation issues. A written reason is required and will be logged to the audit trail.</span>
          </div>
          <label style={{ fontSize: 11.5, fontWeight: 700, color: "#8A9099", textTransform: "uppercase" }}>Reason (required)</label>
          <textarea
            value={reason} onChange={e => setReason(e.target.value)}
            placeholder="Explain why this claim must be force-submitted…"
            style={{ width: "100%", minHeight: 80, marginTop: 6, padding: 10, fontSize: 13, border: "1px solid #E4E7EB", borderRadius: 8, resize: "vertical", fontFamily: "inherit" }}
          />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
            <button style={btnGhost} onClick={() => setModal(null)}>Cancel</button>
            <button style={{ ...btnDanger, background: reason.trim() ? "#C22B2B" : "#F3C2C2", color: "#fff", border: "none", cursor: reason.trim() ? "pointer" : "not-allowed" }} onClick={confirmForce} disabled={!reason.trim()}>Force Submit</button>
          </div>
        </Modal>
      )}

      {toast && <Toast message={toast} onClose={() => setToast(null)} />}
    </div>
  );
}

/* ---------------------------- ROOT ---------------------------- */

export default function VeloClaim() {
  const [view, setView] = useState("queue");
  const [activeClaim, setActiveClaim] = useState(null);
  const [data, setData] = useState(fallbackClaims);

  React.useEffect(() => {
    let mounted = true;

    fetchDesignClaims()
      .then((result) => {
        if (mounted && result.source === "backend") {
          setData(result.claims);
        } else if (mounted && result.claims.length) {
          setData(result.claims);
        }
      })
      .catch((error) => {
        console.warn("Velo Claim API unavailable, using fallback claims.", error);
      });

    return () => {
      mounted = false;
    };
  }, []);

  const handleOpenClaim = (cl) => { setActiveClaim(cl); setView("detail"); };
  const handleBack = () => setView("queue");
  const handleUpdateStatus = (id, status, metadata = {}) => {
    setData(d => d.map(c => c.id === id ? { ...c, status } : c));
    setActiveClaim(c => c ? { ...c, status } : c);
    updateDesignClaimStatus(id, status, metadata).catch((error) => {
      console.warn("Status update API call failed.", error);
    });
  };

  return (
    <div style={{ fontFamily: "Inter, -apple-system, BlinkMacSystemFont, sans-serif", background: "#F6F7F8", minHeight: "100vh" }}>
      <TopBar />
      {view === "queue"
        ? <ClaimsQueue claims={data} onOpenClaim={handleOpenClaim} />
        : <ClaimDetail claim={activeClaim} onBack={handleBack} onUpdateStatus={handleUpdateStatus} />}
    </div>
  );
}
