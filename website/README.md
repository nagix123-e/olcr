# OLCR — Make every token do more work

This directory contains the OLCR product website. It is a static Vite build and does not call an external API.

## Run locally

```sh
npm ci
npm run dev
```

## Build for GitHub Pages

The workflow builds this site with `GITHUB_PAGES=true`, which sets Vite's project-site base path to `/olcr/`, then deploys `dist/` from the repository's `main` branch.

After the workflow is merged, set **Settings → Pages → Build and deployment → Source** to **GitHub Actions** once. The site will be available at <https://nagix123-e.github.io/olcr/> after a successful deployment.

To build the Pages version locally:

```sh
GITHUB_PAGES=true npm run build
```
