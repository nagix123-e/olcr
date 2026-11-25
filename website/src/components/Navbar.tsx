import { useState } from 'react';
import { ArrowUpRight, Menu, X } from 'lucide-react';
import { assetPath, RELEASES_URL } from '../constants';
import { useLanguage } from '../i18n/LanguageContext';

export function Navbar() {
  const { lang, setLang, t } = useLanguage();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  return (
    <header className="sticky top-0 z-50 border-b border-white/10 bg-[#060706]/95 backdrop-blur-sm">
      <div className="mx-auto flex h-16 max-w-[1240px] items-center justify-between gap-4 px-4 sm:px-6 lg:gap-8 lg:px-8">
        {/* Zone 1: Brand mark (transparent PNG, no background tile) + single-line wordmark */}
        <a
          href="#top"
          className="flex shrink-0 items-center gap-2.5 whitespace-nowrap text-white focus-visible:outline-none"
        >
          <img
            src={assetPath('olcr-mark.png')}
            alt="OLCR mark"
            className="h-7 w-7 object-contain"
            width={28}
            height={28}
          />
          <span className="font-display text-lg font-bold tracking-tight text-white">
            OLCR
          </span>
        </a>

        {/* Zone 2: Clean single-line navigation links */}
        <nav
          aria-label="Primary navigation"
          className="hidden items-center gap-6 md:flex lg:gap-8"
        >
          {t.nav.links.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="shrink-0 whitespace-nowrap text-sm font-medium text-neutral-300 underline-offset-4 transition-colors duration-150 hover:text-[#95E329] hover:underline"
            >
              {link.label}
            </a>
          ))}
        </nav>

        {/* Zone 3: Compact JA/EN toggle + primary Releases CTA */}
        <div className="flex shrink-0 items-center gap-3">
          <div
            role="group"
            aria-label={t.nav.langToggleLabel}
            className="inline-flex items-center rounded-md border border-white/15 bg-[#10120F] p-0.5"
          >
            <button
              type="button"
              onClick={() => setLang('ja')}
              aria-pressed={lang === 'ja'}
              className={`min-h-[32px] whitespace-nowrap rounded px-2.5 py-1 text-xs font-medium transition-colors duration-150 ${
                lang === 'ja'
                  ? 'bg-[#95E329] text-[#060706]'
                  : 'text-neutral-300 hover:text-white'
              }`}
            >
              日本語
            </button>
            <button
              type="button"
              onClick={() => setLang('en')}
              aria-pressed={lang === 'en'}
              className={`min-h-[32px] whitespace-nowrap rounded px-2.5 py-1 text-xs font-medium transition-colors duration-150 ${
                lang === 'en'
                  ? 'bg-[#95E329] text-[#060706]'
                  : 'text-neutral-300 hover:text-white'
              }`}
            >
              EN
            </button>
          </div>

          <a
            href={RELEASES_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="hidden min-h-[40px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md bg-[#95E329] px-4 py-2 text-xs font-semibold text-[#060706] transition-colors duration-150 hover:bg-[#a6ee3f] sm:inline-flex"
          >
            <span>{t.nav.cta}</span>
            <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
          </a>

          <button
            type="button"
            onClick={() => setMobileMenuOpen((prev) => !prev)}
            aria-expanded={mobileMenuOpen}
            aria-label="Toggle navigation menu"
            className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md border border-white/15 text-neutral-200 hover:text-white md:hidden"
          >
            {mobileMenuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
        </div>
      </div>

      {mobileMenuOpen && (
        <div className="border-t border-white/10 bg-[#0A0B0A] px-4 py-4 md:hidden">
          <nav aria-label="Mobile navigation" className="flex flex-col gap-2">
            {t.nav.links.map((link) => (
              <a
                key={link.href}
                href={link.href}
                onClick={() => setMobileMenuOpen(false)}
                className="flex min-h-[44px] items-center rounded-md px-3 text-sm font-medium text-neutral-200 hover:bg-white/5 hover:text-[#95E329]"
              >
                {link.label}
              </a>
            ))}
            <a
              href={RELEASES_URL}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => setMobileMenuOpen(false)}
              className="mt-2 flex min-h-[44px] items-center justify-center gap-1.5 rounded-md bg-[#95E329] px-4 py-2 text-sm font-semibold text-[#060706]"
            >
              <span>{t.hero.primaryCta}</span>
              <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
            </a>
          </nav>
        </div>
      )}
    </header>
  );
}
