# Task 7 report — Vercel and Cloudflare Pages

## Result

Implemented `VercelReleaseAdapter` and `CloudflarePagesReleaseAdapter` against the shared release contract. Both bind requests to existing provider projects, verify local build artifacts and commit identity, separate preview from production operations, inspect remote state after uncertain outcomes, and require provider read-back before reporting success.

Vercel uses exact project/scope selection, preview deployment, staged production deployment, and exact-ID promotion as distinct operations. Production promotion checks the authorized complete custom-domain set and confirms the final alias/read-back state. A preview-filtered deployment query accommodates Vercel's optional `target` response field without treating an unfiltered/unknown target as preview.

Cloudflare Pages deploys through the existing project's Direct Upload REST API. It uses bounded asset batches, official BLAKE3 asset hashes, upload-token/check-missing/upload/upsert/deploy calls, and remote read-back. It does not invoke Wrangler's first-use project-creating behavior or modify project settings. Known Pages control files and Functions inputs that this static Direct Upload path cannot preserve are blocked during preflight. Production requires the configured production branch and an explicit exact authorization for every custom domain plus the generated `*.pages.dev` host; the project is re-read immediately before deployment to catch a changed branch or domain set.

Added `blake3==1.0.11` to runtime requirements and synchronized `uv.lock`, since Cloudflare's official asset hashing algorithm must be reproduced exactly. The temporary wheel used for verification lives outside the repository.

## Verification

Focused web, shared contract, prior provider-adapter, and coding workflow regression suites:

```text
164 passed, 1 skipped in 0.49s
```

The skipped test requires BLAKE3 to be absent from the runtime path; it was also run separately without the temporary BLAKE3 wheel and passed. Python byte-compilation, `git diff --check`, and `UV_CACHE_DIR=/private/tmp/olcr-uv-cache uv lock --check` passed. Tests use fake runners and API transports; no provider account was contacted and no deployment was made.

## Review

An independent read-only review found and the implementation addressed these gaps: Pages control/Functions files are rejected before upload; production authorization covers all custom domains; the project is re-read after upload and before deployment; Vercel preview list queries are explicitly filtered; and Cloudflare's generated `pages.dev` subdomain is treated separately from custom `domains` and included in production authorization. Final reviewer result: no remaining P1/P2 findings.

## Official behavior checked

- [Vercel list deployments](https://vercel.com/docs/rest-api/deployments/list-deployments) and [get deployment](https://vercel.com/docs/rest-api/deployments/get-a-deployment-by-id-or-url)
- [Vercel CLI global options](https://vercel.com/docs/cli/global-options) and [promote](https://vercel.com/docs/cli/promote)
- [Cloudflare Pages create deployment](https://developers.cloudflare.com/api/resources/pages/subresources/projects/subresources/deployments/methods/create/), [check missing assets](https://developers.cloudflare.com/api/resources/pages/subresources/assets/methods/check_missing/), [upload assets](https://developers.cloudflare.com/api/resources/pages/subresources/assets/methods/upload/), and [upsert hashes](https://developers.cloudflare.com/api/resources/pages/subresources/assets/methods/upsert_hashes/)
- [Cloudflare Wrangler asset hash implementation](https://github.com/cloudflare/workers-sdk/blob/main/packages/deploy-helpers/src/deploy/helpers/hash.ts), [Pages validator ignore rules](https://github.com/cloudflare/workers-sdk/blob/main/packages/wrangler/src/pages/validate.ts), [Pages project API](https://developers.cloudflare.com/api/resources/pages/subresources/projects/methods/get/), and [custom domain behavior](https://developers.cloudflare.com/pages/configuration/custom-domains/)
