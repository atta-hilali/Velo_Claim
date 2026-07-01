import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  Bell,
  Check,
  ChevronRight,
  Clock,
  Copy,
  Database,
  FileCode2,
  FileText,
  Filter,
  Hash,
  Layers3,
  RefreshCw,
  Search,
  Shield,
  Sparkles,
  Stethoscope,
  UploadCloud,
  X,
} from "lucide-react";
import { fetchClaims, runClaimAction, updateClaimStatus } from "./api.js";

const statusMeta = {
  ready: { label: "Ready to Submit", tone: "green", dot: "ready" },
  READY_TO_SUBMIT: { label: "Ready to Submit", tone: "green", dot: "ready" },
  needs_review: { label: "Needs Review", tone: "amber", dot: "review" },
  NEEDS_REVIEW: { label: "Needs Review", tone: "amber", dot: "review" },
  review: { label: "Needs Review", tone: "amber", dot: "review" },
  hold: { label: "Hold Critical", tone: "red", dot: "hold" },
  HOLD_CRITICAL: { label: "Hold Critical", tone: "red", dot: "hold" },
  waiting: { label: "Waiting on Payer", tone: "blue", dot: "waiting" },
  WAITING_FOR_PAYER: { label: "Waiting on Payer", tone: "blue", dot: "waiting" },
  submitted: { label: "Submitted", tone: "gray", dot: "submitted" },
};

const severityMeta = {
  critical: { label: "Critical", icon: AlertCircle, tone: "red" },
  CRITICAL: { label: "Critical", icon: AlertCircle, tone: "red" },
  error: { label: "Error", icon: AlertTriangle, tone: "orange" },
  ERROR: { label: "Error", icon: AlertTriangle, tone: "orange" },
  warning: { label: "Warning", icon: AlertTriangle, tone: "amber" },
  WARNING: { label: "Warning", icon: AlertTriangle, tone: "amber" },
  info: { label: "Info", icon: Bell, tone: "blue" },
  INFO: { label: "Info", icon: Bell, tone: "blue" },
};

const tabs = [
  { id: "validation", label: "Validation", icon: Shield },
  { id: "payload", label: "Built Claim", icon: FileCode2 },
  { id: "priorAuth", label: "Prior Auth", icon: UploadCloud },
  { id: "eligibility", label: "Eligibility", icon: Stethoscope },
  { id: "audit", label: "Audit", icon: Layers3 },
];

function normalizeStatus(status) {
  return statusMeta[status] ? status : "needs_review";
}

function getChecks(claim) {
  return claim?.validation?.checks || [];
}

function getAllIssues(claim) {
  return getChecks(claim).flatMap((check) =>
    (check.issues || []).map((issue) => ({ ...issue, checkName: check.name })),
  );
}

function scoreTone(score) {
  if (score >= 85) return "green";
  if (score >= 60) return "amber";
  return "red";
}

function StatusBadge({ status }) {
  const meta = statusMeta[normalizeStatus(status)];
  return (
    <span className={`status-badge ${meta.tone}`}>
      <span className={`status-dot ${meta.dot}`} />
      {meta.label}
    </span>
  );
}

function ScoreRing({ score }) {
  const tone = scoreTone(score);
  return (
    <div className={`score-ring ${tone}`} aria-label={`Claim score ${score}`}>
      <span>{score}</span>
      <small>/100</small>
    </div>
  );
}

function EmptyState({ icon: Icon = FileText, title, text }) {
  return (
    <div className="empty-state">
      <Icon size={22} />
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}

function TopBar({ source, apiError, onRefresh, loading }) {
  return (
    <header className="top-bar">
      <div className="brand-lockup">
        <div className="brand-mark">VC</div>
        <div>
          <h1>Velo Claim</h1>
          <p>RCM claim operations console</p>
        </div>
      </div>
      <div className="top-actions">
        <div className={`api-pill ${source === "backend" ? "live" : "demo"}`}>
          <Database size={16} />
          {source === "backend" ? "Backend connected" : "Demo adapter"}
        </div>
        {apiError ? <div className="api-error">{apiError}</div> : null}
        <button className="icon-button" onClick={onRefresh} disabled={loading} title="Refresh claims">
          <RefreshCw size={18} className={loading ? "spin" : ""} />
        </button>
      </div>
    </header>
  );
}

function QueueItem({ claim, selected, onSelect }) {
  const issueCount = getAllIssues(claim).length;

  return (
    <button className={`queue-item ${selected ? "selected" : ""}`} onClick={onSelect}>
      <div className="queue-main">
        <div>
          <strong>{claim.id}</strong>
          <span>{claim.patient}</span>
        </div>
        <ScoreRing score={claim.score} />
      </div>
      <div className="queue-meta">
        <span>{claim.payer}</span>
        <span>{claim.standard}</span>
        <span>{claim.amount}</span>
      </div>
      <div className="queue-footer">
        <StatusBadge status={claim.status} />
        <span className="issue-count">{issueCount} issues</span>
      </div>
    </button>
  );
}

function ClaimsQueue({ claims, selectedId, onSelect }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");

  const filteredClaims = useMemo(() => {
    const q = query.trim().toLowerCase();
    return claims.filter((claim) => {
      const matchesText =
        !q ||
        [claim.id, claim.patient, claim.mrn, claim.payer, claim.standard, claim.jurisdiction]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(q));
      const meta = statusMeta[normalizeStatus(claim.status)];
      const matchesStatus = status === "all" || meta.dot === status;
      return matchesText && matchesStatus;
    });
  }, [claims, query, status]);

  return (
    <aside className="claims-queue">
      <div className="queue-toolbar">
        <div className="search-box">
          <Search size={17} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search claims" />
        </div>
        <div className="filter-box">
          <Filter size={16} />
          <select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Filter status">
            <option value="all">All</option>
            <option value="ready">Ready</option>
            <option value="review">Review</option>
            <option value="hold">Hold</option>
            <option value="waiting">Waiting</option>
            <option value="submitted">Submitted</option>
          </select>
        </div>
      </div>
      <div className="queue-list">
        {filteredClaims.map((claim) => (
          <QueueItem
            key={claim.id}
            claim={claim}
            selected={claim.id === selectedId}
            onSelect={() => onSelect(claim.id)}
          />
        ))}
        {!filteredClaims.length ? (
          <EmptyState icon={Search} title="No claims found" text="Try another status or search term." />
        ) : null}
      </div>
    </aside>
  );
}

function DetailHeader({ claim, onAction }) {
  return (
    <section className="detail-header">
      <div className="claim-title">
        <button className="back-button" type="button" aria-label="Back to queue">
          <ChevronRight size={18} />
        </button>
        <div>
          <div className="eyebrow">{claim.standard} / {claim.jurisdiction}</div>
          <h2>{claim.id}</h2>
          <p>{claim.patient} - {claim.mrn} - {claim.payer} - {claim.plan}</p>
        </div>
      </div>
      <div className="header-metrics">
        <ScoreRing score={claim.score} />
        <div className="metric-stack">
          <StatusBadge status={claim.status} />
          <span>{claim.serviceDate}</span>
          <span>{claim.updated}</span>
        </div>
      </div>
      <div className="header-actions">
        <button className="secondary-button" onClick={() => onAction("run_eligibility")}>
          <Stethoscope size={17} />
          Eligibility
        </button>
        <button className="secondary-button" onClick={() => onAction("run_prior_auth")}>
          <UploadCloud size={17} />
          Prior Auth
        </button>
        <button className="primary-button" onClick={() => onAction("mark_ready")}>
          <Check size={17} />
          Mark Ready
        </button>
      </div>
    </section>
  );
}

function IssueRow({ issue }) {
  const meta = severityMeta[issue.severity] || severityMeta.warning;
  const Icon = meta.icon;

  return (
    <div className={`issue-row ${meta.tone}`}>
      <Icon size={18} />
      <div>
        <div className="issue-head">
          <strong>{issue.code || "ISSUE"}</strong>
          <span>{issue.checkName}</span>
          {issue.field ? <code>{issue.field}</code> : null}
        </div>
        <p>{issue.message || issue.msg}</p>
        {issue.fix || issue.suggestion ? <small>{issue.fix || issue.suggestion}</small> : null}
      </div>
    </div>
  );
}

function ValidationTab({ claim }) {
  const checks = getChecks(claim);
  const issues = getAllIssues(claim);

  return (
    <div className="tab-grid">
      <section className="panel">
        <div className="panel-heading">
          <h3>Validation Checks</h3>
          <span>{checks.length} checks</span>
        </div>
        <div className="check-list">
          {checks.map((check) => (
            <div className={`check-card ${check.status}`} key={check.name}>
              <div>
                <strong>{check.name}</strong>
                <span>{(check.issues || []).length} issue(s)</span>
              </div>
              <StatusIcon status={check.status} />
            </div>
          ))}
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h3>Issues</h3>
          <span>{issues.length} total</span>
        </div>
        <div className="issue-list">
          {issues.length ? (
            issues.map((issue, index) => <IssueRow issue={issue} key={`${issue.code}-${index}`} />)
          ) : (
            <EmptyState icon={Check} title="No blocking issues" text="The claim can move to submission flow." />
          )}
        </div>
      </section>
    </div>
  );
}

function StatusIcon({ status }) {
  if (status === "pass") return <Check className="check-icon pass" size={18} />;
  if (status === "waiting") return <Clock className="check-icon waiting" size={18} />;
  if (status === "warn") return <AlertTriangle className="check-icon warn" size={18} />;
  return <AlertCircle className="check-icon fail" size={18} />;
}

function PayloadTab({ claim, onCopy }) {
  const builtClaim = claim.builtClaim || {};

  return (
    <div className="payload-layout">
      <section className="panel">
        <div className="panel-heading">
          <h3>Built Claim Payload</h3>
          <button className="secondary-button compact" onClick={() => onCopy(builtClaim.payload)}>
            <Copy size={16} />
            Copy
          </button>
        </div>
        <div className="payload-meta">
          <InfoTile icon={FileCode2} label="Type" value={builtClaim.payloadType || "-"} />
          <InfoTile icon={Hash} label="Hash" value={builtClaim.hash || "-"} />
          <InfoTile icon={Shield} label="Schema" value={builtClaim.schemaStatus || "-"} />
          <InfoTile icon={Clock} label="Generated" value={builtClaim.generatedAt || "-"} />
        </div>
        <pre className="payload-code">{builtClaim.payload || "No payload available."}</pre>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h3>Routing Context</h3>
        </div>
        <dl className="definition-list">
          <div><dt>Claim standard</dt><dd>{claim.route?.claimStandard || claim.standard}</dd></div>
          <div><dt>Prior auth</dt><dd>{claim.route?.priorAuthStandard || "-"}</dd></div>
          <div><dt>Eligibility</dt><dd>{claim.route?.eligibilityStandard || "-"}</dd></div>
          <div><dt>Payer portal</dt><dd>{claim.route?.payerPortal || "-"}</dd></div>
          <div><dt>Stored object</dt><dd>{builtClaim.objectUri || "-"}</dd></div>
        </dl>
      </section>
    </div>
  );
}

function InfoTile({ icon: Icon, label, value }) {
  return (
    <div className="info-tile">
      <Icon size={18} />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function PriorAuthTab({ claim, onAction }) {
  const pa = claim.priorAuth || {};

  return (
    <section className="panel">
      <div className="panel-heading">
        <h3>Prior Authorization</h3>
        <button className="secondary-button compact" onClick={() => onAction("run_prior_auth")}>
          <UploadCloud size={16} />
          Run PA Flow
        </button>
      </div>
      <div className="summary-grid">
        <InfoTile icon={Shield} label="Status" value={pa.status || "-"} />
        <InfoTile icon={FileText} label="Request" value={pa.requestStatus || "-"} />
        <InfoTile icon={Hash} label="Pre-auth ref" value={pa.preAuthRef || "Not attached"} />
        <InfoTile icon={Layers3} label="Codes" value={(pa.requiredCodes || []).join(", ") || "None"} />
      </div>
      <div className="next-step">
        <Sparkles size={18} />
        <span>{pa.nextStep || "No prior authorization action is required."}</span>
      </div>
    </section>
  );
}

function EligibilityTab({ claim, onAction }) {
  const eligibility = claim.eligibility || {};

  return (
    <section className="panel">
      <div className="panel-heading">
        <h3>Eligibility</h3>
        <button className="secondary-button compact" onClick={() => onAction("run_eligibility")}>
          <Stethoscope size={16} />
          Run Eligibility
        </button>
      </div>
      <div className="summary-grid">
        <InfoTile icon={Check} label="Status" value={eligibility.status || "-"} />
        <InfoTile icon={Clock} label="Checked" value={eligibility.checkedAt || "-"} />
        <InfoTile icon={Hash} label="Reference" value={eligibility.reference || "-"} />
        <InfoTile icon={Layers3} label="Method" value={eligibility.method || "-"} />
      </div>
      <div className="next-step">
        <Stethoscope size={18} />
        <span>{eligibility.notes || "No eligibility response stored yet."}</span>
      </div>
    </section>
  );
}

function AuditTab({ claim }) {
  const events = claim.audit || [];

  return (
    <section className="panel">
      <div className="panel-heading">
        <h3>Audit Trail</h3>
        <span>{events.length} events</span>
      </div>
      <div className="timeline">
        {events.map((event, index) => (
          <div className="timeline-row" key={`${event.node}-${event.at}-${index}`}>
            <span>{event.at || event.timestamp}</span>
            <div>
              <strong>{event.node}</strong>
              <p>{event.detail || event.message}</p>
            </div>
            <StatusBadge status={event.status} />
          </div>
        ))}
        {!events.length ? (
          <EmptyState icon={Clock} title="No audit events" text="Audit events will appear after backend storage is connected." />
        ) : null}
      </div>
    </section>
  );
}

function ClaimDetail({ claim, activeTab, setActiveTab, onAction, onCopy }) {
  return (
    <main className="claim-detail">
      <DetailHeader claim={claim} onAction={onAction} />
      <nav className="tabs" aria-label="Claim detail sections">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              className={activeTab === tab.id ? "active" : ""}
              onClick={() => setActiveTab(tab.id)}
            >
              <Icon size={17} />
              {tab.label}
            </button>
          );
        })}
      </nav>
      <div className="tab-content">
        {activeTab === "validation" ? <ValidationTab claim={claim} /> : null}
        {activeTab === "payload" ? <PayloadTab claim={claim} onCopy={onCopy} /> : null}
        {activeTab === "priorAuth" ? <PriorAuthTab claim={claim} onAction={onAction} /> : null}
        {activeTab === "eligibility" ? <EligibilityTab claim={claim} onAction={onAction} /> : null}
        {activeTab === "audit" ? <AuditTab claim={claim} /> : null}
      </div>
    </main>
  );
}

function Toast({ toast, onClose }) {
  if (!toast) return null;
  return (
    <div className={`toast ${toast.type || "info"}`}>
      <span>{toast.message}</span>
      <button onClick={onClose} aria-label="Dismiss notification">
        <X size={16} />
      </button>
    </div>
  );
}

export default function App() {
  const [claims, setClaims] = useState([]);
  const [source, setSource] = useState("demo");
  const [apiError, setApiError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [activeTab, setActiveTab] = useState("validation");
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState(null);

  const selectedClaim = useMemo(
    () => claims.find((claim) => claim.id === selectedId) || claims[0],
    [claims, selectedId],
  );

  async function loadClaims() {
    setLoading(true);
    const result = await fetchClaims();
    setClaims(result.claims);
    setSource(result.source);
    setApiError(result.error);
    setSelectedId((current) => current || result.claims[0]?.id || null);
    setLoading(false);
  }

  useEffect(() => {
    loadClaims();
  }, []);

  async function handleAction(action) {
    if (!selectedClaim) return;

    if (action === "mark_ready") {
      await updateClaimStatus(selectedClaim.id, "ready", "Marked ready from console");
      setClaims((current) =>
        current.map((claim) =>
          claim.id === selectedClaim.id ? { ...claim, status: "ready", score: Math.max(claim.score, 85) } : claim,
        ),
      );
      setToast({ type: "success", message: `${selectedClaim.id} marked ready in the console.` });
      return;
    }

    const response = await runClaimAction(selectedClaim.id, action);
    setToast({
      type: "info",
      message:
        response.source === "demo"
          ? `${action.replaceAll("_", " ")} queued in demo mode.`
          : `${action.replaceAll("_", " ")} requested.`,
    });
  }

  async function handleCopy(value) {
    if (!value) return;
    await navigator.clipboard.writeText(value);
    setToast({ type: "success", message: "Payload copied to clipboard." });
  }

  return (
    <div className="app-shell">
      <TopBar source={source} apiError={apiError} onRefresh={loadClaims} loading={loading} />
      <div className="workspace">
        <ClaimsQueue claims={claims} selectedId={selectedClaim?.id} onSelect={setSelectedId} />
        {selectedClaim ? (
          <ClaimDetail
            claim={selectedClaim}
            activeTab={activeTab}
            setActiveTab={setActiveTab}
            onAction={handleAction}
            onCopy={handleCopy}
          />
        ) : (
          <main className="claim-detail centered">
            <EmptyState icon={FileText} title="No claims loaded" text="Connect an API or reload the demo adapter." />
          </main>
        )}
      </div>
      <Toast toast={toast} onClose={() => setToast(null)} />
    </div>
  );
}
