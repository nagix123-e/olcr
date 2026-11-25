import { useRef, useState } from 'react';
import { animate } from 'animejs';
import { useAnimeScope } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function HybridSection() {
  const { t, lang } = useLanguage();
  const [selectedTaskId, setSelectedTaskId] = useState(t.hybrid.taskExamples[0].id);
  const routeRef = useRef<HTMLDivElement | null>(null);

  const activeTask =
    t.hybrid.taskExamples.find((item) => item.id === selectedTaskId) ??
    t.hybrid.taskExamples[0];

  useAnimeScope(
    routeRef,
    (_scope, reduceMotion) => {
      if (reduceMotion) return;
      animate('.hybrid-route-card', {
        opacity: [0.65, 1],
        translateY: [4, 0],
        duration: 220,
        ease: 'outQuad',
      });
    },
    [selectedTaskId, lang],
  );

  const taskNoteByColumn: Record<'local' | 'hybrid' | 'connected', string> = {
    local: activeTask.localNote,
    hybrid: activeTask.hybridNote,
    connected: activeTask.connectedNote,
  };

  return (
    <section
      id="hybrid"
      className="border-b border-white/10 bg-[#0A0B0A] py-20 lg:py-28"
    >
      <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
        <div className="max-w-2xl">
          <p className="text-xs font-medium text-[#95E329]">
            {t.hybrid.eyebrow}
          </p>
          <h2
            style={{ textWrap: 'balance' }}
            className="mt-3 font-display text-3xl font-bold tracking-tight text-white sm:text-4xl"
          >
            {t.hybrid.headline}
          </h2>
          <p className="mt-4 text-base leading-relaxed text-neutral-300">
            {t.hybrid.lead}
          </p>
        </div>

        {/* Unified Task Selector + 3-Path Route & Tradeoff Matrix */}
        <div
          ref={routeRef}
          className="mt-10 rounded-lg border border-white/12 bg-[#0D0F0C] p-5 sm:p-6"
        >
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 pb-4">
            <span className="text-xs font-semibold text-white">
              {t.hybrid.selectorLabel}
            </span>
            <span className="text-xs text-neutral-400">
              {t.hybrid.illustrativeNotice}
            </span>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            {t.hybrid.taskExamples.map((task) => {
              const selected = task.id === activeTask.id;
              return (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => setSelectedTaskId(task.id)}
                  aria-pressed={selected}
                  className={`min-h-[42px] whitespace-nowrap rounded-md border px-4 py-2 text-xs font-medium transition-colors duration-150 ${
                    selected
                      ? 'border-[#95E329] bg-[#95E329] text-[#060706]'
                      : 'border-white/12 bg-[#141712] text-neutral-200 hover:border-white/30 hover:text-white'
                  }`}
                >
                  {task.label}
                </button>
              );
            })}
          </div>

          <div className="mt-5 rounded-md border border-white/10 bg-[#080A08] px-4 py-3">
            <p className="font-mono text-xs text-neutral-200">
              {activeTask.promptExample}
            </p>
          </div>

          <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-3">
            {t.hybrid.columns.map((col) => {
              const isHighlighted = activeTask.recommendedPath === col.id;
              return (
                <div
                  key={col.id}
                  className={`hybrid-route-card flex flex-col justify-between rounded-md border p-4 transition-colors duration-150 ${
                    isHighlighted
                      ? 'border-[#95E329] bg-[#141B10]'
                      : 'border-white/10 bg-[#121411]'
                  }`}
                >
                  <div>
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className={`text-sm font-semibold ${
                          isHighlighted ? 'text-[#95E329]' : 'text-white'
                        }`}
                      >
                        {col.title}
                      </span>
                      {isHighlighted && (
                        <span className="font-mono text-[11px] font-medium text-[#95E329]">
                          FIT
                        </span>
                      )}
                    </div>
                    <p className="mt-2.5 text-xs leading-relaxed text-neutral-200">
                      {taskNoteByColumn[col.id]}
                    </p>
                  </div>

                  <div className="mt-4 space-y-1.5 border-t border-white/10 pt-3 text-[11px] text-neutral-400">
                    <p>{col.requirements}</p>
                    <p className="text-neutral-300">{col.costNote}</p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
