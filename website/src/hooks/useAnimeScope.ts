import { useEffect, useState, type RefObject } from 'react';
import { createScope, type Scope } from 'animejs';

export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);

  return reduced;
}

export function useInViewport(
  ref: RefObject<HTMLElement | null>,
  rootMargin = '120px 0px',
): boolean {
  const [isVisible, setIsVisible] = useState(true);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.target === el) {
            setIsVisible(entry.isIntersecting);
          }
        }
      },
      { rootMargin, threshold: 0.05 },
    );

    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, rootMargin]);

  return isVisible;
}

export function useAnimeScope(
  rootRef: RefObject<HTMLElement | null>,
  setup: (scope: Scope, reduceMotion: boolean) => void,
  deps: readonly unknown[] = [],
) {
  const reduceMotion = usePrefersReducedMotion();

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const scope = createScope({
      root,
      mediaQueries: {
        reduceMotion: '(prefers-reduced-motion: reduce)',
      },
    });

    scope.add((self) => {
      if (!self) return;
      const isReduced = Boolean(self.matches?.reduceMotion) || reduceMotion;
      setup(self, isReduced);
    });

    return () => {
      scope.revert();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootRef, reduceMotion, ...deps]);
}
