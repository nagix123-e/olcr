import { useRef } from 'react';
import { animate, stagger } from 'animejs';
import { ArrowDown, ArrowUpRight } from 'lucide-react';
import { assetPath, RELEASES_URL } from '../constants';
import { useAnimeScope, useInViewport } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function HeroSection() {
  const { t, lang } = useLanguage();
  const rootRef = useRef<HTMLElement | null>(null);
  const inViewport = useInViewport(rootRef);

  useAnimeScope(
    rootRef,
    (_scope, reduceMotion) => {
      if (reduceMotion || !inViewport) return;

      animate('.hero-pulse-local', {
        translateX: [0, 156],
        opacity: [0, 1, 1, 0],
        duration: 2100,
        delay: stagger(340),
        ease: 'inOutQuad',
        loop: true,
      });

      animate('.hero-pulse-external', {
        translateX: [0, 156],
        opacity: [0, 0.9, 0.9, 0],
        duration: 3200,
        delay: 750,
        ease: 'inOutQuad',
        loop: true,
      });
    },
    [inViewport, lang],
  );

  return (
    <section
      id="top"
      ref={rootRef}
      className="border-b border-white/10 bg-[#060706] pb-14 pt-12 sm:pt-16 lg:pb-20 lg:pt-20"
    >
      <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
        <div className="grid grid-cols-1 items-start gap-12 lg:grid-cols-12 lg:gap-12">
          {/* Left: Dominant product headline & primary CTA */}
          <div className="lg:col-span-6">
            <div className="mb-6 flex items-center gap-3">
              <img
                src={assetPath('olcr-mark.png')}
                alt="OLCR"
                className="h-9 w-9 object-contain"
                width={36}
                height={36}
              />
              <p className="text-xs font-medium tracking-wide text-neutral-400">
                {t.hero.kicker}
              </p>
            </div>

            <h1
              style={{ textWrap: 'balance' }}
              className="font-display text-4xl font-bold leading-[1.08] tracking-tight text-white sm:text-5xl lg:text-[3.5rem]"
            >
              {t.hero.headline}
            </h1>

            <p className="mt-6 max-w-xl text-base leading-relaxed text-neutral-300 sm:text-lg">
              {t.hero.subheadline}
            </p>

            <div className="mt-8 flex flex-wrap items-center gap-4">
              <a
                href={RELEASES_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex min-h-[48px] shrink-0 items-center gap-2 whitespace-nowrap rounded-md bg-[#95E329] px-6 py-3 text-sm font-semibold text-[#060706] transition-colors duration-150 hover:bg-[#a6ee3f]"
              >
                <span>{t.hero.primaryCta}</span>
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </a>

              <a
                href="#token-spend"
                className="inline-flex min-h-[48px] shrink-0 items-center gap-2 whitespace-nowrap rounded-md border border-white/15 px-5 py-3 text-sm font-medium text-neutral-200 transition-colors duration-150 hover:border-white/30 hover:text-white"
              >
                <span>{t.hero.secondaryAnchor}</span>
                <ArrowDown className="h-4 w-4 text-[#95E329]" aria-hidden="true" />
              </a>
            </div>
          </div>

          {/* Right: Qualitative Token-Stream Illustration */}
          <div className="lg:col-span-6">
            <div className="rounded-lg border border-white/12 bg-[#0D0F0C] p-5 sm:p-6">
              <div className="mb-5 flex flex-wrap items-center justify-between gap-2 border-b border-white/10 pb-3">
                <span className="font-mono text-xs font-medium text-[#95E329]">
                  OLCR · Flow Architecture
                </span>
                <span className="text-xs text-neutral-400">
                  {t.hero.diagramLabel}
                </span>
              </div>

              <div className="space-y-4">
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="rounded-md border border-white/10 bg-[#141712] p-3.5">
                    <span className="font-mono text-[11px] text-neutral-400">01</span>
                    <p className="mt-1 text-sm font-semibold text-white">
                      {t.hero.diagramStages.request}
                    </p>
                  </div>
                  <div className="rounded-md border border-[#95E329]/45 bg-[#141712] p-3.5">
                    <span className="font-mono text-[11px] text-[#95E329]">02</span>
                    <p className="mt-1 text-sm font-semibold text-white">
                      {t.hero.diagramStages.localRouting}
                    </p>
                  </div>
                </div>

                <div className="rounded-md border border-white/10 bg-[#080A08] p-4">
                  <div className="space-y-4">
                    <div>
                      <div className="mb-1.5 flex items-center justify-between gap-2">
                        <span className="text-xs font-semibold text-[#95E329]">
                          {t.hero.diagramStages.localPath}
                        </span>
                        <span className="text-[11px] text-neutral-400">
                          {t.hero.diagramNotes.narrowed}
                        </span>
                      </div>
                      <div className="relative h-7 overflow-hidden rounded border border-[#95E329]/30 bg-[#131910]">
                        <div className="absolute inset-y-0 left-3 flex items-center gap-3">
                          <span className="hero-pulse-local h-2 w-8 rounded-sm bg-[#95E329]" />
                          <span className="hero-pulse-local h-2 w-6 rounded-sm bg-[#95E329]/80" />
                          <span className="hero-pulse-local h-2 w-10 rounded-sm bg-[#95E329]/65" />
                        </div>
                      </div>
                      <p className="mt-1.5 text-[11px] text-neutral-400">
                        {t.hero.diagramNotes.localCompute}
                      </p>
                    </div>

                    <div>
                      <div className="mb-1.5 flex items-center justify-between gap-2">
                        <span className="text-xs font-medium text-neutral-200">
                          {t.hero.diagramStages.externalPath}
                        </span>
                        <span className="text-[11px] text-neutral-400">
                          {t.hero.diagramNotes.reserved}
                        </span>
                      </div>
                      <div className="relative h-4 overflow-hidden rounded border border-white/15 bg-[#121311]">
                        <div className="absolute inset-y-0 left-3 flex items-center">
                          <span className="hero-pulse-external h-1.5 w-6 rounded-sm bg-neutral-300" />
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="flex items-center justify-between gap-4 rounded-md border border-white/12 bg-[#141712] px-4 py-3">
                  <div>
                    <span className="font-mono text-[11px] text-[#95E329]">04</span>
                    <p className="text-sm font-semibold text-white">
                      {t.hero.diagramStages.output}
                    </p>
                  </div>
                  <span className="hidden font-mono text-xs text-neutral-400 sm:inline">
                    request → route → source/model → result
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Compact 5-point First-Screen Strip (non-redundant one-line index) */}
        <div className="mt-12 grid grid-cols-1 gap-px overflow-hidden rounded-lg border border-white/10 bg-white/10 sm:grid-cols-2 lg:grid-cols-5">
          {t.hero.orientationStrip.map((item) => (
            <div key={item.index} className="flex items-center gap-2.5 bg-[#0A0B0A] px-4 py-3.5">
              <span className="font-mono text-xs font-medium text-[#95E329] tabular-nums">
                {item.index}
              </span>
              <span className="text-xs font-medium text-neutral-200">
                {item.title}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
