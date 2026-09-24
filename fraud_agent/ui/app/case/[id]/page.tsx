"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { use } from "react";

const API = "http://localhost:8000";

type Evidence = { claim: string; source: string; ref: string; entity_ids: string[] };
type Action = { action: string; route: string; reason: string };
type CaseDetail = {
  case_id: string;
  case: {
    status: string; verdict: string; fraud_probability: number;
    pattern: string; pattern_description: string;
    affected_txn_ids: string[]; first_suspicious_txn_id: string;
    connected_card_ids: string[]; connected_device_profiles: string[];
    exposure_usd: number; evidence: Evidence[];
    similar_prior_cases: string[]; summary: string;
    written_to_graph: boolean; graph_case_id: string;
  };
  evidence_requests: { type: string; asked_after_step: number; assumed_response: string }[];
  next_best_actions: { initial: Action[]; final: Action[]; what_changed: string };
  sar: { file: boolean; reason: string; narrative: string; subjects: string[]; total_amount_usd: number; activity_dates: string[] };
  stop_reason: string; tool_calls: number; tokens: number; latency_s: number;
};

function routeStyle(route: string) {
  if (route === "L2") return "bg-red-500/20 text-red-300 border border-red-500/40";
  if (route === "L1") return "bg-amber-500/20 text-amber-300 border border-amber-500/40";
  return "bg-zinc-700/50 text-zinc-400 border border-zinc-600/40";
}

function sourceIcon(source: string) {
  const icons: Record<string, string> = { graph: "◈", document: "◉", customer: "◎", external: "◇" };
  return icons[source] || "•";
}

function sourceColor(source: string) {
  if (source === "graph") return "text-violet-400";
  if (source === "customer") return "text-cyan-400";
  if (source === "external") return "text-amber-400";
  return "text-zinc-500";
}

export default function CaseDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [data, setData] = useState<CaseDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<"evidence" | "actions" | "sar">("evidence");

  useEffect(() => {
    fetch(`${API}/api/cases/${id}`).then((r) => r.json()).then(setData).finally(() => setLoading(false));
  }, [id]);

  if (loading)
    return (
      <div className="min-h-screen bg-[#080b14] flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 rounded-full border-2 border-violet-500 border-t-transparent animate-spin" />
          <span className="text-zinc-400 text-sm">Loading case...</span>
        </div>
      </div>
    );

  if (!data || !data.case)
    return (
      <div className="min-h-screen bg-[#080b14] flex items-center justify-center text-white">
        <div className="text-center">
          <p className="text-zinc-400 mb-3">Case not yet investigated.</p>
          <Link href="/" className="text-violet-400 hover:text-violet-300 transition-colors">← Back to Dashboard</Link>
        </div>
      </div>
    );

  const c = data.case;
  const isfraud = c.verdict === "fraud";
  const isLegit = c.verdict === "legitimate";

  const verdictGlow = isfraud ? "shadow-red-500/20" : isLegit ? "shadow-emerald-500/20" : "shadow-amber-500/20";
  const verdictBorder = isfraud ? "border-red-500/30" : isLegit ? "border-emerald-500/30" : "border-amber-500/30";
  const verdictText = isfraud ? "text-red-400" : isLegit ? "text-emerald-400" : "text-amber-400";
  const verdictBg = isfraud ? "bg-red-500/10" : isLegit ? "bg-emerald-500/10" : "bg-amber-500/10";

  return (
    <div className="min-h-screen bg-[#080b14] text-white">
      <div className="fixed top-0 left-1/2 -translate-x-1/2 w-[600px] h-[200px] bg-violet-600/8 blur-[100px] pointer-events-none" />

      {/* Header */}
      <header className="relative border-b border-white/5 bg-[#0d1117]/80 backdrop-blur-xl px-6 py-4 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto flex items-center gap-3 flex-wrap">
          <Link href="/" className="text-zinc-500 hover:text-zinc-300 text-sm transition-colors flex items-center gap-1">
            ← Cases
          </Link>
          <span className="text-zinc-700">/</span>
          <span className="font-mono text-violet-400 font-bold text-sm">{data.case_id}</span>
          <span className={`px-2 py-0.5 rounded-md text-xs font-bold border ${verdictBg} ${verdictBorder} ${verdictText}`}>
            {c.verdict.toUpperCase()}
          </span>
          {c.written_to_graph && (
            <span className="px-2 py-0.5 rounded-md text-xs bg-violet-500/15 text-violet-300 border border-violet-500/30">
              ◈ In TigerGraph
            </span>
          )}
          <div className="ml-auto flex items-center gap-3 text-xs text-zinc-500">
            <span>{data.tool_calls} graph calls</span>
            <span>·</span>
            <span>{Number(data.tokens).toLocaleString()} tokens</span>
            <span>·</span>
            <span>{data.latency_s}s</span>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 md:px-6 py-6 space-y-5">

        {/* Hero card */}
        <div className={`relative border rounded-2xl p-5 overflow-hidden shadow-xl ${verdictBorder} ${verdictBg} ${verdictGlow}`}>
          <div className={`absolute top-0 right-0 w-64 h-64 bg-gradient-to-bl ${isfraud ? "from-red-500/10" : isLegit ? "from-emerald-500/10" : "from-amber-500/10"} blur-3xl pointer-events-none`} />
          <div className="relative flex items-start justify-between gap-4">
            <div className="flex-1">
              <div className="flex flex-wrap items-center gap-3 mb-2">
                <span className={`text-3xl font-black ${verdictText}`}>
                  {(Number(c.fraud_probability) * 100).toFixed(0)}%
                </span>
                <span className="text-zinc-400 text-sm font-medium">fraud probability</span>
                {c.pattern && c.pattern !== "none" && (
                  <span className="bg-white/5 border border-white/10 text-zinc-300 text-xs px-2 py-0.5 rounded-full font-mono">
                    {c.pattern.replace(/_/g, " ")}
                  </span>
                )}
              </div>
              <p className="text-zinc-300 text-sm leading-relaxed max-w-xl">{c.summary}</p>
            </div>
            <div className="text-right shrink-0">
              <div className="text-pink-400 text-2xl font-black">${Number(c.exposure_usd).toFixed(2)}</div>
              <div className="text-zinc-500 text-xs">exposure</div>
              <div className="mt-2 text-zinc-500 text-xs font-mono">{c.status.replace(/_/g, " ")}</div>
            </div>
          </div>
          {c.pattern_description && (
            <div className="mt-3 bg-amber-500/10 border border-amber-500/20 rounded-xl p-3 text-amber-200 text-sm">
              <span className="font-semibold">⚡ Undocumented pattern: </span>{c.pattern_description}
            </div>
          )}
        </div>

        {/* Tab nav */}
        <div className="flex gap-1 bg-[#0d1117] border border-white/5 rounded-xl p-1 w-fit">
          {(["evidence", "actions", "sar"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer ${
                activeTab === tab
                  ? "bg-gradient-to-r from-violet-600 to-fuchsia-600 text-white shadow-lg shadow-violet-500/20"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {tab === "evidence" ? `◈ Evidence (${c.evidence.length})` :
               tab === "actions" ? "⚡ Actions" :
               `${data.sar.file ? "⚠ SAR" : "SAR"}`}
            </button>
          ))}
        </div>

        {/* Evidence tab */}
        {activeTab === "evidence" && (
          <div className="space-y-3">
            {c.evidence.map((ev, i) => (
              <div key={i} className="bg-[#0d1117] border border-white/5 rounded-xl p-4 hover:border-white/10 transition-all">
                <div className="flex items-start gap-3">
                  <span className={`text-lg mt-0.5 ${sourceColor(ev.source)}`}>{sourceIcon(ev.source)}</span>
                  <div className="flex-1 min-w-0">
                    <p className="text-zinc-200 text-sm leading-snug">{ev.claim}</p>
                    <div className="flex flex-wrap items-center gap-2 mt-2">
                      <span className={`text-xs font-medium ${sourceColor(ev.source)}`}>{ev.source}</span>
                      <span className="text-zinc-700 text-xs font-mono truncate max-w-[200px]">{ev.ref}</span>
                      {ev.entity_ids.slice(0, 4).map((eid) => (
                        <span key={eid} className="bg-zinc-800 text-zinc-400 text-xs px-2 py-0.5 rounded-md font-mono border border-zinc-700/50">
                          {eid}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Actions tab */}
        {activeTab === "actions" && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Initial */}
            <div className="bg-[#0d1117] border border-white/5 rounded-xl p-5">
              <h3 className="text-zinc-400 text-xs font-semibold uppercase tracking-widest mb-4">Initial Actions</h3>
              <div className="space-y-2">
                {data.next_best_actions.initial.map((a, i) => (
                  <div key={i} className="bg-zinc-800/40 border border-white/5 rounded-xl p-3">
                    <div className="flex items-center gap-2 mb-1">
                      <code className="text-cyan-300 text-xs font-bold">{a.action}</code>
                      <span className={`text-xs px-1.5 py-0.5 rounded-md font-mono ${routeStyle(a.route)}`}>{a.route}</span>
                    </div>
                    <p className="text-zinc-500 text-xs">{a.reason}</p>
                  </div>
                ))}
              </div>
            </div>

            <div className="space-y-4">
              {/* Evidence request */}
              {data.evidence_requests.length > 0 && (
                <div className="bg-[#0d1117] border border-cyan-500/20 rounded-xl p-4">
                  <h3 className="text-cyan-400 text-xs font-semibold uppercase tracking-widest mb-3">Evidence Requested</h3>
                  {data.evidence_requests.map((er, i) => (
                    <div key={i} className="bg-cyan-500/5 border border-cyan-500/20 rounded-xl p-3 mb-2">
                      <div className="text-cyan-300 text-xs font-semibold mb-1">{er.type.replace(/_/g, " ")}</div>
                      <p className="text-zinc-300 text-sm italic">&ldquo;{er.assumed_response}&rdquo;</p>
                    </div>
                  ))}
                </div>
              )}

              {/* Final */}
              <div className="bg-[#0d1117] border border-white/5 rounded-xl p-5">
                <h3 className="text-zinc-400 text-xs font-semibold uppercase tracking-widest mb-3">Final Actions</h3>
                {data.next_best_actions.what_changed !== "nothing" && (
                  <div className="mb-3 bg-fuchsia-500/10 border border-fuchsia-500/20 rounded-xl p-3 text-fuchsia-300 text-xs">
                    <span className="font-semibold">Changed: </span>{data.next_best_actions.what_changed}
                  </div>
                )}
                <div className="space-y-2">
                  {data.next_best_actions.final.map((a, i) => (
                    <div key={i} className="bg-zinc-800/40 border border-white/5 rounded-xl p-3">
                      <div className="flex items-center gap-2 mb-1">
                        <code className="text-emerald-300 text-xs font-bold">{a.action}</code>
                        <span className={`text-xs px-1.5 py-0.5 rounded-md font-mono ${routeStyle(a.route)}`}>{a.route}</span>
                      </div>
                      <p className="text-zinc-500 text-xs">{a.reason}</p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* SAR tab */}
        {activeTab === "sar" && (
          data.sar.file ? (
            <div className="bg-[#0d1117] border border-red-500/20 rounded-2xl p-5 shadow-xl shadow-red-500/5">
              <div className="flex items-center gap-3 mb-4">
                <span className="text-red-400 text-lg">⚠</span>
                <h3 className="font-bold text-red-300">Suspicious Activity Report</h3>
                <span className="ml-auto bg-red-500/15 text-red-300 text-xs px-2 py-0.5 rounded-lg border border-red-500/30">
                  L2 · File Required
                </span>
              </div>
              <div className="grid grid-cols-3 gap-4 mb-5">
                <div className="bg-zinc-800/50 rounded-xl p-3">
                  <div className="text-zinc-500 text-xs mb-1">Total Amount</div>
                  <div className="text-pink-400 font-bold text-lg">${Number(data.sar.total_amount_usd).toFixed(2)}</div>
                </div>
                <div className="bg-zinc-800/50 rounded-xl p-3">
                  <div className="text-zinc-500 text-xs mb-1">Activity Period</div>
                  <div className="text-zinc-300 text-sm">{data.sar.activity_dates.join(" → ")}</div>
                </div>
                <div className="bg-zinc-800/50 rounded-xl p-3">
                  <div className="text-zinc-500 text-xs mb-1">Subjects</div>
                  <div className="text-zinc-300 font-mono text-xs">{data.sar.subjects.slice(0, 2).join(", ")}</div>
                </div>
              </div>
              <div className="bg-red-500/5 border border-red-500/10 rounded-xl p-4">
                <p className="text-zinc-200 text-sm leading-relaxed">{data.sar.narrative}</p>
              </div>
              <p className="text-zinc-600 text-xs mt-3">{data.sar.reason}</p>
            </div>
          ) : (
            <div className="bg-[#0d1117] border border-white/5 rounded-2xl p-8 text-center">
              <div className="text-4xl mb-3">✓</div>
              <p className="text-emerald-400 font-semibold mb-1">No SAR Required</p>
              <p className="text-zinc-500 text-sm">{data.sar.reason}</p>
            </div>
          )
        )}

        {/* Bottom grid */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="bg-[#0d1117] border border-white/5 rounded-xl p-4">
            <h3 className="text-zinc-500 text-xs font-semibold uppercase tracking-widest mb-3">Affected Transactions</h3>
            {c.affected_txn_ids.length === 0 ? (
              <p className="text-zinc-600 text-sm">None</p>
            ) : c.affected_txn_ids.map((tid) => (
              <div key={tid} className={`font-mono text-xs px-2 py-1.5 rounded-lg mb-1 ${
                tid === c.first_suspicious_txn_id
                  ? "bg-red-500/15 text-red-300 border border-red-500/20"
                  : "bg-zinc-800/50 text-zinc-400"
              }`}>
                {tid === c.first_suspicious_txn_id && "★ "}{tid}
              </div>
            ))}
          </div>

          <div className="bg-[#0d1117] border border-white/5 rounded-xl p-4">
            <h3 className="text-zinc-500 text-xs font-semibold uppercase tracking-widest mb-3">Connected Cards</h3>
            {c.connected_card_ids.length === 0 ? (
              <p className="text-zinc-600 text-sm">None</p>
            ) : c.connected_card_ids.map((cid) => (
              <div key={cid} className="bg-orange-500/10 text-orange-300 font-mono text-xs px-2 py-1.5 rounded-lg mb-1 border border-orange-500/20">
                {cid}
              </div>
            ))}
          </div>

          <div className="bg-[#0d1117] border border-white/5 rounded-xl p-4">
            <h3 className="text-zinc-500 text-xs font-semibold uppercase tracking-widest mb-3">GraphRAG Memory</h3>
            {c.similar_prior_cases.length === 0 ? (
              <p className="text-zinc-600 text-sm">No prior cases</p>
            ) : c.similar_prior_cases.map((cid) => (
              <div key={cid} className="bg-violet-500/10 text-violet-300 font-mono text-xs px-2 py-1.5 rounded-lg mb-1 border border-violet-500/20">
                {cid}
              </div>
            ))}
          </div>
        </div>

        {/* Stop reason */}
        <div className="bg-[#0d1117] border border-white/5 rounded-xl p-4">
          <h3 className="text-zinc-500 text-xs font-semibold uppercase tracking-widest mb-2">Stop Reason</h3>
          <p className="text-zinc-300 text-sm">{data.stop_reason}</p>
        </div>
      </main>
    </div>
  );
}
