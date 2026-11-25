import { useRef } from 'react';
import { animate } from 'animejs';
import { ArrowUpRight } from 'lucide-react';
import { assetPath, RELEASES_URL } from '../constants';
import { useAnimeScope, useInViewport } from '../hooks/useAnimeScope';
import { useLanguage } from '../i18n/LanguageContext';

export function FinalCtaAndFooter() {
  const { lang, t } = useLanguage();
  const ctaRef = useRef<HTMLElement | null>(null);
  const inViewport = useInViewport(ctaRef);

  useAnimeScope(
    ctaRef,
    (_scope, reduceMotion) => {
      if (reduceMotion || !inViewport) return;
      animate('.final-cta-reveal', {
        opacity: [0.65, 1],
        translateY: [8, 0],
        duration: 420,
        ease: 'outQuad',
      });
    },
    [inViewport, lang],
  );

  return (
    <>
      <section
        ref={ctaRef}
        className="border-b border-white/10 bg-[#0A0B0A] py-20 lg:py-28"
      >
        <div className="mx-auto max-w-[1240px] px-4 sm:px-6 lg:px-8">
          <div className="final-cta-reveal mx-auto max-w-2xl text-center">
            <img
              src={assetPath('olcr-mark.png')}
              alt="OLCR mark"
              className="mx-auto h-12 w-12 object-contain"
              width={48}
              height={48}
            />
            <h2
              style={{ textWrap: 'balance' }}
              className="mt-6 font-display text-3xl font-bold tracking-tight text-white sm:text-4xl lg:text-[2.75rem]"
            >
              {t.finalCta.headline}
            </h2>
            <p className="mt-4 text-base leading-relaxed text-neutral-300">
              {t.finalCta.body}
            </p>
            <div className="mt-8 flex justify-center">
              <a
                href={RELEASES_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex min-h-[48px] items-center gap-2 whitespace-nowrap rounded-md bg-[#95E329] px-7 py-3 text-sm font-semibold text-[#060706] transition-colors duration-150 hover:bg-[#a6ee3f]"
              >
                <span>{t.finalCta.cta}</span>
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </a>
            </div>
            <p className="mt-5 text-xs leading-relaxed text-neutral-400">
              {t.finalCta.conditionNote}
            </p>
          </div>
        </div>
      </section>

      <footer className="bg-[#060706] py-8">
        <div className="mx-auto flex max-w-[1240px] flex-col justify-between gap-4 px-4 sm:px-6 md:flex-row md:items-center lg:px-8">
          <div className="flex items-center gap-3">
            <img
              src={assetPath('olcr-mark.png')}
              alt="OLCR mark"
              className="h-5 w-5 object-contain"
              width={20}
              height={20}
            />
            <span className="text-xs text-neutral-400">
              {t.footer.wordmarkNote}
            </span>
          </div>

          <div className="flex flex-col gap-2 md:items-end">
            <p className="text-xs leading-relaxed text-neutral-500">
              {t.footer.disclosure}
            </p>
          </div>
        </div>
      </footer>
    </>
  );
}
