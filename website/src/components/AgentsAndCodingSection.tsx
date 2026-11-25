import { useRef, useState, type FormEvent } from 'react';
import { animate } from 'animejs';
import { Pause, Play, Square } from 'lucide-react';
import { useAnimeScope } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function AgentsAndCodingSection() {
  const { t, lang } = useLanguage();
  const [presetIdx, setPresetIdx] = useState(0);
  const [retrievalOverride, setRetrievalOverride] = useState<boolean | null>(null);
  const [codingState, setCodingState] = useState<'running' | 'paused' | 'stopped'>('running');
  const [codingTab, setCodingTab] = useState<'plan' | 'files' | 'workspace'>('plan');
  const [followUpInput, setFollowUpInput] = useState('');
  const [submittedFollowUp, setSubmittedFollowUp] = useState<string | null>(null);

  const agentPreviewRef = useRef<HTMLDivElement | null>(null);

  const activePreset = t.agentsAndCoding.agentsPanel.presets[presetIdx] ?? t.agentsAndCoding.agentsPanel.presets[0];
  const retrievalEnabled = retrievalOverride ?? activePreset.retrievalEnabled;

  useAnimeScope(
    agentPreviewRef,
    (_scope, reduceMotion) => {
      if (reduceMotion) return;
      animate('.agent-preview-block', {
        opacity: [0.5, 1],
        translateY: [5, 0],
        duration: 240,
        ease: 'outQuad',
      });
    },
    [presetIdx, retrievalEnabled, lang],
  );

  const handlePresetSelect = (idx: number) => {
    setPresetIdx(idx);
    setRetrievalOverride(null);
  };

  const handleFollowUpSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = followUpInput.trim();
    if (!trimmed) return;
    setSubmittedFollowUp(trimmed);
    setFollowUpInput('');
  };

  const statusText =
    codingState === 'running'
      ? t.agentsAndCoding.codingPanel.controls.stateRunning
      : codingState === 'paused'
        ? t.agentsAndCoding.codingPanel.controls.statePaused
        : t.agentsAndCoding.codingPanel.controls.stateStopped;

  return (
    <section
      id="agents-coding"
      className="border-b border-white/10 bg-[#060706] py-20 lg:py-28"
    >
      <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
        <div className="max-w-2xl">
          <p className="text-xs font-medium text-[#95E329]">
            {t.agentsAndCoding.eyebrow}
          </p>
          <h2
            style={{ textWrap: 'balance' }}
            className="mt-3 font-display text-3xl font-bold tracking-tight text-white sm:text-4xl"
          >
            {t.agentsAndCoding.headline}
          </h2>
          <p className="mt-4 text-base leading-relaxed text-neutral-300">
            {t.agentsAndCoding.lead}
          </p>
        </div>

        <div className="mt-12 grid grid-cols-1 items-stretch gap-8 lg:grid-cols-12">
          {/* Panel A: Custom Agents Builder */}
          <div className="flex flex-col justify-between rounded-lg border border-white/12 bg-[#0D0F0C] p-6 lg:col-span-5">
            <div>
              <h3 className="text-xl font-semibold text-white">
                {t.agentsAndCoding.agentsPanel.title}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-neutral-300">
                {t.agentsAndCoding.agentsPanel.subtitle}
              </p>

              {/* Preset Selector */}
              <div className="mt-5">
                <p className="text-xs font-medium text-neutral-400">
                  {t.agentsAndCoding.agentsPanel.presetLabel}
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {t.agentsAndCoding.agentsPanel.presets.map((preset, idx) => {
                    const selected = idx === presetIdx;
                    return (
                      <button
                        key={preset.id}
                        type="button"
                        onClick={() => handlePresetSelect(idx)}
                        aria-pressed={selected}
                        className={`min-h-[40px] whitespace-nowrap rounded-md border px-3 py-1.5 text-xs font-medium transition-colors duration-150 ${
                          selected
                            ? 'border-[#95E329] bg-[#95E329] text-[#060706]'
                            : 'border-white/12 bg-[#141712] text-neutral-200 hover:border-white/25'
                        }`}
                      >
                        {preset.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Assembled Agent Preview */}
              <div
                ref={agentPreviewRef}
                className="agent-preview-block mt-6 space-y-3 rounded-md border border-white/10 bg-[#080A08] p-4"
              >
                <p className="font-mono text-[11px] font-medium text-[#95E329]">
                  {t.agentsAndCoding.agentsPanel.previewHeading}
                </p>

                <div>
                  <span className="text-[11px] text-neutral-400">
                    {t.agentsAndCoding.agentsPanel.fieldLabels.name}
                  </span>
                  <p className="text-sm font-semibold text-white">
                    {activePreset.name}
                  </p>
                </div>

                <div>
                  <span className="text-[11px] text-neutral-400">
                    {t.agentsAndCoding.agentsPanel.fieldLabels.role}
                  </span>
                  <p className="text-xs leading-relaxed text-neutral-200">
                    {activePreset.role}
                  </p>
                </div>

                <div>
                  <span className="text-[11px] text-neutral-400">
                    {t.agentsAndCoding.agentsPanel.fieldLabels.instructions}
                  </span>
                  <p className="mt-0.5 rounded border border-white/10 bg-[#10120F] p-2.5 font-mono text-[11px] leading-relaxed text-neutral-300">
                    {activePreset.instructions}
                  </p>
                </div>

                <div className="grid grid-cols-1 gap-2 pt-1 sm:grid-cols-2">
                  <div>
                    <span className="text-[11px] text-neutral-400">
                      {t.agentsAndCoding.agentsPanel.fieldLabels.purposeRole}
                    </span>
                    <p className="text-xs font-medium text-[#95E329]">
                      {activePreset.purposeRole}
                    </p>
                  </div>

                  <div>
                    <span className="text-[11px] text-neutral-400">
                      {t.agentsAndCoding.agentsPanel.fieldLabels.workspaceScope}
                    </span>
                    <p className="text-xs font-medium text-neutral-200">
                      {activePreset.workspaceScope}
                    </p>
                  </div>
                </div>

                <div className="flex items-center justify-between gap-3 border-t border-white/10 pt-3">
                  <span className="text-xs text-neutral-300">
                    {t.agentsAndCoding.agentsPanel.fieldLabels.retrieval}
                  </span>
                  <button
                    type="button"
                    onClick={() => setRetrievalOverride(!retrievalEnabled)}
                    aria-pressed={retrievalEnabled}
                    className={`min-h-[36px] whitespace-nowrap rounded border px-3 py-1 text-xs font-medium transition-colors duration-150 ${
                      retrievalEnabled
                        ? 'border-[#95E329]/60 bg-[#141B10] text-[#95E329]'
                        : 'border-white/15 bg-[#121411] text-neutral-300'
                    }`}
                  >
                    {retrievalEnabled
                      ? t.agentsAndCoding.agentsPanel.retrievalOn
                      : t.agentsAndCoding.agentsPanel.retrievalOff}
                  </button>
                </div>
              </div>
            </div>

            <p className="mt-4 text-[11px] text-neutral-400">
              {t.agentsAndCoding.agentsPanel.disclaimer}
            </p>
          </div>

          {/* Panel B: Coding Orchestrator Mockup */}
          <div className="flex flex-col justify-between rounded-lg border border-white/12 bg-[#0D0F0C] p-6 lg:col-span-7">
            <div>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h3 className="text-xl font-semibold text-white">
                    {t.agentsAndCoding.codingPanel.title}
                  </h3>
                  <p className="mt-2 max-w-xl text-sm leading-relaxed text-neutral-300">
                    {t.agentsAndCoding.codingPanel.subtitle}
                  </p>
                </div>
              </div>

              <p className="mt-3 text-[11px] text-neutral-400">
                {t.agentsAndCoding.codingPanel.illustrativeLabel}
              </p>

              {/* Control Bar */}
              <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-md border border-white/10 bg-[#080A08] p-3">
                <span
                  className={`text-xs font-medium ${
                    codingState === 'running'
                      ? 'text-[#95E329]'
                      : codingState === 'paused'
                        ? 'text-neutral-200'
                        : 'text-neutral-400'
                  }`}
                >
                  {statusText}
                </span>

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setCodingState('paused')}
                    disabled={codingState !== 'running'}
                    className="inline-flex min-h-[38px] items-center gap-1.5 whitespace-nowrap rounded border border-white/15 bg-[#141712] px-3 py-1.5 text-xs font-medium text-neutral-200 transition-colors hover:border-white/30 disabled:opacity-40"
                  >
                    <Pause className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{t.agentsAndCoding.codingPanel.controls.pause}</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setCodingState('running')}
                    disabled={codingState === 'running'}
                    className="inline-flex min-h-[38px] items-center gap-1.5 whitespace-nowrap rounded border border-[#95E329]/50 bg-[#141B10] px-3 py-1.5 text-xs font-medium text-[#95E329] transition-colors hover:bg-[#1b2615] disabled:opacity-40"
                  >
                    <Play className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{t.agentsAndCoding.codingPanel.controls.resume}</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setCodingState('stopped')}
                    disabled={codingState === 'stopped'}
                    className="inline-flex min-h-[38px] items-center gap-1.5 whitespace-nowrap rounded border border-white/15 bg-[#141712] px-3 py-1.5 text-xs font-medium text-neutral-200 transition-colors hover:border-white/30 disabled:opacity-40"
                  >
                    <Square className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>{t.agentsAndCoding.codingPanel.controls.stop}</span>
                  </button>
                </div>
              </div>

              {/* Workspace Inspection Tabs */}
              <div
                role="tablist"
                aria-label="Coding orchestrator views"
                className="mt-4 flex flex-wrap gap-2 border-b border-white/10 pb-3"
              >
                {(
                  [
                    ['plan', t.agentsAndCoding.codingPanel.tabs.plan],
                    ['files', t.agentsAndCoding.codingPanel.tabs.files],
                    ['workspace', t.agentsAndCoding.codingPanel.tabs.workspace],
                  ] as const
                ).map(([tabId, tabLabel]) => {
                  const active = codingTab === tabId;
                  return (
                    <button
                      key={tabId}
                      type="button"
                      role="tab"
                      aria-selected={active}
                      onClick={() => setCodingTab(tabId)}
                      className={`min-h-[40px] whitespace-nowrap rounded-md px-3.5 py-1.5 text-xs font-medium transition-colors duration-150 ${
                        active
                          ? 'bg-[#95E329] text-[#060706]'
                          : 'bg-[#141712] text-neutral-300 hover:text-white'
                      }`}
                    >
                      {tabLabel}
                    </button>
                  );
                })}
              </div>

              {/* Tab Content */}
              <div className="mt-4 min-h-[180px]">
                {codingTab === 'plan' && (
                  <div className="space-y-2.5">
                    {t.agentsAndCoding.codingPanel.planSteps.map((step, i) => {
                      const effectiveState =
                        codingState === 'stopped' && step.state === 'active'
                          ? 'pending'
                          : step.state;
                      return (
                        <div
                          key={step.id}
                          className="flex items-start justify-between gap-3 rounded border border-white/10 bg-[#121411] p-3"
                        >
                          <div className="flex items-start gap-3">
                            <span className="font-mono text-xs text-[#95E329] tabular-nums">
                              0{i + 1}
                            </span>
                            <p className="text-xs leading-relaxed text-neutral-200">
                              {step.title}
                            </p>
                          </div>
                          <span
                            className={`shrink-0 font-mono text-[11px] uppercase ${
                              effectiveState === 'done'
                                ? 'text-[#95E329]'
                                : effectiveState === 'active'
                                  ? 'text-white'
                                  : 'text-neutral-500'
                            }`}
                          >
                            {effectiveState}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}

                {codingTab === 'files' && (
                  <div className="space-y-2.5">
                    {t.agentsAndCoding.codingPanel.scopedFiles.map((file) => (
                      <div
                        key={file.path}
                        className="rounded border border-white/10 bg-[#121411] p-3"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="font-mono text-xs font-medium text-white">
                            {file.path}
                          </span>
                          <span className="font-mono text-[11px] text-[#95E329]">
                            {file.action}
                          </span>
                        </div>
                        <p className="mt-1 text-xs text-neutral-400">
                          {file.note}
                        </p>
                      </div>
                    ))}
                  </div>
                )}

                {codingTab === 'workspace' && (
                  <div className="space-y-2.5">
                    {t.agentsAndCoding.codingPanel.workspaceOptions.map((opt) => (
                      <div
                        key={opt.label}
                        className="flex flex-col justify-between gap-1 rounded border border-white/10 bg-[#121411] p-3 sm:flex-row sm:items-center"
                      >
                        <span className="text-xs font-medium text-neutral-400">
                          {opt.label}
                        </span>
                        <span className="text-xs font-semibold text-white">
                          {opt.value}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Follow-up Instruction Preview */}
              <form onSubmit={handleFollowUpSubmit} className="mt-5 border-t border-white/10 pt-4">
                <label
                  htmlFor="coding-followup-input"
                  className="block text-xs font-medium text-neutral-300"
                >
                  {t.agentsAndCoding.codingPanel.followUpLabel}
                </label>
                <div className="mt-2 flex flex-col gap-2 sm:flex-row">
                  <input
                    id="coding-followup-input"
                    type="text"
                    value={followUpInput}
                    onChange={(e) => setFollowUpInput(e.target.value)}
                    placeholder={t.agentsAndCoding.codingPanel.followUpPlaceholder}
                    className="min-h-[44px] flex-1 rounded-md border border-white/15 bg-[#080A08] px-3.5 py-2 text-xs text-white placeholder:text-neutral-500"
                  />
                  <button
                    type="submit"
                    className="min-h-[44px] shrink-0 whitespace-nowrap rounded-md border border-[#95E329] bg-[#141B10] px-4 py-2 text-xs font-semibold text-[#95E329] transition-colors hover:bg-[#95E329] hover:text-[#060706]"
                  >
                    {t.agentsAndCoding.codingPanel.followUpSend}
                  </button>
                </div>
                {submittedFollowUp && (
                  <p className="mt-2 text-xs text-[#95E329]">
                    {t.agentsAndCoding.codingPanel.followUpAck}{' '}
                    <span className="font-mono text-white">{submittedFollowUp}</span>
                  </p>
                )}
              </form>
            </div>

            <p className="mt-4 text-[11px] text-neutral-400">
              {t.agentsAndCoding.codingPanel.reviewNote}
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
