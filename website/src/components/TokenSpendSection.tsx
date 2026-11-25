import { useRef, useState } from 'react';
import { animate, stagger } from 'animejs';
import { RotateCcw } from 'lucide-react';
import { useAnimeScope, useInViewport } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function TokenSpendSection() {
  const { t, lang } = useLanguage();
  const rootRef = useRef<HTMLElement | null>(null);
  const inViewport = useInViewport(rootRef);
  const [replayCount, setReplayCount] = useState(0);

  useAnimeScope(
    rootRef,
    (_scope, reduceMotion) => {
      if (reduceMotion || !inViewport) return;

      animate('.legacy-packet', {
        translateX: [-12, 12],
        opacity: [0.35, 1, 0.35],
        duration: 1400,
        delay: stagger(180),
        ease: 'inOutSine',
        loop: true,
      });

      animate('.olcr-local-packet', {
        scaleX: [0.92, 1],
        opacity: [0.65, 1],
        duration: 1300,
        delay: stagger(160),
        ease: 'outQuad',
      });

      animate('.olcr-external-packet', {
        translateX: [-8, 8],
        opacity: [0.4, 1, 0.4],
        duration: 2100,
        ease: 'inOutSine',
        loop: true,
      });
    },
    [inViewport, replayCount, lang],
  );

  return (
    <section
      id="token-spend"
      ref={rootRef}
      className="border-b border-white/10 bg-[#0A0B0A] py-20 lg:py-28"
    >
      <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
        <div className="flex flex-col justify-between gap-6 lg:flex-row lg:items-end">
          <div className="max-w-2xl">
            <p className="text-xs font-medium text-[#95E329]">
              {t.tokenSpend.eyebrow}
            </p>
            <h2
              style={{ textWrap: 'balance' }}
              className="mt-3 font-display text-3xl font-bold tracking-tight text-white sm:text-4xl"
            >
              {t.tokenSpend.headline}
            </h2>
            <p className="mt-4 text-base leading-relaxed text-neutral-300">
              {t.tokenSpend.lead}
            </p>
          </div>

          <button
            type="button"
            onClick={() => setReplayCount((c) => c + 1)}
            className="inline-flex min-h-[44px] shrink-0 items-center gap-2 self-start whitespace-nowrap rounded-md border border-white/15 bg-[#10120F] px-4 py-2 text-xs font-medium text-neutral-200 transition-colors duration-150 hover:border-[#95E329]/50 hover:text-white lg:self-auto"
          >
            <RotateCcw className="h-3.5 w-3.5 text-[#95E329]" aria-hidden="true" />
            <span>{t.tokenSpend.replayBtn}</span>
          </button>
        </div>

        <p className="mt-6 text-xs text-neutral-400">
          {t.tokenSpend.diagramCaption}
        </p>

        {/* Architectural Comparison */}
        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
          {/* Path A: One-model-for-everything */}
          <div className="flex flex-col justify-between rounded-lg border border-white/12 bg-[#10120F] p-6">
            <div>
              <div className="flex items-center justify-between gap-4 border-b border-white/10 pb-4">
                <h3 className="text-lg font-semibold text-white">
                  {t.tokenSpend.legacyTitle}
                </h3>
                <span className="text-xs font-medium text-neutral-400">
                  {t.tokenSpend.legacyBadge}
                </span>
              </div>

              <p className="mt-4 text-sm leading-relaxed text-neutral-300">
                {t.tokenSpend.legacyDesc}
              </p>

              <div className="mt-6 space-y-2.5">
                {t.tokenSpend.legacySteps.map((step, idx) => (
                  <div
                    key={idx}
                    className="flex items-center justify-between gap-3 rounded border border-white/10 bg-[#171916] px-3.5 py-2.5"
                  >
                    <span className="text-xs text-neutral-200">{step}</span>
                    <span className="legacy-packet h-2 w-12 shrink-0 rounded-sm bg-neutral-400/75" />
                  </div>
                ))}
              </div>
            </div>

            <div className="mt-6 border-t border-white/10 pt-4">
              <div className="h-3.5 w-full rounded-sm bg-neutral-500/45" />
              <p className="mt-2 text-xs font-medium text-neutral-300">
                {t.tokenSpend.legacyOutcome}
              </p>
            </div>
          </div>

          {/* Path B: OLCR Local-First Orchestration */}
          <div className="flex flex-col justify-between rounded-lg border border-[#95E329]/45 bg-[#10120F] p-6">
            <div>
              <div className="flex items-center justify-between gap-4 border-b border-white/10 pb-4">
                <h3 className="text-lg font-semibold text-white">
                  {t.tokenSpend.olcrTitle}
                </h3>
                <span className="text-xs font-semibold text-[#95E329]">
                  {t.tokenSpend.olcrBadge}
                </span>
              </div>

              <p className="mt-4 text-sm leading-relaxed text-neutral-300">
                {t.tokenSpend.olcrDesc}
              </p>

              <div className="mt-6 space-y-2.5">
                {t.tokenSpend.olcrLocalSteps.map((step, idx) => (
                  <div
                    key={idx}
                    className="olcr-local-packet flex items-center justify-between gap-3 rounded border border-[#95E329]/25 bg-[#141B10] px-3.5 py-2.5"
                  >
                    <span className="text-xs text-white">{step}</span>
                    <span className="font-mono text-[11px] font-medium text-[#95E329]">
                      LOCAL
                    </span>
                  </div>
                ))}

                <div className="flex items-center justify-between gap-3 rounded border border-white/15 bg-[#161815] px-3.5 py-2.5">
                  <span className="text-xs text-neutral-200">
                    {t.tokenSpend.olcrExternalStep}
                  </span>
                  <span className="olcr-external-packet h-2 w-6 shrink-0 rounded-sm bg-[#95E329]" />
                </div>
              </div>
            </div>

            <div className="mt-6 border-t border-white/10 pt-4">
              <div className="flex items-center gap-2">
                <div className="h-3.5 w-3/4 rounded-sm bg-[#95E329]/25" />
                <div className="h-2 w-1/4 rounded-sm bg-[#95E329]" />
              </div>
              <p className="mt-2 text-xs font-medium text-[#95E329]">
                {t.tokenSpend.olcrOutcome}
              </p>
            </div>
          </div>
        </div>

        <p className="mt-6 rounded-md border border-white/10 bg-[#0D0F0C] px-4 py-3 text-xs leading-relaxed text-neutral-300">
          {t.tokenSpend.footnote}
        </p>
      </div>
    </section>
  );
}
