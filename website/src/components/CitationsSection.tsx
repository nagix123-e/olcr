import { useRef, useState } from 'react';
import { animate } from 'animejs';
import { FileText } from 'lucide-react';
import { useAnimeScope } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function CitationsSection() {
  const { t, lang } = useLanguage();
  const [scenarioIndex, setScenarioIndex] = useState(0);
  const [selectedCitation, setSelectedCitation] = useState<number>(1);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const activeScenario = t.citations.scenarios[scenarioIndex] ?? t.citations.scenarios[0];

  useAnimeScope(
    panelRef,
    (_scope, reduceMotion) => {
      if (reduceMotion) return;
      animate('.citation-evidence-active', {
        opacity: [0.6, 1],
        translateY: [4, 0],
        duration: 240,
        ease: 'outQuad',
      });
    },
    [scenarioIndex, selectedCitation, lang],
  );

  const handleScenarioChange = (idx: number) => {
    setScenarioIndex(idx);
    const nextScenario = t.citations.scenarios[idx];
    if (nextScenario && nextScenario.sources.length > 0) {
      setSelectedCitation(nextScenario.sources[0].index);
    }
  };

  return (
    <section
      id="citations"
      className="border-b border-white/10 bg-[#0A0B0A] py-20 lg:py-28"
    >
      <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
        <div className="grid grid-cols-1 gap-10 lg:grid-cols-12 lg:gap-12">
          {/* Left Narrative & Conditions */}
          <div className="lg:col-span-5">
            <p className="text-xs font-medium text-[#95E329]">
              {t.citations.eyebrow}
            </p>
            <h2
              style={{ textWrap: 'balance' }}
              className="mt-3 font-display text-3xl font-bold tracking-tight text-white sm:text-4xl"
            >
              {t.citations.headline}
            </h2>
            <p className="mt-4 text-base leading-relaxed text-neutral-300">
              {t.citations.lead}
            </p>

            <div className="mt-8 space-y-5 border-t border-white/10 pt-6">
              <div>
                <h3 className="text-sm font-semibold text-white">
                  {t.citations.conditionHeading}
                </h3>
                <p className="mt-1.5 text-sm leading-relaxed text-neutral-300">
                  {t.citations.conditionBody}
                </p>
              </div>

              <p className="rounded-md border border-white/10 bg-[#10120F] p-4 text-xs leading-relaxed text-neutral-300">
                {t.citations.trustCaveat}
              </p>
            </div>
          </div>

          {/* Right Interactive Evidence & Citation Panel */}
          <div className="lg:col-span-7" ref={panelRef}>
            <div className="rounded-lg border border-white/12 bg-[#0D0F0C] p-5 sm:p-6">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/10 pb-3">
                <span className="font-mono text-xs font-medium text-[#95E329]">
                  {t.citations.evidencePanelTitle}
                </span>
                <span className="text-xs text-neutral-400">
                  {t.citations.illustrativeLabel}
                </span>
              </div>

              {/* Scenario Switcher */}
              <div
                role="tablist"
                aria-label="Citation scenario examples"
                className="mt-4 flex flex-wrap gap-2"
              >
                {t.citations.scenarios.map((scenario, idx) => {
                  const selected = idx === scenarioIndex;
                  return (
                    <button
                      key={scenario.id}
                      type="button"
                      role="tab"
                      aria-selected={selected}
                      onClick={() => handleScenarioChange(idx)}
                      className={`min-h-[40px] whitespace-nowrap rounded-md border px-3.5 py-2 text-xs font-medium transition-colors duration-150 ${
                        selected
                          ? 'border-[#95E329] bg-[#141B10] text-[#95E329]'
                          : 'border-white/12 bg-[#121411] text-neutral-300 hover:border-white/25 hover:text-white'
                      }`}
                    >
                      {scenario.tabLabel}
                    </button>
                  );
                })}
              </div>

              {/* Query & Grounded Answer */}
              <div className="mt-5 rounded-md border border-white/10 bg-[#080A08] p-4">
                <p className="text-[11px] font-medium text-neutral-400">
                  {activeScenario.modeNote}
                </p>
                <p className="mt-2 font-mono text-xs text-neutral-200">
                  {activeScenario.query}
                </p>

                <div className="mt-4 border-t border-white/10 pt-4 text-sm leading-relaxed text-neutral-100">
                  {activeScenario.answerSegments.map((seg, i) =>
                    seg.citationIndex !== undefined ? (
                      <button
                        key={i}
                        type="button"
                        onClick={() => setSelectedCitation(seg.citationIndex!)}
                        className={`mx-0.5 inline-flex items-center rounded px-1.5 py-0.5 font-mono text-xs font-semibold transition-colors duration-150 ${
                          selectedCitation === seg.citationIndex
                            ? 'bg-[#95E329] text-[#060706]'
                            : 'bg-[#1D2616] text-[#95E329] hover:bg-[#95E329]/30'
                        }`}
                      >
                        {seg.text}
                      </button>
                    ) : (
                      <span key={i}>{seg.text}</span>
                    ),
                  )}
                </div>

                {activeScenario.sources.length > 0 && (
                  <p className="mt-3 text-[11px] text-neutral-400">
                    {t.citations.selectHint}
                  </p>
                )}
              </div>

              {/* Source Records List or No-Citation Explanation */}
              <div className="mt-4 space-y-3">
                {activeScenario.sources.length > 0 ? (
                  activeScenario.sources.map((src) => {
                    const isFocused = selectedCitation === src.index;
                    return (
                      <button
                        key={src.index}
                        type="button"
                        onClick={() => setSelectedCitation(src.index)}
                        className={`block w-full rounded-md border p-4 text-left transition-colors duration-150 ${
                          isFocused
                            ? 'citation-evidence-active border-[#95E329]/70 bg-[#141B10]'
                            : 'border-white/10 bg-[#121411] hover:border-white/25'
                        }`}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <FileText
                              className={`h-4 w-4 ${
                                isFocused ? 'text-[#95E329]' : 'text-neutral-400'
                              }`}
                              aria-hidden="true"
                            />
                            <span className="font-mono text-xs font-semibold text-[#95E329]">
                              [{src.index}]
                            </span>
                            <span className="text-sm font-semibold text-white">
                              {src.title}
                            </span>
                          </div>
                        </div>
                        <p className="mt-1 font-mono text-[11px] text-neutral-400">
                          {src.origin}
                        </p>
                        <p className="mt-2 text-xs leading-relaxed text-neutral-200">
                          {src.excerpt}
                        </p>
                        <p className="mt-2 text-[11px] text-neutral-400">
                          {src.condition}
                        </p>
                      </button>
                    );
                  })
                ) : (
                  <div className="rounded-md border border-white/10 bg-[#121411] p-4">
                    <p className="text-xs leading-relaxed text-neutral-300">
                      {activeScenario.noCitationNotice}
                    </p>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
