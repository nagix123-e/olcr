# Chat location maps

Use the composer’s `+` → `地図・地点を指定` to open a Leaflet map using OpenStreetMap tiles. Click to place A, then B (optional). Choose A/B to replace a pin; remove a pin to return to a single-place task. Place labels are editable. Coordinate/name entry supports keyboard use and geocoder outages. Expand/restore and Escape preserve the same map instance and its viewport. Maps are saved as `location_map` message blocks and appear in conversation history.

Example: `ピンの落ちている二点における人口密度を比較して`.

An empty map opens with one world centered and uses the available map height to bring it closer; world tiles do not repeat horizontally. Saved maps initially frame their existing pins. Zoom uses fractional steps, animated tiles and markers, and drag inertia (respecting reduced motion). Editing a place label or choosing a visible point preserves the current view; fullscreen resize work is coalesced into one update per frame. A/B use teardrop SVG pins with their bottom tip anchored at the exact selected coordinate.

The typed input contains one or two finite coordinates and nonblank place names. Names and pin identifiers ground the existing embedding/API router, Laya source selection and Brain analysis recipe. The provider compiler receives the original typed coordinates separately, so the model cannot relocate a pin. Evidence retrieval runs separately for each pin and labels packets A/B before combined synthesis; road routing is one joint A→B request. Provider failures retain the selected source, pin and model trace and explicitly report missing evidence. External access settings continue to apply. A subsequent prompt mentioning pins, the map or locations reuses the latest map in that conversation.

Map requests retain local geographic scope instead of the population heuristic narrowing them to national World Bank data. Planner/synthesis instructions require administrative area, source, year, definitions and units; a coordinate itself has no population density. Missing or incompatible local data must be reported. These checks guide evidence use; they do not guarantee that every locality has available statistics.

`map_analysis.py` constrains map candidates to capable sources before Laya/Brain planning. It supports named-place Wikipedia explanations, Open-Meteo forecasts, local-time sunrise/daylight, GSI ground elevation, USGS radius/time/magnitude catalogs, nearby facilities, OSRM road routes and the existing Japanese municipal population-density adapter. Quantitative results use deterministic arithmetic. For explanations, Brain selects source passage IDs separately for each requested viewpoint and pin; the renderer displays the actual source text with citations. Invalid IDs, the other pin's evidence and omitted viewpoints are rejected, with one bounded correction. This avoids the unsupported facts observed when the local model freely rewrote article prose. These are extractive answers, and their relevance still needs evaluation.

Wikipedia retrieval uses the official REST summary and full-article HTML endpoints. Evidence is balanced by viewpoint before the context limit, excluding image captions, references and navigation. Explicit contradictions in dates for the same named enactment are excluded. Requested sights retrieve linked articles, with distinct sources per sight. Climate comparisons read monthly temperature, precipitation and snowfall cells with their stated normal period; comparisons require the same period for all places. The response identifies the normal period, observation point and seasonal aggregation, rather than treating a short forecast as climate.

Weather resolves tomorrow and multi-day windows against the provider's local date. Earthquake requests use the original pin, radius and one shared UTC time window for both points; a verified empty catalog is zero, not a retrieval failure. Sunrise uses the provider's actual local timezone. Nearby facilities are filtered by distance; OSM queries use bounded form POST requests. When public Overpass endpoints fail, linked Wikipedia facilities with coordinates provide a bounded discovery fallback; the response names its actual sources. Parked domains are rejected. Public provider availability can still prevent a task from completing.

The existing `geo.poi` provider was restored after moving bounded Overpass queries to a working public endpoint and the documented form POST method. The production catalog contains 89 providers; the historical 102-case fixture is unchanged. The endpoints are listed in [OSM's public-instance documentation](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances). Current API parameter references: [Open-Meteo](https://open-meteo.com/en/docs), [USGS](https://earthquake.usgs.gov/fdsnws/event/1/), [Sunrise-Sunset](https://sunrise-sunset.org/api).

## Services

Leaflet 1.9.4 is bundled locally. Only the tile image origin `https://tile.openstreetmap.org` is added to the desktop image CSP. Attribution remains visible. Tile loading follows Leaflet's ordinary viewport requests and browser cache; there is no prefetch/offline download.

Reverse lookup uses OSM-based [Photon](https://github.com/komoot/photon), not the public Nominatim service. Requests go through the backend, at most one per second, with a bounded response, timeout, application User-Agent and 512-entry in-process cache. The public Photon demo has no availability guarantee and requires reasonable traffic. Set `OLCR_MAP_GEOCODER_URL` to a compatible Photon `/reverse` endpoint for a private/service deployment; the endpoint is operator-controlled, not accepted from chat. Failed lookup leaves existing pins intact. Place names are approximate and should be checked before analysis. Tiles and lookup require network access; manual labels/coordinates remain usable without lookup.

## Verification

[修正後の実E2E結果](verification/2026-10-26-map-general-fixed-e2e.md): 25ケースの実モデル・実公開API試験と修正箇所の再試験を実施。経路・根拠と質問別の内容を確認した。説明は出典付き抜粋形式、数値は取得値から計算する。人口密度の公式統計APIも別枠で再検証した（2026-10-27整理）。

- `npm --prefix frontend run build`
- `npm --prefix frontend test -- src/conceptDiagram.test.tsx src/ComposerActions.test.tsx src/ConversationIsolation.test.tsx`
- `OLCR_DB_PATH=/private/tmp/olcr-map-tests.sqlite PYTHONPATH=backend:backend/tests .venv312/bin/python -m unittest discover -s backend/tests -p test_chat_maps.py -q`
- `.venv312/bin/python scripts/chat_map_general_live_e2e.py`: isolated headless live `/api/chat` run, actual configured test models and public providers, all 25 cases. `OLCR_MAP_CASE_IDS=MAP-G02,MAP-G12` selects a subset.
- `.venv312/bin/python scripts/review_chat_map_live_e2e.py /private/tmp/<live-profile> --output /private/tmp/map-evidence-review.json`: independent provider, coordinate, period, unit, arithmetic and quotation checks on the recorded evidence. This does not replace task-specific content review.
- Start Vite with `VITE_OLCR_API=http://127.0.0.1:5197/__map_api npm --prefix frontend run dev -- --port 5197`, then `node scripts/chat_map_browser_smoke.cjs`. Set `OLCR_PLAYWRIGHT_MODULE` if Playwright is in a prepared runtime rather than `mcp-runtime/node/node_modules/playwright`. This smoke uses mocked API/geocoder/tiles; no live model or external statistical API is exercised.

## Real tile verification (2026-10-26)

The original smoke screenshot used transparent mock tiles and did not prove that a basemap rendered. `OLCR_MAP_LIVE_TILES=1` now disables the tile mock, checks loaded 256px OSM images, enters real Tokyo/Osaka coordinates, and captures inline/fullscreen maps. API/model responses remain isolated fixtures; this verifies real basemap rendering, not live statistical analysis. `OLCR_MAP_FAIL_TILES=1` verifies the visible tile failure message and retry recovery without external calls.

Real tile testing exposed a deferred Leaflet zoom callback after a draft map was removed. The 2026-10-27 interaction update enables user zoom animations, disables repeated world tiles, fits an unpinned world to the available height, and clears Leaflet 1.9's pending zoom flag on removal, while initial pin fitting remains synchronous. Fullscreen keeps the same map instance; resize preserves the geographic center. Failed tile loads show a retry action rather than silently leaving an empty map.

## 実統計APIによる検証

[2026-10-26の実データ検証記録](verification/2026-10-26-map-statistics-live.md)で、千代田区と大阪市の人口密度比較を、実Embedding・Laya・ブレインと公式統計APIを通した `/api/chat` で確認した。市区町村の総人口・総面積は登録不要の統計ダッシュボードAPIを既存e-Statアダプター経由で取得する。国勢調査人口と同年総面積が揃う最新共通年を使い、面積単位、行政区域、常住人口の定義と出典を回答に明記する。Layaが棄権した場合の既存のドメイン候補回復も記録している。

## Map interaction verification (2026-10-27)

`npm --prefix frontend test -- src/ChatMap.test.tsx src/ChatComposer.test.tsx`: 55 tests passed. The map tests use real Leaflet in jsdom and cover the initial world bounds, label edits without recentering or marker replacement, click coordinates and pin anchor, viewport continuity through fullscreen, coalesced resize and closing during a pending WebKit zoom completion. `npm --prefix frontend run build` passed.

UI polish changes:

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| MEDIUM | `frontend/src/ChatMap.tsx` | Zoom transitions disabled; every label edit refitted the viewport | Animated fractional zoom; label changes keep markers and viewport | Interruptible motion; avoid disruptive movement during editing |
| MEDIUM | `frontend/src/ChatMap.tsx`, `frontend/src/chat-map.css` | Asymmetric rounded badge did not visibly point at its anchor | Teardrop SVG with its stroked tip at the coordinate anchor | Optical alignment; make the selected location unambiguous |
| MEDIUM | `frontend/src/ChatMap.tsx` | The world repeated across wide viewports and stayed at the smallest zoom after resizing | A single world tile set; unpinned world zoom adjusts to the available canvas height | Keep the whole world visible while using the available area |
| LOW | `frontend/src/ComposerActions.tsx`, `frontend/src/UiIcon.tsx` | Map and concept diagram used the same branching icon | Map action uses a folded map and location pin | Make the menu action visually match its function |
| MEDIUM | `frontend/src/ChatMap.tsx` | Every resize callback updated the map immediately | At most one update per animation frame | Avoid redundant layout work during fullscreen changes |

Hover, keyboard focus, disabled, loading/error and empty states, reduced-motion handling and transition properties were inspected in source. Not verified: native WebKit frame rate, trackpad feel, real-tile interaction and the new pin's visual rendering on the user's running app. The already running desktop bundle has not been rebuilt or relaunched for this change.

Approve for the inspected source and automated coverage; native visual and motion verification remains pending.
