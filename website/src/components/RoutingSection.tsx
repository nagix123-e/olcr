import { useEffect, useRef, useState } from 'react';
import { animate } from 'animejs';
import { useAnimeScope } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function RoutingSection() {
  const { t, lang } = useLanguage();
  const [activeIndex, setActiveIndex] = useState(0);
  const diagramRef = useRef<HTMLDivElement | null>(null);
  const stageRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const idx = Number(entry.target.getAttribute('data-stage-index'));
            if (!Number.isNaN(idx)) {
              setActiveIndex(idx);
            }
          }
        }
      },
      {
        rootMargin: '-32% 0px -48% 0px',
        threshold: 0.1,
      },
    );

    stageRefs.current.forEach((el) => {
      if (el) observer.observe(el);
    });

    return () => observer.disconnect();
  }, [lang]);

  useAnimeScope(
    diagramRef,
    (_scope, reduceMotion) => {
      if (reduceMotion) return;
      animate('.routing-active-highlight', {
        opacity: [0.45, 1],
        scale: [0.985, 1],
        duration: 220,
        ease: 'outQuad',
      });
    },
    [activeIndex, lang],
  );

  const renderSchematicNode = (idx: number, compact = false) => {
    const stage = t.routing.stages[idx];
    if (!stage) return null;
    const isActive = activeIndex === idx;
    return (
      <div
        key={stage.id}
        className={`rounded-md border transition-colors duration-150 ${
          compact ? 'p-3' : 'p-3.5'
        } ${
          isActive
            ? 'routing-active-highlight border-[#95E329] bg-[#141B10]'
            : 'border-white/10 bg-[#121411]'
        }`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-[11px] text-[#95E329] tabular-nums">
            {stage.index}
          </span>
          <span className="text-[11px] text-neutral-400">
            {stage.pathLabel}
          </span>
        </div>
        <p className={`mt-1 font-semibold text-white ${compact ? 'text-xs' : 'text-sm'}`}>
          {stage.title}
        </p>
      </div>
    );
  };

  return (
    <section
      id="routing"
      className="border-b border-white/10 bg-[#060706] py-20 lg:py-28"
    >
      <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
        <div className="max-w-2xl">
          <p className="text-xs font-medium text-[#95E329]">
            {t.routing.eyebrow}
          </p>
          <h2
            style={{ textWrap: 'balance' }}
            className="mt-3 font-display text-3xl font-bold tracking-tight text-white sm:text-4xl"
          >
            {t.routing.headline}
          </h2>
          <p className="mt-4 text-base leading-relaxed text-neutral-300">
            {t.routing.lead}
          </p>
        </div>

        <div className="mt-12 grid grid-cols-1 items-start gap-8 lg:grid-cols-12 lg:gap-10">
          {/* Sticky Visual Topology Panel (display-only mirror synced to scroll & right-column stage buttons) */}
          <div className="lg:sticky lg:top-24 lg:col-span-6" ref={diagramRef}>
            <div className="rounded-lg border border-white/12 bg-[#0D0F0C] p-5 sm:p-6">
              <div className="border-b border-white/10 pb-3">
                <h3 className="text-sm font-semibold text-white">
                  {t.routing.diagramHeading}
                </h3>
                <p className="mt-1 text-xs leading-relaxed text-neutral-400">
                  {t.routing.diagramNote}
                </p>
              </div>

              <div className="mt-5 space-y-3">
                {renderSchematicNode(0)}
                {renderSchematicNode(1)}

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-2.5 rounded-md border border-white/10 bg-[#090B08] p-3">
                    <p className="font-mono text-[11px] text-neutral-400">
                      {t.routing.inferenceSplitLabel}
                    </p>
                    {renderSchematicNode(2, true)}
                    {renderSchematicNode(3, true)}
                  </div>

                  <div className="space-y-2.5 rounded-md border border-white/10 bg-[#090B08] p-3">
                    <p className="font-mono text-[11px] text-neutral-400">
                      {t.routing.retrievalSplitLabel}
                    </p>
                    {renderSchematicNode(4, true)}
                    {renderSchematicNode(5, true)}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Right Scrollable & Interactive Stage Selector List */}
          <div className="space-y-4 lg:col-span-6">
            {t.routing.stages.map((stage, idx) => {
              const isActive = activeIndex === idx;
              return (
                <button
                  key={stage.id}
                  ref={(el) => {
                    stageRefs.current[idx] = el;
                  }}
                  data-stage-index={idx}
                  type="button"
                  onClick={() => setActiveIndex(idx)}
                  aria-current={isActive ? 'step' : undefined}
                  className={`block w-full rounded-lg border p-5 text-left transition-colors duration-150 ${
                    isActive
                      ? 'border-[#95E329]/70 bg-[#12160F]'
                      : 'border-white/10 bg-[#0D0F0C] hover:border-white/25'
                  }`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-mono text-xs font-medium text-[#95E329] tabular-nums">
                      {stage.index}
                    </span>
                    <span className="text-xs text-neutral-400">
                      {stage.pathLabel}
                    </span>
                  </div>
                  <h3 className="mt-2 text-lg font-semibold text-white">
                    {stage.title}
                  </h3>
                  <p className="mt-1.5 text-sm leading-relaxed text-neutral-300">
                    {stage.detail}
                  </p>
                  <p className="mt-2.5 text-xs text-neutral-400">
                    {stage.condition}
                  </p>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
