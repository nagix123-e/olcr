# Task 3 report — constrained adapter contract and GitHub Releases

## Result

Implemented the shared release adapter protocol, bounded command runner, typed provider API transport, and GitHub Releases adapter. The adapter requires explicit intent, External Access, current verified publishing and terms evidence, a resolved GitHub repository/version/channel, a single matching push destination, CLI authentication, an existing Git tag, and workspace-confined release artifacts.

GitHub remote resolution reuses `GitHubReview.repo()` and the existing `GitWorkspace` safe Git invocation. Release creation uses the separate fixed-argv runner with `gh release create --verify-tag`. It checks the tag's resolved commit against the validated local `build_commit`, repeats that check after a missing-release response and immediately before creation, then checks again around release and asset read-back. Artifacts are opened descriptor-relatively without following symlinks, hashed with read-size caps, staged under their original asset filenames, and confirmed by exact SHA-256 digest through the GitHub API. Unclear outcomes remain `UNCERTAIN` and require inspection before retry. External Access off stops before CLI or API calls.

The adapter subpackage is listed in setuptools packages so the source-tested implementation is present in built distributions.

## Verification

Before `github.py` existed, the focused suite failed during collection because `olcr_api.release_adapters.github` could not be imported. After implementation and the release-asset filename regression test:

```text
PYTHONPATH=backend "/Users/nagimachino/Documents/ChatGPT/lightweight local llm/olcr/.venv312/bin/pytest" -q backend/tests/test_release_adapter_contract.py backend/tests/test_github_release_adapter.py
30 passed in 0.06s
```

Python byte-compilation of the adapter package passed. `git diff --check` passed. No live GitHub API, GitHub CLI login, or release operation was used.

## Official behavior checked

- The [GitHub CLI `gh release create` manual](https://cli.github.com/manual/gh_release_create) documents that a missing tag may be created automatically unless `--verify-tag` is supplied. The adapter therefore requires an existing remote tag and passes `--verify-tag`.
- GitHub's [Get a reference](https://docs.github.com/en/rest/git/refs) endpoint returns the tag reference object; [Get a tag object](https://docs.github.com/en/rest/git/tags) provides the target object type and SHA needed to resolve annotated tags to a commit.
- The [GitHub REST release assets API](https://docs.github.com/en/rest/releases/assets) exposes an asset `digest` such as `sha256:<hex>`. The adapter requires read-back of each requested asset's exact digest before reporting `PUBLISHED`.
- The selected `2022-11-28` REST API version remains supported through March 10, 2028 according to GitHub's [API versioning policy](https://docs.github.com/en/rest/about-the-rest-api/api-versions).

## Review

Two independent reviews found and verified fixes for these issues:

- GitHub API reads reuse the configured CLI credential provider, so private repository reads can authenticate. The API transport adds the token only to its Authorization header and redacts it from responses.
- Subprocess stdout/stderr are drained through fixed-size in-memory buffers; oversized output is discarded instead of consuming temporary disk.
- The current source commit and lightweight/annotated remote tag commit must match. Read-back repeats the comparison before `NOT_FOUND`, immediately before creation, and after reading release assets. Temporary API failures remain `UNCERTAIN`, not a tag conflict.
- Artifact files are opened beneath the project root using descriptor-relative `O_NOFOLLOW` access. Hashing and staging enforce per-file and combined size limits.

Both reviewers confirmed the findings addressed and reported no remaining actionable issues. Tests cover moved tags, duplicate asset names, preserved asset filenames, non-boolean authorization and symlink artifact paths.
