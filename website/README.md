# OLCR — Make every token do more work

This directory contains the OLCR product website. It is a static Vite build and does not call an external API.

## Run locally

```sh
npm ci
npm run dev
```

## Build for GitHub Pages

The workflow builds this site with `GITHUB_PAGES=true`, which sets Vite's project-site base path to `/olcr/`, then deploys `dist/` from the repository's `main` branch.

The repository is configured to publish through GitHub Actions. Merging site changes to `main` runs the build and deploy workflow; after a successful deployment, the site is available at <https://nagix123-e.github.io/olcr/>.

To build the Pages version locally:

```sh
GITHUB_PAGES=true npm run build
```
