/**
 * DevMesh Mobile — Triage App
 *
 * Wire protocol matches backend/CHANGELOG.md (July 9, 2026 session), PLUS
 * one new pair of messages added in this pass — NOT YET in CHANGELOG.md,
 * needs to be communicated to Hardik since his round-trip verdict step is
 * what will answer these:
 *
 *   Backend -> mobile (every broadcast):
 *   {
 *     "commit": { "id", "short_id", "author", "author_email", "message", "timestamp" },
 *     "file": "auth.py",
 *     "findings": [{ "id", "severity", "line", "description", "fix" }]
 *   }
 *
 *   Mobile -> backend:
 *   { "type": "finding_decision", "finding_id": "f3", "decision": "approved" }
 *   { "type": "finding_decision", "finding_id": "f7", "decision": "false_positive", "comment": "..." }
 *   { "type": "request_verdicts", "finding_ids": ["f7", "f9"] }   // NEW — sent only on Generate Report tap, for FP findings without a verdict yet
 *   { "type": "generate_report" }
 *
 *   Backend -> mobile (replies):
 *   { "type": "verdict", "finding_id": "f7", "verdict": "accepted" | "needs_review", "note": "..." }  // NEW — one per finding_id in a request_verdicts batch
 *   { "type": "report_ready", "path": "..." }
 *   { "type": "report_error", "message": "..." }
 *   { "type": "decision_error", "message": "..." }
 *
 * NEW BEHAVIOR IN THIS PASS:
 * 1. Generate Report is the trigger point for FP re-verification — not
 *    automatic the moment something's marked false positive. Tapping the
 *    button: if any false_positive-decided finding has no verdict yet,
 *    sends `request_verdicts` for just those, shows a "Verifying false
 *    positives…" state, and waits. Once every outstanding FP has a verdict
 *    (accepted OR needs_review — either counts as settled, no forced
 *    convergence), `generate_report` auto-fires. If there are no
 *    outstanding FPs, it skips straight to `generate_report`.
 * 2. A "Cancel" affordance appears during the verifying state so a stuck
 *    wait (e.g. backend not wired up yet during dev) doesn't permanently
 *    lock the button — cancelling only resets the report-request state,
 *    decisions are untouched.
 * 3. On `report_ready`, a native Alert confirms the report location. On
 *    the developer tapping OK, the entire per-review-run state (findings,
 *    decisions, reasons, verdicts, commit) resets to a clean slate ready
 *    for the next commit — the socket connection itself is left alone.
 * 4. Findings now track verdict state per id and surface it in the card
 *    (mirrors the report template's approved / false_positive /
 *    needs_review vocabulary) so triage and the final report never
 *    disagree about a finding's status.
 */

import React, { useEffect, useRef, useState, useCallback } from "react";
import {
  SafeAreaView,
  View,
  Text,
  FlatList,
  StyleSheet,
  TouchableOpacity,
  StatusBar,
  Platform,
  Modal,
  TextInput,
  KeyboardAvoidingView,
  Alert,
} from "react-native";

// ---- CONFIG ---------------------------------------------------------------
const SERVER_IP = "10.91.49.255";
const SERVER_PORT = 8765;
const WS_URL = `ws://${SERVER_IP}:${SERVER_PORT}`;
const CONNECTION_TIMEOUT_MS = 3000;

const FALLBACK_FINDINGS = [
  {
    id: "demo-1",
    severity: "CRITICAL",
    file: "auth.py",
    line: 42,
    description: "SQL injection vulnerability",
    fix: "Use parameterized queries instead of string formatting",
  },
  {
    id: "demo-2",
    severity: "MAJOR",
    file: "utils.py",
    line: 17,
    description: "Unused import 'os'",
    fix: "Remove the unused import",
  },
  {
    id: "demo-3",
    severity: "SUGGESTION",
    file: "helpers.py",
    line: 55,
    description: "Consider extracting to separate function",
    fix: "Pull the repeated block into a named helper",
  },
];

// Unpacks the backend's { commit, file, findings: [...] } wire shape into a
// flat array the UI renders one card per finding for. Each finding keeps
// the server-assigned `id` — this is the identity used everywhere below,
// never a locally-derived index.
function flattenPayload(payload) {
  if (!payload || !Array.isArray(payload.findings)) return [];
  return payload.findings.map((f) => ({
    id: f.id,
    severity: f.severity,
    file: payload.file,
    line: f.line,
    description: f.description,
    fix: f.fix,
  }));
}

const SEVERITY_STYLES = {
  CRITICAL: { color: "#EF4444", bg: "#FEF2F2", icon: "\u{1F534}", label: "CRITICAL" },
  MAJOR: { color: "#EA580C", bg: "#FFF7ED", icon: "\u{1F7E0}", label: "MAJOR" },
  MINOR: { color: "#EAB308", bg: "#FFFBEB", icon: "\u{1F7E1}", label: "MINOR" },
  SUGGESTION: { color: "#16A34A", bg: "#F0FDF4", icon: "\u{1F7E2}", label: "SUGGESTION" },
};

// Derives what to actually show for a finding's status, folding the
// developer's decision together with any verdict that's come back for it.
// pending_verdict is a UI-only state — it's never sent over the wire, it
// just means "marked FP, waiting on the model's re-check."
function getDisplayStatus(decision, verdict) {
  if (decision === "approved") return "approved";
  if (decision === "false_positive") {
    if (!verdict) return "pending_verdict";
    return verdict.verdict === "needs_review" ? "needs_review" : "false_positive";
  }
  return "pending";
}

const STATUS_BADGE_STYLES = {
  approved: { bg: "rgba(22,163,74,0.12)", color: "#16A34A", label: "\u2713 Approved" },
  false_positive: { bg: "rgba(100,116,139,0.15)", color: "#64748B", label: "False Positive" },
  needs_review: { bg: "rgba(147,51,234,0.12)", color: "#9333EA", label: "Needs Review" },
  pending_verdict: { bg: "rgba(217,119,6,0.12)", color: "#EAB308", label: "Awaiting Model Review" },
  pending: null,
};

export default function App() {
  const [findings, setFindings] = useState([]);
  const [commit, setCommit] = useState(null); // { id, short_id, author, message, timestamp } | null
  const [status, setStatus] = useState("connecting");
  const [expandedIds, setExpandedIds] = useState({});
  const [decisions, setDecisions] = useState({}); // id -> "approved" | "false_positive"
  const [reasons, setReasons] = useState({}); // id -> reason text
  const [verdicts, setVerdicts] = useState({}); // id -> { verdict: "accepted" | "needs_review", note }
  const [attemptCount, setAttemptCount] = useState(0);
  const [demoActive, setDemoActive] = useState(false);

  // False-positive reason modal state
  const [fpModalTarget, setFpModalTarget] = useState(null); // { id, finding } | null
  const [fpReasonDraft, setFpReasonDraft] = useState("");

  // Report generation state
  const [reportRequested, setReportRequested] = useState(false);
  const [verifyingFalsePositives, setVerifyingFalsePositives] = useState(false);
  const [reportStatus, setReportStatus] = useState(null); // { type: "error", message } | null — "ready" now goes through Alert instead

  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  // reset demo data when connected
  const demoActiveRef = useRef(false);
  // Mirrors `commit` in a ref so the onmessage closure (set up once in the
  // effect) always sees the latest known commit without re-subscribing.
  const commitRef = useRef(null);

  const showDemoData = useCallback(() => {
    setDemoActive(true);
    demoActiveRef.current = true;
    setCommit(null);
    setDecisions({});
    setReasons({});
    setVerdicts({});
    setVerifyingFalsePositives(false);
    setReportRequested(false);
    setReportStatus(null);
    setFindings(FALLBACK_FINDINGS);
  }, []);

  const hideDemoData = useCallback(() => {
    setDemoActive(false);
    demoActiveRef.current = false;
    setFindings([]);
  }, []);

  // Resets all per-review-run state. Called whenever a broadcast arrives
  // for a commit.short_id we haven't seen yet, so a new review run never
  // inherits stale findings/decisions from the previous one.
  const resetForNewCommit = useCallback((newCommit, initialFindings) => {
    commitRef.current = newCommit || null;
    setCommit(newCommit || null);
    setFindings(initialFindings);
    setDecisions({});
    setReasons({});
    setVerdicts({});
    setVerifyingFalsePositives(false);
    setReportRequested(false);
    setReportStatus(null);
    setDemoActive(false);
  }, []);

  // Full reset once a report has actually been generated — same shape as
  // resetForNewCommit but with no incoming commit to seed, since we're
  // going back to an empty "waiting for the next review" slate.
  const resetAfterReport = useCallback(() => {
    commitRef.current = null;
    setCommit(null);
    setFindings([]);
    setDecisions({});
    setReasons({});
    setVerdicts({});
    setVerifyingFalsePositives(false);
    setReportRequested(false);
    setReportStatus(null);
    setExpandedIds({});
  }, []);

  // ---- WebSocket send helpers ----------------------------------------------
  const sendMessage = useCallback((message) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(message));
    } else {
      console.log("[DevMesh] Socket not open — message not sent:", message);
    }
  }, []);

  const sendDecision = useCallback(
    (id, decisionValue, comment) => {
      sendMessage({
        type: "finding_decision",
        finding_id: id,
        decision: decisionValue,
        ...(decisionValue === "false_positive" ? { comment } : {}),
      });
    },
    [sendMessage]
  );

  useEffect(() => {
    let mounted = true;
    let reconnectAttempts = 0;
    const MAX_RETRIES = 20;

    const connect = () => {
      if (!mounted) return;

      reconnectAttempts += 1;
      if (mounted) setAttemptCount(reconnectAttempts);

      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      const timeout = setTimeout(() => {
        if (ws.readyState !== WebSocket.OPEN && mounted) {
          setStatus("waiting");
          ws.close();
        }
      }, CONNECTION_TIMEOUT_MS);

      ws.onopen = () => {
        clearTimeout(timeout);
        reconnectAttempts = 0;
        if (mounted) {
          setStatus("live");
          setAttemptCount(0);
          setDemoActive(false);
          setFindings([]);
          setDecisions({});
          setReasons({});
          setVerdicts({});
          setVerifyingFalsePositives(false);
          setReportRequested(false);
          setReportStatus(null);
          setExpandedIds({});
        }
      };

      ws.onmessage = (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch (e) {
          console.warn("[DevMesh] Parse error:", e);
          return;
        }
        if (!mounted) return;

        // Control replies (not a findings broadcast).
        if (payload.type === "verdict") {
          setVerdicts((prev) => ({
            ...prev,
            [payload.finding_id]: { verdict: payload.verdict, note: payload.note },
          }));
          return;
        }
        if (payload.type === "report_ready") {
          Alert.alert(
            "Report Generated",
            payload.path ? `Saved to ${payload.path}` : "The review report is ready.",
            [{ text: "OK", onPress: resetAfterReport }],
            { cancelable: false }
          );
          return;
        }
        if (payload.type === "report_error") {
          setReportRequested(false);
          setVerifyingFalsePositives(false);
          setReportStatus({ type: "error", message: payload.message });
          return;
        }
        if (payload.type === "decision_error") {
          setReportStatus({ type: "error", message: payload.message });
          return;
        }

        // Findings broadcast.
        const newFindings = flattenPayload(payload);
        if (newFindings.length === 0) return;

        const incomingShortId = payload.commit?.short_id ?? null;
        const currentShortId = commitRef.current?.short_id ?? null;

        if (incomingShortId !== currentShortId) {
          // New review run — replace, don't append.
          resetForNewCommit(payload.commit, newFindings);
        } else {
          setFindings((prev) => [...prev, ...newFindings]);
        }
      };

      ws.onerror = (e) => {
        console.log("[DevMesh] ERROR:", e.message || e);
      };

      ws.onclose = () => {
        clearTimeout(timeout);
        if (!mounted) return;
        if (mounted) setStatus("waiting");
        if (reconnectAttempts < MAX_RETRIES) {
          const delay = Math.min(reconnectAttempts * 1000, 5000);
          reconnectTimerRef.current = setTimeout(() => connect(), delay);
        }
      };
    };

    connect();

    return () => {
      mounted = false;
      clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [resetForNewCommit, resetAfterReport]);

  const toggleExpand = (id) => {
    setExpandedIds((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  // Approve is a direct decision — no reason required.
  const approve = (id) => {
    setDecisions((prev) => ({ ...prev, [id]: "approved" }));
    sendDecision(id, "approved");
  };

  // False Positive opens the reason modal instead of deciding immediately.
  const openFalsePositiveModal = (id, finding) => {
    setFpReasonDraft(reasons[id] || "");
    setFpModalTarget({ id, finding });
  };

  const cancelFalsePositiveModal = () => {
    setFpModalTarget(null);
    setFpReasonDraft("");
  };

  const confirmFalsePositive = () => {
    if (!fpModalTarget) return;
    const trimmed = fpReasonDraft.trim();
    if (trimmed.length === 0) return; // reason is required — button also disabled below

    const { id } = fpModalTarget;
    setDecisions((prev) => ({ ...prev, [id]: "false_positive" }));
    setReasons((prev) => ({ ...prev, [id]: trimmed }));
    // Marking FP again after a prior verdict (e.g. new reasoning) clears
    // the old verdict so the finding goes back to "awaiting model review"
    // rather than keeping a stale accepted/needs_review from before.
    setVerdicts((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    sendDecision(id, "false_positive", trimmed);

    setFpModalTarget(null);
    setFpReasonDraft("");
  };

  // ---- Generate Report gating & flow ---------------------------------------
  const allResolved =
    findings.length > 0 &&
    findings.every((f) => decisions[f.id] === "approved" || decisions[f.id] === "false_positive");

  const fpAwaitingVerdictIds = findings
    .filter((f) => decisions[f.id] === "false_positive" && !verdicts[f.id])
    .map((f) => f.id);

  // Once verifying, auto-fire generate_report the moment every outstanding
  // FP has a verdict — driven off state, not off counting individual
  // "verdict" messages, so it's safe regardless of arrival order.
  useEffect(() => {
    if (verifyingFalsePositives && fpAwaitingVerdictIds.length === 0) {
      setVerifyingFalsePositives(false);
      sendMessage({ type: "generate_report" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [verifyingFalsePositives, fpAwaitingVerdictIds.length, sendMessage]);

  const handleGenerateReport = () => {
    if (!allResolved || reportRequested || verifyingFalsePositives || demoActive) return;

    setReportRequested(true);
    setReportStatus(null);

    if (fpAwaitingVerdictIds.length > 0) {
      setVerifyingFalsePositives(true);
      sendMessage({ type: "request_verdicts", finding_id: fpAwaitingVerdictIds });
    } else {
      sendMessage({ type: "generate_report" });
    }
  };

  const cancelVerifying = () => {
    setVerifyingFalsePositives(false);
    setReportRequested(false);
  };

  const renderItem = ({ item }) => {
    const id = item.id;
    const sev = SEVERITY_STYLES[item.severity] || SEVERITY_STYLES.SUGGESTION;
    const expanded = !!expandedIds[id];
    const decision = decisions[id];
    const reason = reasons[id];
    const verdict = verdicts[id];
    const displayStatus = getDisplayStatus(decision, verdict);
    const badge = STATUS_BADGE_STYLES[displayStatus];

    return (
      <View style={[styles.card, { backgroundColor: sev.bg, borderLeftColor: sev.color }]}>
        <View style={styles.cardHeader}>
          <Text style={[styles.severityBadge, { color: sev.color }]}>
            {sev.icon} {sev.label}
          </Text>
          <Text style={styles.location}>
            {item.file}:{item.line}
          </Text>
        </View>

        <Text style={styles.issue}>{item.description}</Text>

        {expanded && (
          <View style={styles.fixBox}>
            <Text style={styles.fixLabel}>Recommended fix</Text>
            <Text style={styles.fixText}>{item.fix}</Text>
          </View>
        )}

        {decision === "false_positive" && reason && (
          <View style={styles.reasonBox}>
            <Text style={styles.reasonLabel}>Your reason</Text>
            <Text style={styles.reasonText}>{reason}</Text>
            {verdict && (
              <>
                <Text style={[styles.reasonLabel, { marginTop: 8 }]}>Model</Text>
                <Text style={styles.reasonText}>{verdict.note}</Text>
              </>
            )}
          </View>
        )}

        <View style={styles.actionRow}>
          {badge && (
            <View style={[styles.statusChip, { backgroundColor: badge.bg }]}>
              <Text style={[styles.statusChipText, { color: badge.color }]}>{badge.label}</Text>
            </View>
          )}

          <View style={styles.actionButtons}>
            <TouchableOpacity onPress={() => toggleExpand(id)} style={styles.actionButtonGhost}>
              <Text style={styles.actionGhostText}>{expanded ? "Hide fix" : "Show fix"}</Text>
            </TouchableOpacity>

            <TouchableOpacity
              onPress={() => openFalsePositiveModal(id, item)}
              style={[
                styles.actionButtonGhost,
                decision === "false_positive" && styles.actionFalsePositiveActive,
              ]}
            >
              <Text style={styles.actionGhostText}>
                {decision === "false_positive" ? "Re-explain" : "Mark as false positive"}
              </Text>
            </TouchableOpacity>

            <TouchableOpacity
              onPress={() => approve(id)}
              style={[styles.actionButtonSolid, decision === "approved" && styles.actionApprovedActive]}
            >
              <Text style={styles.actionSolidText}>
                {decision === "approved" ? "\u2713 Approved" : "Approve"}
              </Text>
            </TouchableOpacity>
          </View>
        </View>
      </View>
    );
  };

  const statusLabel = {
    connecting: "Connecting to review server...",
    live: "Live \u2014 streaming from AI PC",
    waiting: `Waiting for AI PC${attemptCount > 1 ? ` (attempt ${attemptCount})` : ""}`,
  }[status];

  const statusColor = status === "live" ? "#16A34A" : status === "waiting" ? "#64748B" : "#94A3B8";
  const showDemoBanner = demoActive && status !== "live";

  const resolvedCount = findings.filter(
    (f) => decisions[f.id] === "approved" || decisions[f.id] === "false_positive"
  ).length;

  let generateButtonLabel = "Generate Report";
  if (verifyingFalsePositives) {
    generateButtonLabel = "Verifying false positives\u2026";
  } else if (reportRequested) {
    generateButtonLabel = "Generating\u2026";
  }

  return (
    <SafeAreaView style={styles.safeArea}>
      <StatusBar barStyle="light-content" backgroundColor="#0F172A" />
      <View style={styles.header}>
        <Text style={styles.headerTitle}>DevMesh</Text>
        <Text style={styles.headerSubtitle}>On-device code review, in your pocket</Text>
        <View style={styles.statusRow}>
          <View style={[styles.statusDot, { backgroundColor: statusColor }]} />
          <Text style={styles.statusText}>{statusLabel}</Text>
        </View>
        {commit && (
          <View style={styles.commitBlock}>
            <Text style={styles.commitLine} numberOfLines={1}>
              <Text style={styles.commitLabel}>Commit: </Text>
              {commit.short_id}
            </Text>
            <Text style={styles.commitLine} numberOfLines={1}>
              <Text style={styles.commitLabel}>Message: </Text>
              {commit.message}
            </Text>
          </View>
        )}
        {showDemoBanner && (
          <View style={styles.demoBanner}>
            <Text style={styles.demoBannerText}>SAMPLE DATA — not from a live review</Text>
            <TouchableOpacity onPress={hideDemoData}>
              <Text style={styles.demoBannerAction}>Hide</Text>
            </TouchableOpacity>
          </View>
        )}
        {reportStatus && reportStatus.type === "error" && (
          <View style={[styles.reportStatusBanner, styles.reportStatusError]}>
            <Text style={styles.reportStatusText}>{reportStatus.message}</Text>
          </View>
        )}
      </View>

      <FlatList
        data={findings}
        keyExtractor={(item) => item.id}
        renderItem={renderItem}
        contentContainerStyle={styles.listContent}
        ListEmptyComponent={
          <View style={styles.emptyState}>
            <Text style={styles.emptyText}>
              {status === "live" ? "Waiting for findings..." : "Not connected to the AI PC yet."}
            </Text>
            {status !== "live" && (
              <TouchableOpacity onPress={showDemoData} style={styles.demoButton}>
                <Text style={styles.demoButtonText}>Show Demo Data</Text>
              </TouchableOpacity>
            )}
          </View>
        }
      />

      {findings.length > 0 && (
        <View style={styles.reportBar}>
          <View style={styles.reportBarLeft}>
            <Text style={styles.reportProgress}>
              {verifyingFalsePositives
                ? `Verifying ${fpAwaitingVerdictIds.length} false positive${fpAwaitingVerdictIds.length === 1 ? "" : "s"
                }\u2026`
                : `${resolvedCount} / ${findings.length} resolved`}
            </Text>
            {verifyingFalsePositives && (
              <TouchableOpacity onPress={cancelVerifying}>
                <Text style={styles.cancelVerifyingText}>Cancel</Text>
              </TouchableOpacity>
            )}
          </View>
          <TouchableOpacity
            onPress={handleGenerateReport}
            disabled={!allResolved || reportRequested || verifyingFalsePositives || demoActive}
            style={[
              styles.generateButton,
              (!allResolved || reportRequested || verifyingFalsePositives || demoActive) &&
              styles.generateButtonDisabled,
            ]}
          >
            <Text style={styles.generateButtonText}>
              {demoActive ? "Connect to AI PC to generate" : generateButtonLabel}
            </Text>
          </TouchableOpacity>
        </View>
      )}

      {/* False Positive reason modal */}
      <Modal
        visible={!!fpModalTarget}
        transparent
        animationType="fade"
        onRequestClose={cancelFalsePositiveModal}
      >
        <KeyboardAvoidingView
          behavior={Platform.OS === "ios" ? "padding" : undefined}
          style={styles.modalBackdrop}
        >
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>Why is this a false positive?</Text>
            {fpModalTarget && (
              <Text style={styles.modalSubtitle}>
                {fpModalTarget.finding.file}:{fpModalTarget.finding.line}
              </Text>
            )}
            <TextInput
              style={styles.modalInput}
              placeholder="e.g. This is behind an internal-only VPN, low risk."
              placeholderTextColor="#94A3B8"
              value={fpReasonDraft}
              onChangeText={setFpReasonDraft}
              multiline
              autoFocus
            />
            <View style={styles.modalActions}>
              <TouchableOpacity onPress={cancelFalsePositiveModal} style={styles.modalCancelButton}>
                <Text style={styles.modalCancelText}>Cancel</Text>
              </TouchableOpacity>
              <TouchableOpacity
                onPress={confirmFalsePositive}
                disabled={fpReasonDraft.trim().length === 0}
                style={[
                  styles.modalConfirmButton,
                  fpReasonDraft.trim().length === 0 && styles.modalConfirmDisabled,
                ]}
              >
                <Text style={styles.modalConfirmText}>Submit</Text>
              </TouchableOpacity>
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: "#0F172A",
  },
  header: {
    paddingHorizontal: 20,
    paddingTop: Platform.OS === "android" ? 20 : 8,
    paddingBottom: 16,
    backgroundColor: "#0F172A",
  },
  headerTitle: {
    color: "#FFFFFF",
    fontSize: 26,
    fontWeight: "700",
  },
  headerSubtitle: {
    color: "#94A3B8",
    fontSize: 13,
    marginTop: 2,
  },
  statusRow: {
    flexDirection: "row",
    alignItems: "center",
    marginTop: 10,
  },
  statusDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    marginRight: 8,
  },
  statusText: {
    color: "#CBD5E1",
    fontSize: 12,
  },
  commitBlock: {
    marginTop: 6,
  },
  commitLine: {
    color: "#64748B",
    fontSize: 11,
    fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
  },
  commitLabel: {
    color: "#94A3B8",
    fontWeight: "700",
  },
  listContent: {
    padding: 16,
    paddingBottom: 40,
    backgroundColor: "#F1F5F9",
    flexGrow: 1,
  },
  emptyState: {
    marginTop: 60,
    alignItems: "center",
  },
  emptyText: {
    color: "#94A3B8",
    fontSize: 14,
    marginBottom: 16,
  },
  demoButton: {
    borderWidth: 1,
    borderColor: "#CBD5E1",
    borderRadius: 8,
    paddingVertical: 8,
    paddingHorizontal: 16,
  },
  demoButtonText: {
    color: "#475569",
    fontSize: 13,
    fontWeight: "600",
  },
  demoBanner: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    backgroundColor: "#78350F",
    borderRadius: 8,
    paddingVertical: 6,
    paddingHorizontal: 10,
    marginTop: 10,
  },
  demoBannerText: {
    color: "#FDE68A",
    fontSize: 11,
    fontWeight: "700",
    letterSpacing: 0.3,
  },
  demoBannerAction: {
    color: "#FDE68A",
    fontSize: 11,
    fontWeight: "700",
    textDecorationLine: "underline",
  },
  reportStatusBanner: {
    borderRadius: 8,
    paddingVertical: 6,
    paddingHorizontal: 10,
    marginTop: 10,
  },
  reportStatusError: {
    backgroundColor: "#7F1D1D",
  },
  reportStatusText: {
    color: "#F1F5F9",
    fontSize: 11,
    fontWeight: "600",
  },
  card: {
    borderRadius: 12,
    borderLeftWidth: 4,
    padding: 14,
    marginBottom: 12,
  },
  cardHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 6,
  },
  severityBadge: {
    fontWeight: "700",
    fontSize: 12,
    letterSpacing: 0.5,
    paddingBottom: 3,
    lineHeight: 18,
  },
  location: {
    fontSize: 12,
    color: "#475569",
    fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
  },
  issue: {
    fontSize: 15,
    color: "#0F172A",
    fontWeight: "500",
    marginBottom: 8,
  },
  fixBox: {
    backgroundColor: "rgba(255,255,255,0.6)",
    borderRadius: 8,
    padding: 10,
    marginBottom: 10,
  },
  fixLabel: {
    fontSize: 11,
    fontWeight: "700",
    color: "#334155",
    marginBottom: 3,
    textTransform: "uppercase",
  },
  fixText: {
    fontSize: 13,
    color: "#1E293B",
  },
  reasonBox: {
    backgroundColor: "rgba(100,116,139,0.12)",
    borderRadius: 8,
    padding: 10,
    marginBottom: 10,
  },
  reasonLabel: {
    fontSize: 11,
    fontWeight: "700",
    color: "#475569",
    marginBottom: 3,
    textTransform: "uppercase",
  },
  reasonText: {
    fontSize: 13,
    color: "#334155",
  },
  actionRow: {
    flexDirection: "column",
    gap: 8,
  },
  statusChip: {
    alignSelf: "flex-start",
    paddingVertical: 3,
    paddingHorizontal: 8,
    borderRadius: 5,
  },
  statusChipText: {
    fontSize: 10.5,
    fontWeight: "700",
    textTransform: "uppercase",
    letterSpacing: 0.4,
  },
  actionButtons: {
    flexDirection: "row",
    justifyContent: "flex-end",
    gap: 8,
  },
  actionButtonGhost: {
    paddingVertical: 6,
    paddingHorizontal: 10,
    borderRadius: 8,
  },
  actionGhostText: {
    fontSize: 12,
    color: "#475569",
    fontWeight: "600",
  },
  actionFalsePositiveActive: {
    backgroundColor: "rgba(100,116,139,0.15)",
  },
  actionButtonSolid: {
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: 8,
    backgroundColor: "#0F172A",
  },
  actionSolidText: {
    fontSize: 12,
    color: "#FFFFFF",
    fontWeight: "700",
  },
  actionApprovedActive: {
    backgroundColor: "#16A34A",
  },
  reportBar: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingVertical: 14,
    backgroundColor: "#0F172A",
    borderTopWidth: 1,
    borderTopColor: "#1E293B",
  },
  reportBarLeft: {
    flexShrink: 1,
  },
  reportProgress: {
    color: "#94A3B8",
    fontSize: 12,
  },
  cancelVerifyingText: {
    color: "#EF4444",
    fontSize: 11,
    fontWeight: "600",
    marginTop: 4,
  },
  generateButton: {
    backgroundColor: "#5B8DEF",
    paddingVertical: 10,
    paddingHorizontal: 18,
    borderRadius: 8,
  },
  generateButtonDisabled: {
    backgroundColor: "#334155",
  },
  generateButtonText: {
    color: "#FFFFFF",
    fontSize: 13,
    fontWeight: "700",
  },
  modalBackdrop: {
    flex: 1,
    backgroundColor: "rgba(15,23,42,0.6)",
    justifyContent: "center",
    alignItems: "center",
    padding: 24,
  },
  modalCard: {
    width: "100%",
    backgroundColor: "#FFFFFF",
    borderRadius: 14,
    padding: 20,
  },
  modalTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: "#0F172A",
    marginBottom: 4,
  },
  modalSubtitle: {
    fontSize: 12,
    color: "#64748B",
    fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
    marginBottom: 14,
  },
  modalInput: {
    borderWidth: 1,
    borderColor: "#CBD5E1",
    borderRadius: 8,
    padding: 12,
    minHeight: 90,
    fontSize: 14,
    color: "#0F172A",
    textAlignVertical: "top",
    marginBottom: 16,
  },
  modalActions: {
    flexDirection: "row",
    justifyContent: "flex-end",
    gap: 10,
  },
  modalCancelButton: {
    paddingVertical: 10,
    paddingHorizontal: 16,
    borderRadius: 8,
  },
  modalCancelText: {
    color: "#64748B",
    fontSize: 13,
    fontWeight: "600",
  },
  modalConfirmButton: {
    backgroundColor: "#0F172A",
    paddingVertical: 10,
    paddingHorizontal: 16,
    borderRadius: 8,
  },
  modalConfirmDisabled: {
    backgroundColor: "#94A3B8",
  },
  modalConfirmText: {
    color: "#FFFFFF",
    fontSize: 13,
    fontWeight: "700",
  },
});