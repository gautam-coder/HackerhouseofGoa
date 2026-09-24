"use client";

import { useEffect, useState, useRef } from "react";
import Link from "next/link";

const API = "http://localhost:8000";

type CaseRow = {
  case_id: string;
  opened_at: string;
  trigger_type: string;
  flagged_txn_id: string;
  card_id: string;
  customer_id: string;
  risk_score: number | null;
  investigated: boolean;
  verdict: string | null;
  pattern: string | null;
  fraud_probability: number | null;
  exposure_usd: number | null;
  status: string;
  investigation_status: string;
};

type Stats = {
  total: number;
  investigated: number;
  fraud: number;
  legitimate: number;
  uncertain: number;
  total_exposure_usd: number;
  patterns: Record<string, number>;
};

type LogEntry = {
  type: string;
  msg?: string;
  verdict?: string;
  prob?: number;
  pattern?: string;
  exposure?: number;
};

function verdictStyle(verdict: string | null) {
  if (verdict === "fraud") return "bg-red-500/20 text-red-300 border border-red-500/40 shadow-red-500/20 shadow-sm";
  if (verdict === "legitimate") return "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 shadow-emerald-500/20 shadow-sm";
  if (verdict === "uncertain") return "bg-amber-500/20 text-amber-300 border border-amber-500/40";
  return "bg-zinc-800 text-zinc-400 border border-zinc-700";
}

function triggerStyle(type: string) {
  if (type === "customer_report") return "bg-sky-500/20 text-sky-300 border border-sky-500/30";
  if (type === "analyst_request") return "bg-violet-500/20 text-violet-300 border border-violet-500/30";
  return "bg-orange-500/20 text-orange-300 border border-orange-500/30";
}

function patternLabel(pattern: string | null) {
  const map: Record<string, string> = {
    card_testing: "Card Testing",
    card_not_present_fraud: "CNP Fraud",
    card_not_present_new_device: "CNP New Device",
    out_of_region_use: "Out-of-Region",
    account_takeover: "Acct Takeover",
    undocumented: "⚡ Undocumented",
    none: "—",
  };
  return pattern ? (map[pattern] || pattern) : "—";
}

const STAT_CARDS = [
  { key: "total", label: "Total Cases", gradient: "from-violet-500 to-purple-600" },
  { key: "investigated", label: "Investigated", gradient: "from-cyan-500 to-sky-600" },
  { key: "fraud", label: "Fraud", gradient: "from-red-500 to-rose-600" },
  { key: "legitimate", label: "Legitimate", gradient: "from-emerald-500 to-green-600" },
  { key: "uncertain", label: "Uncertain", gradient: "from-amber-400 to-orange-500" },
  { key: "exposure", label: "Exposure", gradient: "from-pink-500 to-fuchsia-600" },
];

export default function Dashboard() {
  const [cases, setCases] = useState<CaseRow[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [triggering, setTriggering] = useState<string | null>(null);

  // Live log panel state
  const [liveCase, setLiveCase] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [liveResult, setLiveResult] = useState<LogEntry | null>(null);
  const [investigationDone, setInvestigationDone] = useState(false);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);

  async function fetchAll() {
    try {
      const [casesRes, statsRes] = await Promise.all([
        fetch(`${API}/api/cases`),
        fetch(`${API}/api/stats`),
      ]);
      setCases(await casesRes.json());
      setStats(await statsRes.json());
      setError("");
    } catch {
      setError("API offline — start with: cd fraud_agent && uvicorn api:app --port 8000");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 4000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  function closePanel() {
    esRef.current?.close();
    esRef.current = null;
    setLiveCase(null);
    setLogs([]);
    setLiveResult(null);
    setInvestigationDone(false);
    fetchAll();
  }

  async function triggerInvestigation(caseId: string) {
    setTriggering(caseId);
    setLiveCase(caseId);
    setLogs([]);
    setLiveResult(null);
    setInvestigationDone(false);

    try {
      await fetch(`${API}/api/investigate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_id: caseId }),
      });
    } catch {
      setLogs(["ERROR: Failed to trigger investigation"]);
      setTriggering(null);
      return;
    }

    setTriggering(null);

    // Open SSE stream
    const es = new EventSource(`${API}/api/investigate/${caseId}/stream`);
    esRef.current = es;

    es.onmessage = (event) => {
      try {
        const data: LogEntry = JSON.parse(event.data);
        if (data.type === "log" && data.msg) {
          setLogs((prev) => [...prev, data.msg!]);
        } else if (data.type === "result") {
          setLiveResult(data);
        } else if (data.type === "error" && data.msg) {
          setLogs((prev) => [...prev, `ERROR: ${data.msg}`]);
        } else if (data.type === "done") {
          setInvestigationDone(true);
          es.close();
          esRef.current = null;
          setTimeout(fetchAll, 1000);
        }
      } catch {
        // ignore parse errors on heartbeat lines
      }
    };

    es.onerror = () => {
      // onerror fires after a clean done-close too — only show error if not already done
      setInvestigationDone((already) => {
        if (!already) {
          setLogs((prev) => [...prev, "ERROR: Stream disconnected unexpectedly."]);
        }
        return true;
      });
      es.close();
      esRef.current = null;
      setTimeout(fetchAll, 1000);
    };
  }

  const statValues = stats ? [
    stats.total,
    stats.investigated,
    stats.fraud,
    stats.legitimate,
    stats.uncertain,
    `$${Number(stats.total_exposure_usd).toLocaleString("en-US", { maximumFractionDigits: 0 })}`,
  ] : Array(6).fill("—");

  if (loading)
    return (
      <div className="min-h-screen bg-[#080b14] flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 rounded-full border-2 border-violet-500 border-t-transparent animate-spin" />
          <span className="text-zinc-400 text-sm">Loading investigations...</span>
        </div>
      </div>
    );

  return (
    <div className="min-h-screen bg-[#080b14] text-white">
      {/* Ambient glow top */}
      <div className="fixed top-0 left-1/2 -translate-x-1/2 w-[800px] h-[300px] bg-violet-600/10 blur-[120px] pointer-events-none" />

      {/* Header */}
      <header className="relative border-b border-white/5 bg-[#0d1117]/80 backdrop-blur-xl px-6 py-4 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-violet-500 to-fuchsia-600 flex items-center justify-center text-sm font-bold shadow-lg shadow-violet-500/30">
              TG
            </div>
            <div>
              <h1 className="text-base font-bold bg-gradient-to-r from-violet-400 via-fuchsia-400 to-cyan-400 bg-clip-text text-transparent">
                Fraud Investigation Agent
              </h1>
              <p className="text-zinc-500 text-xs">Hacker House Goa 2026 · TigerGraph × IEEE-CIS</p>
            </div>
          </div>
          <div className="hidden md:flex items-center gap-2 text-xs text-zinc-500">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse inline-block" />
            Live · 590,742 txns · 5,565 cases
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 md:px-6 py-6">
        {error && (
          <div className="mb-4 bg-red-500/10 border border-red-500/30 rounded-xl p-4 text-red-300 text-sm">
            {error}
          </div>
        )}

        {/* Stats */}
        <div className="grid grid-cols-3 md:grid-cols-6 gap-3 mb-6">
          {STAT_CARDS.map((s, i) => (
            <div
              key={s.key}
              className="relative bg-[#0d1117] border border-white/5 rounded-xl p-4 text-center overflow-hidden group hover:border-white/10 transition-all"
            >
              <div className={`absolute inset-0 bg-gradient-to-br ${s.gradient} opacity-0 group-hover:opacity-5 transition-opacity`} />
              <div className={`text-xl font-bold bg-gradient-to-br ${s.gradient} bg-clip-text text-transparent`}>
                {statValues[i]}
              </div>
              <div className="text-zinc-500 text-xs mt-1">{s.label}</div>
            </div>
          ))}
        </div>

        {/* Table */}
        <div className="bg-[#0d1117] border border-white/5 rounded-2xl overflow-hidden shadow-2xl">
          <div className="px-6 py-4 border-b border-white/5 flex items-center justify-between">
            <h2 className="font-semibold text-zinc-100 flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-violet-500" />
              20 Exam Cases
            </h2>
            <span className="text-zinc-500 text-xs bg-zinc-800/50 px-2 py-1 rounded-lg">
              {cases.filter((c) => c.investigated).length} / 20 complete
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/5 text-zinc-500 text-xs uppercase tracking-widest">
                  <th className="text-left px-5 py-3">Case</th>
                  <th className="text-left px-5 py-3">Customer</th>
                  <th className="text-left px-5 py-3">Trigger</th>
                  <th className="text-right px-5 py-3">Risk</th>
                  <th className="text-left px-5 py-3">Verdict</th>
                  <th className="text-left px-5 py-3">Pattern</th>
                  <th className="text-right px-5 py-3">Prob</th>
                  <th className="text-right px-5 py-3">Exposure</th>
                  <th className="text-center px-5 py-3">Action</th>
                </tr>
              </thead>
              <tbody>
                {cases.map((c) => (
                  <tr
                    key={c.case_id}
                    className={`border-b border-white/[0.03] hover:bg-white/[0.02] transition-all ${
                      liveCase === c.case_id ? "bg-violet-500/5" : ""
                    }`}
                  >
                    <td className="px-5 py-3">
                      <Link
                        href={`/case/${c.case_id}`}
                        className="font-mono text-violet-400 hover:text-violet-300 font-bold text-sm transition-colors"
                      >
                        {c.case_id}
                      </Link>
                      <div className="text-zinc-600 text-xs">{c.opened_at?.split(" ")[0]}</div>
                    </td>
                    <td className="px-5 py-3 font-mono text-xs">
                      <div className="text-zinc-300">{c.customer_id}</div>
                      <div className="text-zinc-600">{c.card_id}</div>
                    </td>
                    <td className="px-5 py-3">
                      <span className={`px-2 py-0.5 rounded-md text-xs font-medium ${triggerStyle(c.trigger_type)}`}>
                        {c.trigger_type === "customer_report" ? "Customer"
                          : c.trigger_type === "analyst_request" ? "Analyst"
                          : "Risk Score"}
                      </span>
                    </td>
                    <td className="px-5 py-3 text-right font-mono">
                      {c.risk_score != null ? (
                        <span className={
                          Number(c.risk_score) >= 0.8 ? "text-red-400" :
                          Number(c.risk_score) >= 0.6 ? "text-amber-400" : "text-zinc-500"
                        }>
                          {Number(c.risk_score).toFixed(2)}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-5 py-3">
                      {c.investigated ? (
                        <span className={`px-2 py-0.5 rounded-md text-xs font-bold ${verdictStyle(c.verdict)}`}>
                          {c.verdict?.toUpperCase()}
                        </span>
                      ) : (
                        <span className="text-zinc-600 text-xs">Pending</span>
                      )}
                    </td>
                    <td className="px-5 py-3 text-xs text-zinc-400">
                      {c.pattern ? patternLabel(c.pattern) : "—"}
                    </td>
                    <td className="px-5 py-3 text-right font-mono">
                      {c.fraud_probability != null ? (
                        <span className={
                          Number(c.fraud_probability) >= 0.7 ? "text-red-400 font-bold" :
                          Number(c.fraud_probability) >= 0.4 ? "text-amber-400" : "text-emerald-400"
                        }>
                          {(Number(c.fraud_probability) * 100).toFixed(0)}%
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-5 py-3 text-right font-mono">
                      {c.exposure_usd && Number(c.exposure_usd) > 0 ? (
                        <span className="text-pink-400 font-semibold">${Number(c.exposure_usd).toFixed(2)}</span>
                      ) : "—"}
                    </td>
                    <td className="px-5 py-3 text-center">
                      {liveCase === c.case_id || c.investigation_status === "running" ? (
                        <span className="text-violet-400 text-xs flex items-center justify-center gap-1">
                          <span className="w-3 h-3 rounded-full border border-violet-400 border-t-transparent animate-spin" />
                          Running
                        </span>
                      ) : c.investigated ? (
                        <div className="flex items-center justify-center gap-2">
                          <Link
                            href={`/case/${c.case_id}`}
                            className="text-cyan-400 hover:text-cyan-300 text-xs font-semibold transition-colors"
                          >
                            View →
                          </Link>
                          <button
                            onClick={() => triggerInvestigation(c.case_id)}
                            disabled={liveCase !== null}
                            className="text-zinc-500 hover:text-violet-400 text-xs transition-colors disabled:opacity-30 cursor-pointer"
                            title="Re-investigate"
                          >
                            ↺
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => triggerInvestigation(c.case_id)}
                          disabled={triggering === c.case_id || liveCase !== null}
                          className="bg-gradient-to-r from-violet-600 to-fuchsia-600 hover:from-violet-500 hover:to-fuchsia-500 disabled:opacity-40 text-white text-xs px-3 py-1 rounded-lg font-medium transition-all shadow-lg shadow-violet-500/20 cursor-pointer"
                        >
                          Investigate
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <p className="text-center text-zinc-600 text-xs mt-4">
          Powered by TigerGraph MCP · OpenAI o3 · GraphRAG memory
        </p>
      </main>

      {/* Live Investigation Log Panel */}
      {liveCase && (
        <div className="fixed inset-0 z-50 flex items-end justify-center pointer-events-none">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm pointer-events-auto"
            onClick={investigationDone ? closePanel : undefined}
          />

          {/* Panel */}
          <div className="relative w-full max-w-3xl mb-0 md:mb-6 md:rounded-2xl bg-[#0d1117] border border-violet-500/30 shadow-2xl shadow-violet-500/10 overflow-hidden pointer-events-auto"
            style={{ maxHeight: "70vh" }}>

            {/* Panel header */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-white/5 bg-[#0d1117]/90 backdrop-blur-xl">
              <div className="flex items-center gap-3">
                {!investigationDone ? (
                  <div className="w-3 h-3 rounded-full border-2 border-violet-400 border-t-transparent animate-spin" />
                ) : (
                  <div className="w-3 h-3 rounded-full bg-emerald-400" />
                )}
                <span className="text-sm font-semibold text-zinc-100">
                  {investigationDone ? "Investigation Complete" : "Investigating"}&nbsp;
                  <span className="font-mono text-violet-400">{liveCase}</span>
                </span>
              </div>
              {investigationDone && (
                <button
                  onClick={closePanel}
                  className="text-zinc-500 hover:text-zinc-200 text-lg leading-none px-2 transition-colors"
                >
                  ×
                </button>
              )}
            </div>

            {/* Result banner (shown after result event) */}
            {liveResult && (
              <div className={`px-5 py-3 border-b border-white/5 flex flex-wrap items-center gap-4 text-sm ${
                liveResult.verdict === "fraud" ? "bg-red-500/10" :
                liveResult.verdict === "legitimate" ? "bg-emerald-500/10" : "bg-amber-500/10"
              }`}>
                <span className={`px-3 py-1 rounded-lg font-bold text-xs ${verdictStyle(liveResult.verdict ?? null)}`}>
                  {liveResult.verdict?.toUpperCase()}
                </span>
                <span className="text-zinc-400 text-xs">
                  Fraud prob: <span className="text-white font-semibold">{((liveResult.prob ?? 0) * 100).toFixed(0)}%</span>
                </span>
                <span className="text-zinc-400 text-xs">
                  Pattern: <span className="text-white font-semibold">{patternLabel(liveResult.pattern ?? null)}</span>
                </span>
                <span className="text-zinc-400 text-xs">
                  Exposure: <span className="text-pink-400 font-semibold">${(liveResult.exposure ?? 0).toFixed(2)}</span>
                </span>
                {investigationDone && (
                  <Link
                    href={`/case/${liveCase}`}
                    onClick={closePanel}
                    className="ml-auto bg-gradient-to-r from-cyan-500 to-sky-600 text-white text-xs px-4 py-1.5 rounded-lg font-semibold hover:opacity-90 transition-all"
                  >
                    View Full Case →
                  </Link>
                )}
              </div>
            )}

            {/* Log output */}
            <div className="overflow-y-auto p-4 font-mono text-xs leading-relaxed" style={{ maxHeight: "calc(70vh - 120px)" }}>
              {logs.length === 0 && (
                <div className="text-zinc-600 italic">Connecting to agent stream...</div>
              )}
              {logs.map((line, i) => (
                <div
                  key={i}
                  className={`py-0.5 ${
                    line.startsWith("ERROR") ? "text-red-400" :
                    line.startsWith("✓") ? "text-emerald-400 font-semibold" :
                    line.startsWith("─") ? "text-zinc-700" :
                    line.startsWith("  ") ? "text-zinc-500" :
                    line.includes("VERDICT") ? "text-emerald-300 font-bold" :
                    line.startsWith("[") ? "text-violet-300" :
                    "text-zinc-300"
                  }`}
                >
                  {line}
                </div>
              ))}
              <div ref={logsEndRef} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
