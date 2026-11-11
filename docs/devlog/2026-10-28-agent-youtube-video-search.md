# Agent-owned YouTube video search

Explicit requests such as “YouTubeでPythonの使い方動画を検索して” and “Pythonの解説動画を探して” route to the native YouTube search operation. Previously the Japanese “検索” expression was not fully recognized, and words such as “使い方” could incorrectly divert a video-search request into ordinary chat/Web routing. Frontend followup classification now agrees with the backend; coding implementation requests and local-file video searches remain outside this route.

The assistant retrieves actual video candidates through the existing key-free public YouTube search. Search-only requests mark their media block `autoplay=false`, so candidates are presented for selection without starting playback. An explicit “探して再生して” still permits playback. Query extraction removes platform/action wording while retaining the requested topic.

Custom agents can also select the `youtube_search` tool. Its typed `query` argument returns actual titles, channels and video URLs to the agent's reasoning loop and records execution evidence. It requires External Access and the agent's existing tool allowlist; it does not play videos or require keys.

Tests cover Japanese/English intent routing, search-only versus play requests, generic-Web bypass in both chat transports, native custom-agent tool execution and its access checks, and fresh-result UI behavior without autoplay. The production frontend and signed macOS bundle are rebuilt with build ID `olcr-agent-youtube-search-20261028`. A running user instance is not terminated; reopening the updated app applies the change.
