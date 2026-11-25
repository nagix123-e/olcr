export type Lang = 'en' | 'ja';

export interface RoutingStageItem {
  id: string;
  index: string;
  title: string;
  detail: string;
  pathLabel: string;
  condition: string;
}

export interface CitationScenario {
  id: string;
  tabLabel: string;
  modeNote: string;
  query: string;
  answerSegments: Array<{
    text: string;
    citationIndex?: number;
  }>;
  sources: Array<{
    index: number;
    title: string;
    origin: string;
    excerpt: string;
    condition: string;
  }>;
  noCitationNotice?: string;
}

export interface CapacityTier {
  id: string;
  index: string;
  taskType: string;
  roleLabel: string;
  executionMode: string;
  tokenProfile: string;
  summary: string;
  setupCondition: string;
  activeLevel: number;
}

export interface HybridTaskExample {
  id: string;
  label: string;
  promptExample: string;
  localNote: string;
  hybridNote: string;
  connectedNote: string;
  recommendedPath: 'local' | 'hybrid' | 'connected';
}

export interface CustomAgentPreset {
  id: string;
  label: string;
  name: string;
  role: string;
  instructions: string;
  purposeRole: string;
  retrievalEnabled: boolean;
  workspaceScope: string;
}

export interface TranslationContent {
  meta: {
    title: string;
    description: string;
  };
  nav: {
    links: Array<{ href: string; label: string }>;
    cta: string;
    langToggleLabel: string;
  };
  hero: {
    kicker: string;
    headline: string;
    subheadline: string;
    primaryCta: string;
    secondaryAnchor: string;
    diagramLabel: string;
    diagramStages: {
      request: string;
      localRouting: string;
      localPath: string;
      externalPath: string;
      output: string;
    };
    diagramNotes: {
      narrowed: string;
      localCompute: string;
      reserved: string;
    };
    orientationStrip: Array<{
      index: string;
      title: string;
    }>;
  };
  tokenSpend: {
    eyebrow: string;
    headline: string;
    lead: string;
    replayBtn: string;
    diagramCaption: string;
    legacyTitle: string;
    legacyBadge: string;
    legacyDesc: string;
    legacySteps: string[];
    legacyOutcome: string;
    olcrTitle: string;
    olcrBadge: string;
    olcrDesc: string;
    olcrLocalSteps: string[];
    olcrExternalStep: string;
    olcrOutcome: string;
    footnote: string;
  };
  routing: {
    eyebrow: string;
    headline: string;
    lead: string;
    diagramHeading: string;
    diagramNote: string;
    retrievalSplitLabel: string;
    inferenceSplitLabel: string;
    stages: RoutingStageItem[];
  };
  citations: {
    eyebrow: string;
    headline: string;
    lead: string;
    illustrativeLabel: string;
    trustCaveat: string;
    conditionHeading: string;
    conditionBody: string;
    evidencePanelTitle: string;
    selectHint: string;
    scenarios: CitationScenario[];
  };
  capacity: {
    eyebrow: string;
    headline: string;
    lead: string;
    mapTitle: string;
    setupLabel: string;
    roleHeading: string;
    modeHeading: string;
    impactHeading: string;
    tiers: CapacityTier[];
  };
  hybrid: {
    eyebrow: string;
    headline: string;
    lead: string;
    selectorLabel: string;
    illustrativeNotice: string;
    taskExamples: HybridTaskExample[];
    columns: Array<{
      id: 'local' | 'hybrid' | 'connected';
      title: string;
      requirements: string;
      costNote: string;
    }>;
  };
  agentsAndCoding: {
    eyebrow: string;
    headline: string;
    lead: string;
    agentsPanel: {
      title: string;
      subtitle: string;
      disclaimer: string;
      presetLabel: string;
      fieldLabels: {
        name: string;
        role: string;
        instructions: string;
        purposeRole: string;
        retrieval: string;
        workspaceScope: string;
      };
      retrievalOn: string;
      retrievalOff: string;
      previewHeading: string;
      presets: CustomAgentPreset[];
    };
    codingPanel: {
      title: string;
      subtitle: string;
      illustrativeLabel: string;
      controls: {
        pause: string;
        resume: string;
        stop: string;
        stateRunning: string;
        statePaused: string;
        stateStopped: string;
      };
      tabs: {
        plan: string;
        files: string;
        workspace: string;
      };
      planSteps: Array<{
        id: string;
        title: string;
        state: 'done' | 'active' | 'pending';
      }>;
      scopedFiles: Array<{
        path: string;
        action: string;
        note: string;
      }>;
      workspaceOptions: Array<{
        label: string;
        value: string;
      }>;
      followUpLabel: string;
      followUpPlaceholder: string;
      followUpSend: string;
      followUpAck: string;
      reviewNote: string;
    };
  };
  finalCta: {
    headline: string;
    body: string;
    cta: string;
    conditionNote: string;
  };
  footer: {
    wordmarkNote: string;
    releasesLabel: string;
    disclosure: string;
  };
}

export const translations: Record<Lang, TranslationContent> = {
  en: {
    meta: {
      title: 'OLCR — Make every token do more work',
      description:
        'Local-first cognitive runtime and AI orchestration workspace. Route routine work locally, match model capacity to the task, and spend external tokens with intent.',
    },
    nav: {
      links: [
        { href: '#token-spend', label: 'Token Spend' },
        { href: '#routing', label: 'Routing' },
        { href: '#capacity', label: 'Capacity' },
        { href: '#agents-coding', label: 'Agents & Coding' },
      ],
      cta: 'GitHub repo',
      langToggleLabel: 'Language',
    },
    hero: {
      kicker: 'Local-first cognitive runtime · AI orchestration workspace',
      headline: 'Make every token do more work.',
      subheadline:
        'Route routine decisions locally, match the model to the task, and reserve externally billed tokens for work that genuinely benefits from them.',
      primaryCta: 'Explore Releases on GitHub',
      secondaryAnchor: 'See how it works',
      diagramLabel: 'Illustrative flow · not live status',
      diagramStages: {
        request: 'Incoming request',
        localRouting: 'Local understanding & routing',
        localPath: 'Local lightweight & specialist paths',
        externalPath: 'Connected higher-capability / retrieval',
        output: 'Cited or task-ready result',
      },
      diagramNotes: {
        narrowed: 'Avoidable remote calls resolved locally',
        localCompute: 'Uses local hardware & ready model; no external per-call fee',
        reserved: 'External tokens spent only when configured & needed',
      },
      orientationStrip: [
        { index: '01', title: 'Fewer avoidable remote calls' },
        { index: '02', title: 'Task-matched model capacity' },
        { index: '03', title: 'Intentional local + external split' },
        { index: '04', title: 'Inspectable source citations' },
        { index: '05', title: 'Custom agents & reviewable coding' },
      ],
    },
    tokenSpend: {
      eyebrow: '01 · External Token Discipline',
      headline: 'Stop spending premium tokens on routine decisions.',
      lead: 'Instead of sending every intent check and minor transform over a paid external API, OLCR isolates routine local compute so external calls stay focused on demanding work.',
      replayBtn: 'Replay stream',
      diagramCaption: 'Qualitative stream width comparison · not a live counter or pricing estimate',
      legacyTitle: 'One-model-for-everything',
      legacyBadge: 'Broad external spend',
      legacyDesc:
        'Every classification, routing guess, transform, and complex prompt travels out to one large remote model.',
      legacySteps: [
        'Intent & routing decision -> external API',
        'Routine formatting & triage -> external API',
        'Source lookup & synthesis -> external API',
        'Complex reasoning & coding -> external API',
      ],
      legacyOutcome: 'Wide external token stream on every interaction',
      olcrTitle: 'OLCR local-first orchestration',
      olcrBadge: 'Targeted external spend',
      olcrDesc:
        'Routine decisions and lightweight processing stay local where configured, narrowing external calls to high-value tasks.',
      olcrLocalSteps: [
        'Intent & route selection -> local path',
        'Lightweight classification -> local path',
        'Configured retrieval check -> source path',
      ],
      olcrExternalStep: 'Demanding reasoning or connected escalation -> external API (when configured)',
      olcrOutcome: 'Narrowed external stream reserved for complex work',
      footnote:
        'Local inference still processes tokens and consumes your hardware compute, storage, time, and electricity—it avoids per-call external API charges only for steps handled locally with a ready model.',
    },
    routing: {
      eyebrow: '02 · Orchestration Pipeline',
      headline: 'The right model for the task. The right source for the answer.',
      lead: 'Follow the six routing stages to see how OLCR separates triage, retrieval, and model execution.',
      diagramHeading: 'Routing & Retrieval Topology',
      diagramNote: 'Routing chooses an appropriate path; it does not guarantee provider availability or answer correctness.',
      retrievalSplitLabel: 'Evidence path (when retrieval applies)',
      inferenceSplitLabel: 'Inference path (local or connected)',
      stages: [
        {
          id: 'intent',
          index: 'Stage 01',
          title: 'Incoming task & intent',
          detail:
            'Evaluates task shape—routine triage, source lookup, vision, or multi-step coding—before committing external provider tokens.',
          pathLabel: 'Entry · Local triage',
          condition: 'Depends on active runtime configuration.',
        },
        {
          id: 'candidates',
          index: 'Stage 02',
          title: 'Capability & source candidates',
          detail:
            'Checks which local models, purpose roles, retrieval paths, and connected providers are configured and ready.',
          pathLabel: 'Registry · Readiness check',
          condition: 'Reflects configured models, permissions, and credentials.',
        },
        {
          id: 'local-purpose',
          index: 'Stage 03',
          title: 'Local or purpose-based selection',
          detail:
            'Resolves routine classification and lightweight tasks locally without issuing a billed external API request.',
          pathLabel: 'Local path · Zero external call fee',
          condition: 'Requires installed runtime, compatible hardware, and a ready local model.',
        },
        {
          id: 'escalation',
          index: 'Stage 04',
          title: 'Specialist or higher-capability model',
          detail:
            'Escalates complex coding, planning, or multimodal work to an assigned specialist role or connected higher-capability model.',
          pathLabel: 'Escalation · Capability match',
          condition: 'Connected providers require valid credentials, network access, and may incur provider charges.',
        },
        {
          id: 'retrieval',
          index: 'Stage 05',
          title: 'Separate retrieval & provider lookup',
          detail:
            'Runs source retrieval separately from generation when applicable, returning inspectable records rather than relying on model memory.',
          pathLabel: 'Retrieval · Grounded evidence',
          condition: 'Citations appear only when a retrieval or source-providing path returns records.',
        },
        {
          id: 'abstain',
          index: 'Stage 06',
          title: 'Safe abstain, chat, or clarification',
          detail:
            'Falls back to clarification or safe abstention if required models, credentials, or clear task context are unavailable.',
          pathLabel: 'Fallback · Explicit boundary',
          condition: 'Prevents silent fallback assumptions when dependencies are unconfigured.',
        },
      ],
    },
    citations: {
      eyebrow: '03 · Inspectable Provenance',
      headline: 'Answers you can trace back to a source.',
      lead: 'Source-aware routing directs information-seeking queries toward retrieval paths and attaches inspectable citations where supported.',
      illustrativeLabel: 'Illustrative examples · not live results',
      trustCaveat: 'Citations help you inspect where information came from; they do not guarantee that a source or answer is true.',
      conditionHeading: 'When citations appear',
      conditionBody:
        'Citations are attached only when the selected retrieval or connected source path returns records. Ungrounded chat without retrieval produces no citations.',
      evidencePanelTitle: 'Evidence & Source Drawer',
      selectHint: 'Click [1] or [2] to inspect the matched record.',
      scenarios: [
        {
          id: 'local-docs',
          tabLabel: 'Local workspace retrieval',
          modeNote: 'Illustrative · Source-backed retrieval path active',
          query: 'How does the workspace separate routing roles from coding execution?',
          answerSegments: [
            {
              text: 'Lightweight classification tasks can be assigned to a dedicated purpose role rather than defaulting to the main coding model ',
            },
            { text: '[1]', citationIndex: 1 },
            {
              text: '. Coding execution remains scoped to explicit workspace targets and reviewable change steps ',
            },
            { text: '[2]', citationIndex: 2 },
            { text: '.' },
          ],
          sources: [
            {
              index: 1,
              title: 'Purpose-based model assignment notes (example)',
              origin: 'Local documentation index · section: model-roles',
              excerpt:
                'Purpose roles allow lighter local paths to handle routine triage while reserving higher-capability models for complex planning or code edits.',
              condition: 'Requires indexed local source path in active configuration.',
            },
            {
              index: 2,
              title: 'Coding workspace scope guidelines (example)',
              origin: 'Local documentation index · section: coding-review',
              excerpt:
                'Coding tasks present scoped file targets, progress stages, and manual pause/stop/review controls so changes remain inspectable.',
              condition: 'Shown as an illustrative local retrieval record.',
            },
          ],
        },
        {
          id: 'connected-lookup',
          tabLabel: 'Connected source lookup',
          modeNote: 'Illustrative · External retrieval path (requires connection)',
          query: 'What conditions apply before a connected provider handles a request?',
          answerSegments: [
            {
              text: 'External execution requires explicit user configuration, valid credentials, and network access ',
            },
            { text: '[1]', citationIndex: 1 },
            {
              text: ', and may incur provider usage charges according to your external account terms ',
            },
            { text: '[2]', citationIndex: 2 },
            { text: '.' },
          ],
          sources: [
            {
              index: 1,
              title: 'Provider connection & permission checklist (example)',
              origin: 'Configured provider reference · section: credentials',
              excerpt:
                'Connected models and external retrieval providers remain inactive until credentials and permissions are configured by the user.',
              condition: 'Requires active provider credentials and permission.',
            },
            {
              index: 2,
              title: 'External inference billing boundary (example)',
              origin: 'Configured provider reference · section: usage-scope',
              excerpt:
                'Local routing reduces avoidable remote calls, while any task sent to a connected API follows that provider’s billing and data policies.',
              condition: 'Subject to external provider terms and availability.',
            },
          ],
        },
        {
          id: 'direct-chat',
          tabLabel: 'Direct chat (no retrieval)',
          modeNote: 'Illustrative · Ungrounded path (no citations)',
          query: 'Draft a short commit message summarizing a CSS spacing cleanup.',
          answerSegments: [
            {
              text: 'Refine container padding and align section spacing tokens across desktop and mobile breakpoints.',
            },
          ],
          sources: [],
          noCitationNotice:
            'No retrieval path was invoked for this drafting request. OLCR does not fabricate citations when no source record is queried.',
        },
      ],
    },
    capacity: {
      eyebrow: '04 · Task-to-Capability Map',
      headline: 'Scale the capability to the task.',
      lead: 'Select a task category to inspect how OLCR matches required capacity, execution path, and setup conditions.',
      mapTitle: 'Capability & Purpose Assignment',
      setupLabel: 'Setup condition',
      roleHeading: 'Assigned role',
      modeHeading: 'Execution path',
      impactHeading: 'Token impact',
      tiers: [
        {
          id: 'routine',
          index: 'Tier 01',
          taskType: 'Routine classification & lightweight transforms',
          roleLabel: 'Local lightweight model role',
          executionMode: 'Local path (when ready)',
          tokenProfile: 'Avoids external per-call charges',
          summary:
            'Intent triage, short labeling, format normalization, and routing checks resolve close to the user without waking a large remote model.',
          setupCondition: 'Requires a ready compatible local model and local compute resources.',
          activeLevel: 1,
        },
        {
          id: 'lookup',
          index: 'Tier 02',
          taskType: 'Translation or source-backed lookup',
          roleLabel: 'Specialist or retrieval-paired role',
          executionMode: 'Local or configured retrieval',
          tokenProfile: 'Focused context · avoids prompt bloat',
          summary:
            'Pairs targeted retrieval or a specialized role with concise context so answers stay grounded without shipping oversized prompts everywhere.',
          setupCondition: 'Retrieval depends on indexed sources or enabled search/provider paths.',
          activeLevel: 2,
        },
        {
          id: 'vision',
          index: 'Tier 03',
          taskType: 'Image & UI understanding',
          roleLabel: 'Vision-capable model role',
          executionMode: 'Local multimodal or connected',
          tokenProfile: 'Invoked selectively for visual payloads',
          summary:
            'Multimodal capacity runs specifically when screenshots, diagrams, or UI inspections are present rather than on text-only tasks.',
          setupCondition: 'Requires a vision-capable local model or configured external vision provider.',
          activeLevel: 3,
        },
        {
          id: 'coding',
          index: 'Tier 04',
          taskType: 'Coding, planning & complex reasoning',
          roleLabel: 'Higher-capability coding / reasoning role',
          executionMode: 'Connected or high-capacity local',
          tokenProfile: 'Premium capacity reserved for high-leverage work',
          summary:
            'Multi-file planning, architectural reasoning, and code synthesis receive full capability when task complexity justifies the compute or external call.',
          setupCondition: 'Depends on configured coding/reasoning model, hardware capacity, or external API credentials.',
          activeLevel: 4,
        },
      ],
    },
    hybrid: {
      eyebrow: '05 · Local-First & Hybrid Spectrum',
      headline: 'Keep easy work close. Escalate with intent.',
      lead: 'Local-first does not mean offline-only. Select an example task to compare how Local, Hybrid, and Connected paths behave alongside their real tradeoffs.',
      selectorLabel: 'Example task',
      illustrativeNotice: 'Static demonstration · does not inspect local config or call any API',
      taskExamples: [
        {
          id: 'classify',
          label: 'Routine classification',
          promptExample: 'Categorize 20 incoming support notes by topic and urgency.',
          localNote: 'Handled locally by a lightweight role with zero external API call fee.',
          hybridNote: 'Classification stays local; only flagged items needing external lookup escalate if configured.',
          connectedNote: 'Sends every item over a remote API—convenient without a local model, but incurs external billing.',
          recommendedPath: 'local',
        },
        {
          id: 'translate',
          label: 'Translation',
          promptExample: 'Translate release documentation notes between English and Japanese with glossary consistency.',
          localNote: 'Runs locally if a multilingual model is installed; output quality depends on local capacity.',
          hybridNote: 'Local lookup aligns workspace terminology first; nuance-heavy passages escalate to a stronger model.',
          connectedNote: 'Uses a connected provider directly when higher linguistic capacity or external context is needed.',
          recommendedPath: 'hybrid',
        },
        {
          id: 'vision',
          label: 'Image / UI understanding',
          promptExample: 'Inspect a layout screenshot to spot overlapping navigation labels.',
          localNote: 'Requires a compatible local multimodal model loaded in memory; not all setups support vision locally.',
          hybridNote: 'Text pre-checks stay local while the image payload routes to an available vision-capable model.',
          connectedNote: 'Routes to a connected multimodal provider when local hardware cannot run vision models comfortably.',
          recommendedPath: 'connected',
        },
        {
          id: 'coding',
          label: 'Coding & planning',
          promptExample: 'Plan a refactor across three modules and review proposed file diffs.',
          localNote: 'Possible with capable local coding models on sufficient hardware, though deeper refactors may hit limits.',
          hybridNote: 'Local file scoping narrows what gets sent; heavy planning escalates to a higher-capability role.',
          connectedNote: 'Full remote reasoning path—requires network, credentials, permissions, and incurs usage charges.',
          recommendedPath: 'hybrid',
        },
      ],
      columns: [
        {
          id: 'local',
          title: 'Local',
          requirements: 'Requires compatible hardware, runtime, and ready local model.',
          costNote: 'No external per-call fee; consumes local compute, storage, time, and electricity.',
        },
        {
          id: 'hybrid',
          title: 'Hybrid',
          requirements: 'Requires local readiness plus explicit config for connected steps.',
          costNote: 'Balances local routing/triage with targeted externally billed API tokens.',
        },
        {
          id: 'connected',
          title: 'Connected provider',
          requirements: 'Requires external account, credentials, permissions, and network.',
          costNote: 'Incurs provider token charges and transmits request payloads externally.',
        },
      ],
    },
    agentsAndCoding: {
      eyebrow: '06 · Reusable Workflows & Inspectable Execution',
      headline: 'Your agents. Your rules. Your workflow.',
      lead: 'Configure reusable custom-role assistants for recurring tasks, and keep coding work inspectable with explicit file scope, controls, and review.',
      agentsPanel: {
        title: 'Custom Agents',
        subtitle: 'Create reusable assistants with your own role and instructions—familiar in spirit to custom-GPT-style workflows, built for OLCR.',
        disclaimer: 'Independent OLCR feature · not affiliated with or claiming parity with OpenAI products.',
        presetLabel: 'Example preset',
        fieldLabels: {
          name: 'Agent name',
          role: 'Role summary',
          instructions: 'Instructions',
          purposeRole: 'Model role preference',
          retrieval: 'Source retrieval',
          workspaceScope: 'Target scope',
        },
        retrievalOn: 'Enabled when configured',
        retrievalOff: 'Disabled (direct prompt)',
        previewHeading: 'Agent Profile Preview',
        presets: [
          {
            id: 'reviewer',
            label: 'Code Reviewer',
            name: 'Diff & Risk Reviewer',
            role: 'Focuses on regressions, edge cases, and missing checks in scoped code changes.',
            instructions: 'Inspect only the changed files. List concrete risks before style comments. Never invent test results.',
            purposeRole: 'Higher-capability coding role',
            retrievalEnabled: true,
            workspaceScope: 'Scoped repository files',
          },
          {
            id: 'release',
            label: 'Release Notes',
            name: 'Changelog Drafter',
            role: 'Summarizes confirmed changes into clear English and Japanese release notes.',
            instructions: 'Group updates by user impact. Keep claims strictly aligned with provided commit or diff records.',
            purposeRole: 'Local lightweight / specialist role',
            retrievalEnabled: true,
            workspaceScope: 'Release notes & docs',
          },
          {
            id: 'triage',
            label: 'Issue Triage',
            name: 'Routine Note Classifier',
            role: 'Categorizes incoming notes and extracts actionable questions locally.',
            instructions: 'Assign one category and list missing reproduction details without escalating unless requested.',
            purposeRole: 'Local lightweight model role',
            retrievalEnabled: false,
            workspaceScope: 'Text input only',
          },
        ],
      },
      codingPanel: {
        title: 'Coding Orchestrator',
        subtitle: 'Inspectable coding plans, scoped file targets, pause/resume/stop controls, and user diff review.',
        illustrativeLabel: 'Illustrative interface · static mockup, not live execution',
        controls: {
          pause: 'Pause',
          resume: 'Resume',
          stop: 'Stop',
          stateRunning: 'Active step (illustrative)',
          statePaused: 'Paused for review',
          stateStopped: 'Stopped by user',
        },
        tabs: {
          plan: 'Plan & Progress',
          files: 'File Review',
          workspace: 'Workspace Options',
        },
        planSteps: [
          { id: 'p1', title: 'Inspect target module boundaries and existing types', state: 'done' },
          { id: 'p2', title: 'Propose scoped edits across selected component files', state: 'active' },
          { id: 'p3', title: 'Present diff summary for user review and follow-up', state: 'pending' },
        ],
        scopedFiles: [
          { path: 'src/routing/selector.ts', action: 'Modified', note: 'Add capability check before escalation' },
          { path: 'src/routing/selector.test.ts', action: 'Reviewed', note: 'Verify fallback path when provider unconfigured' },
          { path: 'src/ui/routeBadge.tsx', action: 'Unchanged', note: 'Outside current edit scope' },
        ],
        workspaceOptions: [
          { label: 'Write scope', value: 'Explicitly selected files only' },
          { label: 'Execution control', value: 'Pause, resume, or stop at any phase' },
          { label: 'Verification posture', value: 'User reviews diffs and runs checks directly' },
        ],
        followUpLabel: 'Follow-up instruction',
        followUpPlaceholder: 'e.g., Keep the fallback check local-only...',
        followUpSend: 'Queue instruction',
        followUpAck: 'Recorded in preview:',
        reviewNote: 'Tasks do not automatically verify or complete themselves; human review remains required.',
      },
    },
    finalCta: {
      headline: 'More capability. Less wasted inference.',
      body: 'Visit the official OLCR releases page to explore local-first routing, task-matched models, inspectable citations, custom agents, and reviewable coding workflows.',
      cta: 'View Releases on GitHub',
      conditionNote: 'Capabilities depend on your installation, hardware, local models, and configured provider credentials.',
    },
    footer: {
      wordmarkNote: 'OLCR · Local-first cognitive runtime & AI orchestration workspace.',
      releasesLabel: 'GitHub Releases',
      disclosure: 'Static product showcase. Diagrams, citations, and workspace panels are illustrative examples, not live telemetry.',
    },
  },
  ja: {
    meta: {
      title: 'OLCR — 1トークンあたりの仕事を、最大化する',
      description:
        'ローカルファーストの認知ランタイム＆AIオーケストレーションワークスペース。定型処理をローカルに保ち、タスクに最適なモデルを選び、外部APIトークンの浪費を抑えます。',
    },
    nav: {
      links: [
        { href: '#token-spend', label: 'トークン効率' },
        { href: '#routing', label: 'ルーティング' },
        { href: '#capacity', label: 'モデル規模' },
        { href: '#agents-coding', label: 'エージェント' },
      ],
      cta: 'プロジェクトリポジトリ',
      langToggleLabel: '言語切替',
    },
    hero: {
      kicker: 'ローカルファースト認知ランタイム · AIオーケストレーション基盤',
      headline: '1トークンあたりの仕事を、最大化する。',
      subheadline:
        '定型的な判断はローカルで完結させ、タスクの重さに応じてモデルを選び、外部課金トークンは本当に必要な推論だけに使う。',
      primaryCta: 'GitHubでリリースを見る',
      secondaryAnchor: '仕組みを見る',
      diagramLabel: '構成イメージ · ライブ実行状態ではありません',
      diagramStages: {
        request: 'リクエスト入力',
        localRouting: 'ローカルでの意図解釈と経路判定',
        localPath: 'ローカル軽量モデル・特化ロール',
        externalPath: '接続済み高能力モデル / 外部検索',
        output: '出典付き・作業可能な出力',
      },
      diagramNotes: {
        narrowed: '不要な外部呼び出しをローカルで吸収',
        localCompute: '手元の計算資源と準備済みモデルを使用（外部APIの従量課金なし）',
        reserved: '設定と必要性が揃った場合のみ外部トークンを使用',
      },
      orientationStrip: [
        { index: '01', title: '不要な外部呼び出しを削減' },
        { index: '02', title: 'タスクに合う能力選択' },
        { index: '03', title: 'ローカルと外部の意図的な併用' },
        { index: '04', title: '出典を確認できる回答' },
        { index: '05', title: '再利用エージェントとコード統制' },
      ],
    },
    tokenSpend: {
      eyebrow: '01 · 外部トークン消費の最適化',
      headline: '定型処理に、高価なトークンを消費しない。',
      lead: '意図判定や簡単な整形まで毎回外部の大型モデルへ送らず、OLCRは手元で捌ける判断と外部推論を分離し、課金トークンを価値の高い作業へ集中させます。',
      replayBtn: '比較を再生',
      diagramCaption: '定性的なトークン流量のイメージ · 実測値や料金試算ではありません',
      legacyTitle: '単一大型モデルへの全送信',
      legacyBadge: '広範な外部消費',
      legacyDesc:
        '分類、経路判定、簡単な変換、複雑な推論のすべてが1本の太い外部API経路を通過します。',
      legacySteps: [
        '意図判定と振り分け -> 外部API',
        '定型整形とトリアージ -> 外部API',
        '参照検索と要約統合 -> 外部API',
        '複雑な推論・実装 -> 外部API',
      ],
      legacyOutcome: 'すべての対話で太い外部トークン流量が発生',
      olcrTitle: 'OLCR ローカルファースト制御',
      olcrBadge: '必要な箇所のみ外部送信',
      olcrDesc:
        '設定に応じて定型判断や軽量処理をローカル経路で処理し、外部呼び出しを真に恩恵がある作業に絞ります。',
      olcrLocalSteps: [
        '意図判定と経路選択 -> ローカル経路',
        '軽量な分類・整理 -> ローカル経路',
        '設定済みの参照確認 -> ソース経路',
      ],
      olcrExternalStep: '高度な推論や外部連携 -> 外部API（設定・認証済みの場合）',
      olcrOutcome: '高付加価値な作業のためだけに絞られた外部ストリーム',
      footnote:
        'ローカル推論でもトークン処理は行われ、お使いのPCの計算資源・ストレージ・時間・電力を消費します。ローカルで完結する範囲に限り外部プロバイダーへの都度課金が発生しません。',
    },
    routing: {
      eyebrow: '02 · オーケストレーション経路',
      headline: 'タスクに適した経路を。回答に適した根拠を。',
      lead: '6つのステージを通じて、OLCRが経路判定・ソース取得・推論実行をどのように切り分けるかを確認できます。',
      diagramHeading: 'ルーティング＆リトリーバル構成図',
      diagramNote: 'ルーティングは適切な経路を選択するものであり、プロバイダーの応答成功や回答の正しさを保証しません。',
      retrievalSplitLabel: '根拠取得パス（検索対象がある場合）',
      inferenceSplitLabel: '推論パス（ローカルまたは外部接続）',
      stages: [
        {
          id: 'intent',
          index: 'Stage 01',
          title: 'タスクと意図の受け取り',
          detail:
            '単純な分類か、根拠調査か、画像・UI理解か、複数ファイル実装かを最初に見分け、不要な外部送信を防ぎます。',
          pathLabel: '入口 · ローカル初期判定',
          condition: '現在のランタイム設定に依存します。',
        },
        {
          id: 'candidates',
          index: 'Stage 02',
          title: '利用可能な能力とソースの確認',
          detail:
            '現在の設定・権限・認証状態・ローカルモデルの準備状況に基づいて利用可能な候補経路を評価します。',
          pathLabel: '照合 · 準備状態チェック',
          condition: '設定済みモデル・権限・認証情報に依存します。',
        },
        {
          id: 'local-purpose',
          index: 'Stage 03',
          title: 'ローカルまたは用途別モデルの選択',
          detail:
            '対応ローカルモデルとハードウェアがあれば、定型判断や軽量処理は外部API課金を発生させずに完了できます。',
          pathLabel: 'ローカル経路 · 外部課金なし',
          condition: 'ランタイム導入、対応ハードウェア、利用可能なローカルモデルが必要です。',
        },
        {
          id: 'escalation',
          index: 'Stage 04',
          title: '特化ロールまたは高能力モデルへの委譲',
          detail:
            '深い推論やコーディング、視覚理解が求められる場合は、指定された用途ロールや接続済み高能力モデルへ送ります。',
          pathLabel: '昇格 · 能力マッチング',
          condition: '外部プロバイダー利用には認証・通信環境が必要で、利用料が発生する場合があります。',
        },
        {
          id: 'retrieval',
          index: 'Stage 05',
          title: '推論と分離されたソース取得',
          detail:
            '有効な場合、推論とは独立した取得パスが関連ソースを参照し、モデルの記憶だけに頼らない検証可能な記録を返します。',
          pathLabel: '参照 · 根拠データの取得',
          condition: '引用表示は参照記録を返す取得・接続パスが機能した場合のみ表示されます。',
        },
        {
          id: 'abstain',
          index: 'Stage 06',
          title: '安全な保留・通常応答・明確化',
          detail:
            '必要な認証やモデルが不足している場合や意図が曖昧な場合は、確認質問や安全な保留へ切り替えます。',
          pathLabel: '保護 · 明示的な境界',
          condition: '未設定の外部依存を暗黙に補完しません。',
        },
      ],
    },
    citations: {
      eyebrow: '03 · 追跡可能な根拠表示',
      headline: 'ソースまで辿れる、根拠付きの回答。',
      lead: 'ソース指向のルーティングにより、調査型の質問は参照パスへ導かれ、取得結果に応じた引用を確認できます。',
      illustrativeLabel: '表示イメージ · ライブ取得結果ではありません',
      trustCaveat: '引用は参照元を検証するための手がかりであり、内容の真偽を保証するものではありません。',
      conditionHeading: '引用が表示される条件',
      conditionBody:
        '引用は、参照記録を返す取得または接続ソース経路が利用された場合にのみ付与されます。検索を伴わない通常チャットでは引用は生成されません。',
      evidencePanelTitle: '参照ソース・エビデンスパネル',
      selectHint: '[1] または [2] をクリックすると対応する記録を確認できます。',
      scenarios: [
        {
          id: 'local-docs',
          tabLabel: 'ローカル資料の参照',
          modeNote: '表示例 · ソース取得パス有効時',
          query: 'モデルの用途別割り当てとコーディング実行範囲はどう分離されていますか？',
          answerSegments: [
            {
              text: '軽量な分類タスクをメインのコーディングモデルではなく専用の用途ロールへ割り当てることができます',
            },
            { text: '[1]', citationIndex: 1 },
            {
              text: '。またコーディング作業は指定された対象ファイルとレビュー可能な変更ステップに限定されます',
            },
            { text: '[2]', citationIndex: 2 },
            { text: '。' },
          ],
          sources: [
            {
              index: 1,
              title: '用途別モデル割り当て仕様（構成例）',
              origin: 'ローカル資料インデックス · section: model-roles',
              excerpt:
                '用途ロールを設定することで、定型トリアージは軽量なローカル経路に任せ、複雑な計画やコード編集には高能力モデルを温存できます。',
              condition: 'インデックス済みのローカルソース設定が必要です。',
            },
            {
              index: 2,
              title: 'コーディング作業スコープ指針（構成例）',
              origin: 'ローカル資料インデックス · section: coding-review',
              excerpt:
                'コーディングタスクは対象ファイル・進行段階・一時停止/停止/レビュー操作を明示し、ユーザーが変更を点検できる状態を保ちます。',
              condition: 'ローカル参照レコードの例として表示しています。',
            },
          ],
        },
        {
          id: 'connected-lookup',
          tabLabel: '外部ソース参照',
          modeNote: '表示例 · 外部検索経路（接続・権限が必要）',
          query: '接続済みプロバイダーへ処理を委譲する際の前提条件は何ですか？',
          answerSegments: [
            {
              text: '外部実行にはユーザーによる明示的な設定、有効な認証情報、ネットワーク接続が必要であり',
            },
            { text: '[1]', citationIndex: 1 },
            {
              text: '、接続先アカウントの規約に応じた利用料が発生する場合があります',
            },
            { text: '[2]', citationIndex: 2 },
            { text: '。' },
          ],
          sources: [
            {
              index: 1,
              title: 'プロバイダー接続・権限チェックリスト（構成例）',
              origin: 'プロバイダー設定リファレンス · section: credentials',
              excerpt:
                '接続モデルおよび外部検索プロバイダーは、ユーザーが必要な認証情報と権限を設定するまで有効化されません。',
              condition: '有効なプロバイダー認証情報と権限が必要です。',
            },
            {
              index: 2,
              title: '外部推論の課金境界について（構成例）',
              origin: 'プロバイダー設定リファレンス · section: usage-scope',
              excerpt:
                'ローカル経路は不要な外部呼び出しを減らしますが、外部APIへ送られた処理は各プロバイダーの料金体系とデータ規約に従います。',
              condition: '外部プロバイダーの提供状況・規約に依存します。',
            },
          ],
        },
        {
          id: 'direct-chat',
          tabLabel: '通常チャット（検索なし）',
          modeNote: '表示例 · 非検索パス（引用なし）',
          query: 'CSSの余白整理に関する短いコミットメッセージ案を作成してください。',
          answerSegments: [
            {
              text: 'デスクトップとモバイルのブレークポイント間でコンテナ余白とセクション間隔トークンを統一。',
            },
          ],
          sources: [],
          noCitationNotice:
            'この下書き作成ではソース取得パスが使われていないため、引用は表示されません。OLCRは参照していない出典を捏造しません。',
        },
      ],
    },
    capacity: {
      eyebrow: '04 · タスク別能力マップ',
      headline: 'タスクの重さに、モデルの器量を合わせる。',
      lead: 'タスク種別を選択して、割り当てられる能力ロール・実行経路・前提条件を確認できます。',
      mapTitle: 'タスク対能力・用途ロール対応マップ',
      setupLabel: '前提条件',
      roleHeading: '割り当てロール',
      modeHeading: '実行経路',
      impactHeading: 'トークン影響',
      tiers: [
        {
          id: 'routine',
          index: 'Tier 01',
          taskType: '定型分類と軽量なテキスト変換',
          roleLabel: 'ローカル軽量モデルロール',
          executionMode: 'ローカル経路（準備済み時）',
          tokenProfile: '外部の都度課金を回避',
          summary:
            '意図の切り分け、ラベル付け、書式整形、ルーティング判定などを手元で処理し、大型の外部モデルを起こしません。',
          setupCondition: '互換性のあるローカルモデルとローカル計算資源が必要です。',
          activeLevel: 1,
        },
        {
          id: 'lookup',
          index: 'Tier 02',
          taskType: '翻訳・根拠付き参照検索',
          roleLabel: '特化ロール / 参照連携ロール',
          executionMode: 'ローカルまたは設定済み参照',
          tokenProfile: '必要文脈のみ抽出・送信肥大を抑制',
          summary:
            '対象ソースからの抜粋や用途特化モデルを組み合わせ、巨大な文脈を丸ごと投げずに根拠ある応答を作ります。',
          setupCondition: 'ローカル索引または設定済みの外部検索経路に依存します。',
          activeLevel: 2,
        },
        {
          id: 'vision',
          index: 'Tier 03',
          taskType: '画像・UI画面の理解',
          roleLabel: '視覚対応（Vision）モデルロール',
          executionMode: 'ローカルマルチモーダル / 接続',
          tokenProfile: '画像を含む要求のみに限定呼出',
          summary:
            'スクリーンショットやUI確認が必要な場面だけに視覚対応モデルを使い、テキスト中心の処理には割り当てません。',
          setupCondition: 'ローカルの視覚対応モデル、または外部Visionプロバイダー設定が必要です。',
          activeLevel: 3,
        },
        {
          id: 'coding',
          index: 'Tier 04',
          taskType: 'コーディング・設計・複雑な推論',
          roleLabel: '高能力コーディング / 推論ロール',
          executionMode: '接続プロバイダー / 大容量ローカル',
          tokenProfile: '投資対効果の高い作業へ集中的に配分',
          summary:
            '複数ファイルにまたがる設計、リファクタリング計画、コード生成など、投資対効果の高い作業に上位能力を充てます。',
          setupCondition: '対応モデル・ハードウェア容量、または外部API認証の設定に依存します。',
          activeLevel: 4,
        },
      ],
    },
    hybrid: {
      eyebrow: '05 · ローカルファーストとハイブリッドの境界',
      headline: '軽い処理は手元で。必要なときだけ外部へ。',
      lead: 'ローカルファーストは完全オフライン専用を意味しません。タスク例を選び、各実行方式の振る舞いと前提条件を並べて確認できます。',
      selectorLabel: 'タスク例',
      illustrativeNotice: '静的デモ · ローカル設定参照やAPI通信は行いません',
      taskExamples: [
        {
          id: 'classify',
          label: '定型分類',
          promptExample: '20件の問い合わせメモをトピックと緊急度で分類する。',
          localNote: '手元の軽量ロールで処理し、外部APIの従量課金は発生しません。',
          hybridNote: '分類自体はローカルで完結し、要確認項目だけを必要時に外部参照へ回せます。',
          connectedNote: '全件をリモートAPIへ送信します。ローカル未導入時は便利ですが外部課金が発生します。',
          recommendedPath: 'local',
        },
        {
          id: 'translate',
          label: '翻訳',
          promptExample: 'リリースノートを用語集の一貫性を保ちつつ日英翻訳する。',
          localNote: '多言語ローカルモデルがあれば手元で実行可能ですが、品質はモデル規模に依存します。',
          hybridNote: '手元の用語参照を先に行い、細かなニュアンス調整が必要な箇所のみ上位モデルへ渡せます。',
          connectedNote: '高度な表現力や外部コンテキストが必要な場合は接続プロバイダーを直接利用します。',
          recommendedPath: 'hybrid',
        },
        {
          id: 'vision',
          label: '画像 / UI理解',
          promptExample: '画面スクリーンショットからナビゲーション要素の重なりを確認する。',
          localNote: 'ローカルで視覚対応モデルが稼働可能である必要があり、すべての端末で動作するわけではありません。',
          hybridNote: '周辺のテキスト判定はローカルに留め、画像解析部分のみ対応モデルへルーティングします。',
          connectedNote: '手元のハードウェア負荷を避けたい場合、接続済みマルチモーダルプロバイダーを利用します。',
          recommendedPath: 'connected',
        },
        {
          id: 'coding',
          label: 'コーディング＆計画',
          promptExample: '3つのモジュールにまたがるリファクタリング計画と変更差分レビューを作成する。',
          localNote: '十分な性能の機材とコーディング向けローカルモデルがあれば実行できますが、大規模変更では限界が出る場合があります。',
          hybridNote: 'ローカルで対象範囲と関連ファイルを絞り込み、設計と実装案だけを高能力ロールへ送ります。',
          connectedNote: 'フルリモート推論経路。ネットワーク・認証・権限が必要となり、外部利用料が発生します。',
          recommendedPath: 'hybrid',
        },
      ],
      columns: [
        {
          id: 'local',
          title: 'ローカル',
          requirements: '対応ハードウェア、導入済みランタイム、互換モデルが必要です。',
          costNote: '外部都度課金なし。計算時間・メモリ・ストレージ・電力を手元で消費します。',
        },
        {
          id: 'hybrid',
          title: 'ハイブリッド',
          requirements: 'ローカル実行環境と、接続先プロバイダーの明示設定が必要です。',
          costNote: '手元の経路判定と、必要な箇所だけの外部課金トークンを両立します。',
        },
        {
          id: 'connected',
          title: '接続プロバイダー',
          requirements: '外部アカウント、認証情報、権限、ネットワーク接続が必要です。',
          costNote: '外部APIトークン課金が発生し、処理内容がプロバイダーへ送信されます。',
        },
      ],
    },
    agentsAndCoding: {
      eyebrow: '06 · 再利用エージェントと検証可能なコード作業',
      headline: '自分のルールで動くエージェントと、見通せるコード実装。',
      lead: '役割と指示を持つ再利用エージェントを定義し、コーディング作業は計画・対象範囲・変更差分を手元でコントロールできます。',
      agentsPanel: {
        title: 'カスタムエージェント',
        subtitle: '自分の役割や指示文に合わせて再利用可能なアシスタントを構築。日々のワークフローにOLCRの経路制御を組み合わせられます。',
        disclaimer: 'OLCR独自の機能であり、OpenAI製品との提携や同一性を主張するものではありません。',
        presetLabel: '構成例',
        fieldLabels: {
          name: 'エージェント名',
          role: '役割の概要',
          instructions: '指示（インストラクション）',
          purposeRole: '優先モデルロール',
          retrieval: 'ソース参照',
          workspaceScope: '対象スコープ',
        },
        retrievalOn: 'ソース設定時に有効',
        retrievalOff: '無効（直接指示のみ）',
        previewHeading: 'エージェント構成プレビュー',
        presets: [
          {
            id: 'reviewer',
            label: 'コードレビュー',
            name: '差分・リスク点検エージェント',
            role: '変更対象コードにおける回帰リスク、境界条件、検証漏れを重点的に点検します。',
            instructions: '指定された変更ファイルのみを精査し、スタイルの好みより先に具体的な不具合リスクを列挙すること。未確認のテスト結果を推測しない。',
            purposeRole: '高能力コーディングロール',
            retrievalEnabled: true,
            workspaceScope: '指定されたリポジトリファイル',
          },
          {
            id: 'release',
            label: 'リリースノート',
            name: '変更サマリー作成エージェント',
            role: '確認済みの変更内容を分かりやすい日英リリースノートに整理します。',
            instructions: 'ユーザーへの影響単位で整理し、提供されたコミット記録・差分に基づく事実のみを記述すること。',
            purposeRole: 'ローカル軽量 / 特化ロール',
            retrievalEnabled: true,
            workspaceScope: 'リリースノート・ドキュメント',
          },
          {
            id: 'triage',
            label: '課題トリアージ',
            name: '定型メモ分類エージェント',
            role: '受け取ったメモをローカルで分類し、不足している確認事項を抽出します。',
            instructions: 'カテゴリを1つ選び、再現条件で不足している項目を箇条書きにする。指示がない限り外部モデルへ昇格しない。',
            purposeRole: 'ローカル軽量モデルロール',
            retrievalEnabled: false,
            workspaceScope: 'テキスト入力のみ',
          },
        ],
      },
      codingPanel: {
        title: 'コーディング・オーケストレーター',
        subtitle: '計画・対象スコープ・一時停止/再開/停止・追加指示・ファイルレビューを可視化。',
        illustrativeLabel: '構成イメージ · 静的モックアップでありライブ実行ではありません',
        controls: {
          pause: '一時停止',
          resume: '再開',
          stop: '停止',
          stateRunning: '進行中（イメージ）',
          statePaused: 'レビューのため一時停止中',
          stateStopped: 'ユーザー操作により停止',
        },
        tabs: {
          plan: '計画と進行',
          files: 'ファイルレビュー',
          workspace: 'ワークスペース設定',
        },
        planSteps: [
          { id: 'p1', title: '対象モジュールの境界と既存型定義を確認', state: 'done' },
          { id: 'p2', title: 'スコープ内コンポーネントへの編集案を作成', state: 'active' },
          { id: 'p3', title: '変更差分サマリーを提示し、ユーザーの確認と追加指示を待つ', state: 'pending' },
        ],
        scopedFiles: [
          { path: 'src/routing/selector.ts', action: '変更予定', note: '上位経路への昇格前に能力チェックを追加' },
          { path: 'src/routing/selector.test.ts', action: '確認対象', note: '外部未設定時のフォールバック経路を検証' },
          { path: 'src/ui/routeBadge.tsx', action: '対象外', note: '今回の編集スコープには含めません' },
        ],
        workspaceOptions: [
          { label: '書き込み範囲', value: '明示的に選択されたファイルのみ' },
          { label: '実行制御', value: '任意の段階で一時停止・再開・停止が可能' },
          { label: '検証方針', value: '変更差分とチェック実行はユーザーが直接確認' },
        ],
        followUpLabel: '追加指示',
        followUpPlaceholder: '例：フォールバック判定はローカル完結にして…',
        followUpSend: '指示を追加',
        followUpAck: 'プレビューに記録:',
        reviewNote: 'すべてのコーディング作業が自動検証・自動完了するわけではありません。最終確認はユーザーが行います。',
      },
    },
    finalCta: {
      headline: 'より高い能力を、より少ない無駄な推論で。',
      body: 'OLCRの公式リリースページから、ローカルファーストのルーティング、タスク別モデル選定、根拠付き回答、カスタムエージェントの構成をご確認ください。',
      cta: 'GitHubリリースページへ',
      conditionNote: '各機能の利用可否は、導入環境・ハードウェア・利用可能なローカルモデルおよび接続プロバイダー設定に依存します。',
    },
    footer: {
      wordmarkNote: 'OLCR · ローカルファースト認知ランタイム＆AIオーケストレーションワークスペース',
      releasesLabel: 'GitHub Releases',
      disclosure: '静的マーケティングページです。画面内の図解・引用例・ワークスペース表示は構成イメージであり、実環境のライブ状態ではありません。',
    },
  },
};
