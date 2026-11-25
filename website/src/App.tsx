/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { AgentsAndCodingSection } from './components/AgentsAndCodingSection';
import { CapacitySection } from './components/CapacitySection';
import { CitationsSection } from './components/CitationsSection';
import { FinalCtaAndFooter } from './components/FinalCtaAndFooter';
import { HeroSection } from './components/HeroSection';
import { HybridSection } from './components/HybridSection';
import { Navbar } from './components/Navbar';
import { RoutingSection } from './components/RoutingSection';
import { TokenSpendSection } from './components/TokenSpendSection';
import { LanguageProvider } from './i18n/LanguageContext';

export default function App() {
  return (
    <LanguageProvider>
      <div className="min-h-screen bg-[#060706] text-[#F4F6F2]">
        <Navbar />
        <main>
          <HeroSection />
          <TokenSpendSection />
          <RoutingSection />
          <CitationsSection />
          <CapacitySection />
          <HybridSection />
          <AgentsAndCodingSection />
        </main>
        <FinalCtaAndFooter />
      </div>
    </LanguageProvider>
  );
}
