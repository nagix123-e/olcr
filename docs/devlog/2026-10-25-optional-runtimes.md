# Optional runtime delivery and single-checkpoint Laya

- Laya loads only the multilingual checkpoint for all input languages, with a
  one-model cache. Upstream Router's implicit model defaults are removed.
  Legacy English settings remain readable but never select/download that model.
- Removed English weights from the three confirmed local locations:
  `models/laya/english`, `models/laya/hub/models--convaiinnovations--laya`, and
  `.cache/laya/models/english`. Regular-file logical bytes removed: 2,538,589,301.
- Verified fresh downloads of Laya, the agent engine, Node tools and browser.
  Promoted those installs into the user's OLCR `components/` directory and
  removed the duplicate generated engines and multilingual checkpoints from the
  source checkout. Temporary test downloads/npm caches were removed.
- Core release staging includes download recipes and a small model catalog;
  it does not stage the four optional payloads. The installer accepts a core
  archive without model/Node directories. The existing main Python environment
  and its dependencies remain core requirements.
- Laya prepares asynchronously at initial API startup. Agent/Node/browser engines
  prepare on first actual use. Settings offers preparation/retry, while read-only
  inventories never download. Chat displays setup progress/failures.

Verification:

- Clean upstream downloads with checksums and npm lock integrity; no host Node,
  compiler, npm install scripts, Docker or user model API required.
- Real LibreChat runner roundtrip with a local fake model reply and SQLite
  checkpoint; real shadcn/Anime.js MCP initialize + tools/list.
- Real Japanese and English weather-selection inference on the same loaded Laya
  checkpoint, and a headless browser rendering an in-memory local page.
- 70 backend tests across component setup/API, Laya, packaging/installer, Node
  resolution, agents, purpose models, routing and MCP connections; 17 frontend
  tests. TypeScript checking, frontend production build and scoped diff checks.
- A fixture core archive was actually assembled and inspected to exclude the
  optional payloads. No new public release was published by this work.
- The managed development app rebuilt automatically. After the required Keychain
  restoration completed, its exact app-owned backend passed `/api/health` and
  reported Laya ready, English checkpoint unavailable, and only the multilingual
  revision. No CUA or user-project/model API interaction bypassed restoration.

## Requested recheck

Repeated the 70 backend and 17 frontend tests plus TypeScript checking: all
passed. Repeated offline reuse of all four installed components with the download
function prohibited, managed-path resolution, real agent execution/SQLite with a
local simulated model reply, both MCP initialize/tools-list probes, and browser
rendering plus a button click on an in-memory local page: all passed.

Real Laya inference selected the expected weather/calculator candidate in four
Japanese/English cases. The router retained only the multilingual model entry and
one loaded checkpoint; the three retired English weight paths remained absent.
The live app PID 71866 owned backend PID 72082 on loopback port 58339, whose health
reported OK, Laya ready, English unavailable and only the multilingual revision.
These PIDs identify this check only; future checks must rediscover ownership.
No GUI acceptance run or external model API call was used. No implementation
change was required by this recheck.
