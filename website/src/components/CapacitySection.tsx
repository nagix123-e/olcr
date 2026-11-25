import { useRef, useState } from 'react';
import { animate, stagger } from 'animejs';
import { useAnimeScope } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function CapacitySection() {
  const { t, lang } = useLanguage();
  const [selectedTierIdx, setSelectedTierIdx] = useState(0);
  const mapRef = useRef<HTMLDivElement | null>(null);

  const activeTier = t.capacity.tiers[selectedTierIdx] ?? t.capacity.tiers[0];

  useAnimeScope(
    mapRef,
    (_scope, reduceMotion) => {
      if (reduceMotion) return;
      animate('.capacity-bar-active', {
        scaleY: [0.7, 1],
        opacity: [0.5, 1],
        duration: 280,
        delay: stagger(55),
        ease: 'outQuad',
      });
      animate('.capacity-detail-card', {
        opacity: [0.5, 1],
        translateY: [6, 0],
        duration: 220,
        ease: 'outQuad',
      });
    },
    [selectedTierIdx, lang],
  );

  return (
    <section
      id="capacity"
      className="border-b border-white/10 bg-[#060706] py-20 lg:py-28"
    >
      <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
        <div className="max-w-2xl">
          <p className="text-xs font-medium text-[#95E329]">
            {t.capacity.eyebrow}
          </p>
          <h2
            style={{ textWrap: 'balance' }}
            className="mt-3 font-display text-3xl font-bold tracking-tight text-white sm:text-4xl"
          >
            {t.capacity.headline}
          </h2>
          <p className="mt-4 text-base leading-relaxed text-neutral-300">
            {t.capacity.lead}
          </p>
        </div>

        <div
          ref={mapRef}
          className="mt-12 grid grid-cols-1 items-stretch gap-8 lg:grid-cols-12"
        >
          {/* Left: Interactive Task Selector Tiers */}
          <div className="flex flex-col justify-between gap-3 lg:col-span-5">
            {t.capacity.tiers.map((tier, idx) => {
              const selected = idx === selectedTierIdx;
              return (
                <button
                  key={tier.id}
                  type="button"
                  onClick={() => setSelectedTierIdx(idx)}
                  aria-pressed={selected}
                  className={`flex w-full flex-1 flex-col justify-center rounded-lg border p-4 text-left transition-colors duration-150 sm:p-5 ${
                    selected
                      ? 'border-[#95E329] bg-[#141B10]'
                      : 'border-white/10 bg-[#0D0F0C] hover:border-white/25'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs font-medium text-[#95E329] tabular-nums">
                      {tier.index}
                    </span>
                    <span className="text-xs text-neutral-400">
                      {tier.executionMode}
                    </span>
                  </div>
                  <p className="mt-1.5 text-base font-semibold text-white">
                    {tier.taskType}
                  </p>
                </button>
              );
            })}
          </div>

          {/* Right: Interactive Capability Map Readout */}
          <div className="lg:col-span-7">
            <div className="capacity-detail-card flex h-full flex-col justify-between rounded-lg border border-white/12 bg-[#0D0F0C] p-6 sm:p-8">
              <div>
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 pb-4">
                  <span className="font-mono text-xs font-medium text-[#95E329]">
                    {t.capacity.mapTitle}
                  </span>
                  <span className="font-mono text-xs text-neutral-400 tabular-nums">
                    {activeTier.index}
                  </span>
                </div>

                {/* Visual Capacity Scale Bars */}
                <div className="mt-6 flex items-end gap-3 rounded-md border border-white/10 bg-[#080A08] p-5">
                  {[1, 2, 3, 4].map((level) => {
                    const isFilled = level <= activeTier.activeLevel;
                    const heights = ['h-8', 'h-14', 'h-20', 'h-28'];
                    return (
                      <div key={level} className="flex flex-1 flex-col items-center gap-2">
                        <div className="flex h-28 w-full items-end">
                          <div
                            className={`w-full origin-bottom rounded-sm ${heights[level - 1]} ${
                              isFilled
                                ? 'capacity-bar-active bg-[#95E329]'
                                : 'bg-white/10'
                            }`}
                          />
                        </div>
                        <span className="font-mono text-[11px] text-neutral-400 tabular-nums">
                          L{level}
                        </span>
                      </div>
                    );
                  })}
                </div>

                {/* Role & Path Breakdown */}
                <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
                  <div className="rounded-md border border-white/10 bg-[#121411] p-4">
                    <p className="text-[11px] font-medium text-neutral-400">
                      {t.capacity.roleHeading}
                    </p>
                    <p className="mt-1.5 text-sm font-semibold text-[#95E329]">
                      {activeTier.roleLabel}
                    </p>
                  </div>
                  <div className="rounded-md border border-white/10 bg-[#121411] p-4">
                    <p className="text-[11px] font-medium text-neutral-400">
                      {t.capacity.modeHeading}
                    </p>
                    <p className="mt-1.5 text-sm font-semibold text-white">
                      {activeTier.executionMode}
                    </p>
                  </div>
                  <div className="rounded-md border border-white/10 bg-[#121411] p-4">
                    <p className="text-[11px] font-medium text-neutral-400">
                      {t.capacity.impactHeading}
                    </p>
                    <p className="mt-1.5 text-sm font-medium text-neutral-200">
                      {activeTier.tokenProfile}
                    </p>
                  </div>
                </div>

                <p className="mt-6 text-sm leading-relaxed text-neutral-200">
                  {activeTier.summary}
                </p>
              </div>

              <div className="mt-6 border-t border-white/10 pt-4">
                <p className="text-xs text-neutral-400">
                  <span className="font-semibold text-neutral-200">
                    {t.capacity.setupLabel}:{' '}
                  </span>
                  {activeTier.setupCondition}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
