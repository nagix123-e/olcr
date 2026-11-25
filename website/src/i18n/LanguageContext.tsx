import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { translations, type Lang, type TranslationContent } from './translations';

interface LanguageContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: TranslationContent;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

function detectInitialLanguage(): Lang {
  if (typeof window === 'undefined') return 'en';
  try {
    const saved = window.localStorage.getItem('olcr-site-lang');
    if (saved === 'ja' || saved === 'en') return saved;
  } catch {
    // Ignore storage access errors
  }
  if (typeof navigator !== 'undefined' && navigator.language?.toLowerCase().startsWith('ja')) {
    return 'ja';
  }
  return 'en';
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(detectInitialLanguage);

  const setLang = (next: Lang) => {
    setLangState(next);
    try {
      window.localStorage.setItem('olcr-site-lang', next);
    } catch {
      // Ignore storage access errors
    }
  };

  useEffect(() => {
    const content = translations[lang];
    document.documentElement.lang = lang;
    document.title = content.meta.title;
    const descMeta = document.querySelector('meta[name="description"]');
    if (descMeta) {
      descMeta.setAttribute('content', content.meta.description);
    }
  }, [lang]);

  return (
    <LanguageContext.Provider value={{ lang, setLang, t: translations[lang] }}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLanguage(): LanguageContextValue {
  const ctx = useContext(LanguageContext);
  if (!ctx) {
    throw new Error('useLanguage must be used within LanguageProvider');
  }
  return ctx;
}
