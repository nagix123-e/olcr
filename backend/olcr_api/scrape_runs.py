"""Checkpointed deterministic Recipe execution over the managed browser."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .recipes import (ScrapeRecipeInput, build_record_selector_recovery_candidates,
                      enrich_detail_rows)


def _bounded_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(0, min(value, 100_000))


class ScrapeRunManager:
    def __init__(self, database, browser_sessions):
        self.database = database
        self.browser_sessions = browser_sessions
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._attached_sessions: dict[str, str] = {}
        self._pause: dict[str, threading.Event] = {}
        self._stop: dict[str, threading.Event] = {}
        self._closed = False
        self.database.interrupt_active_web_scrape_runs()

    def create(self, recipe_id: str, *, session_id: str | None = None) -> dict:
        recipe = self.database.web_recipe(recipe_id)
        if recipe is None: raise KeyError("WEB_RECIPE_NOT_FOUND")
        if recipe.get("status") != "VALIDATED": raise ValueError("RECIPE_NOT_VALIDATED")
        if recipe.get("source") == "network_json":
            if not (recipe.get("network_json") or {}).get("fields"):
                raise ValueError("RECIPE_NEEDS_FIELD_MAPPINGS")
        elif not recipe.get("fields") and not recipe.get("tool"):
            raise ValueError("RECIPE_NEEDS_FIELD_MAPPINGS")
        recipe_url = str(recipe.get("start_url") or (recipe.get("start") or {}).get("url") or "")
        if session_id:
            snapshot = self.browser_sessions.snapshot(session_id)
            if snapshot.get("status") != "AVAILABLE":
                raise ValueError(str(snapshot.get("error") or "ATTACHED_BROWSER_SESSION_UNAVAILABLE"))
            if not self._same_origin(recipe_url, str(snapshot.get("url") or "")):
                raise ValueError("ATTACHED_BROWSER_SESSION_ORIGIN_MISMATCH")
        run_id = str(uuid.uuid4())
        self.database.create_web_scrape_run(run_id, recipe)
        if session_id:
            with self._lock:
                self._attached_sessions[run_id] = session_id
        self.start(run_id)
        return self.database.web_scrape_run(run_id)

    def get(self, run_id: str) -> dict | None:
        return self.database.web_scrape_run(run_id)

    def recovery_snapshot(self, run_id: str) -> dict:
        """Reopen the exact failed Recipe page transiently for explicit review."""
        run = self.database.web_scrape_run(run_id)
        if run is None:
            raise KeyError("SCRAPE_RUN_NOT_FOUND")
        if run.get("status") != "FAILED" or run.get("stop_reason") != "SELECTOR_RECOVERY":
            raise ValueError("SCRAPE_RECOVERY_PREVIEW_UNAVAILABLE")
        with self._lock:
            thread = self._threads.get(run_id)
            if thread and thread.is_alive():
                raise ValueError("SCRAPE_RECOVERY_PREVIEW_NOT_READY")

        recipe = ScrapeRecipeInput.from_record(run["recipe"])
        recovery = run.get("recovery") or {}
        try:
            page_number = int(recovery.get("page_number"))
        except (TypeError, ValueError):
            raise ValueError("SCRAPE_RECOVERY_STATE_INVALID") from None
        if page_number < 1 or page_number > recipe.max_pages:
            raise ValueError("SCRAPE_RECOVERY_STATE_INVALID")

        opened = self.browser_sessions.open(profile_id=recipe.browser_profile_id)
        if opened.get("status") != "READY":
            raise ValueError(str(opened.get("error") or "SCRAPE_RECOVERY_BROWSER_UNAVAILABLE"))
        session_id = str(opened.get("session_id") or "")
        if not session_id:
            raise ValueError("SCRAPE_RECOVERY_BROWSER_SESSION_INVALID")

        try:
            if recipe.navigation.type in {"load_more", "infinite_scroll"}:
                page = self.browser_sessions.navigate(session_id, recipe.start_url)
                if page.get("status") != "AVAILABLE":
                    raise ValueError(str(page.get("error") or "SCRAPE_RECOVERY_NAVIGATION_FAILED"))
                for replayed_page in range(1, page_number):
                    wait_result = self._wait_for_page_state(
                        session_id, recipe.wait_strategy, run_id, float(run["created_at"]),
                        enforce_duration=False,
                    )
                    if wait_result.get("status") != "AVAILABLE":
                        raise ValueError(str(wait_result.get("error") or "SCRAPE_RECOVERY_WAIT_FAILED"))
                    step = (self.browser_sessions.click_recipe_load_more(
                        session_id, recipe.navigation.selector or ""
                    ) if recipe.navigation.type == "load_more" else
                        self.browser_sessions.scroll_for_recipe_page(session_id))
                    if step.get("status") != "AVAILABLE":
                        raise ValueError(str(step.get("error") or "SCRAPE_RECOVERY_REPLAY_FAILED"))
                target_url = recipe.start_url
            elif recipe.navigation.type == "url_parameter":
                target_url = self._page_url(
                    recipe.start_url, recipe.navigation.page_parameter, page_number
                )
                page = self.browser_sessions.navigate(session_id, target_url)
                if page.get("status") != "AVAILABLE":
                    raise ValueError(str(page.get("error") or "SCRAPE_RECOVERY_NAVIGATION_FAILED"))
            elif recipe.navigation.type == "next":
                target_url = recipe.start_url if page_number == 1 else str(run.get("next_url") or "")
                parsed_target = urlsplit(target_url) if target_url else None
                if (not parsed_target or parsed_target.scheme not in {"http", "https"}
                        or not parsed_target.hostname or parsed_target.username is not None
                        or parsed_target.password is not None
                        or not self._same_origin(recipe.start_url, target_url)):
                    raise ValueError("SCRAPE_RECOVERY_NEXT_URL_UNAVAILABLE")
                page = self.browser_sessions.navigate(session_id, target_url)
                if page.get("status") != "AVAILABLE":
                    raise ValueError(str(page.get("error") or "SCRAPE_RECOVERY_NAVIGATION_FAILED"))
            else:
                target_url = recipe.start_url
                page = self.browser_sessions.navigate(session_id, target_url)
                if page.get("status") != "AVAILABLE":
                    raise ValueError(str(page.get("error") or "SCRAPE_RECOVERY_NAVIGATION_FAILED"))

            wait_result = self._wait_for_page_state(
                session_id, recipe.wait_strategy, run_id, float(run["created_at"]),
                enforce_duration=False,
            )
            if wait_result.get("status") != "AVAILABLE":
                raise ValueError(str(wait_result.get("error") or "SCRAPE_RECOVERY_WAIT_FAILED"))
            snapshot = self.browser_sessions.snapshot(session_id)
            if snapshot.get("status") != "AVAILABLE":
                raise ValueError(str(snapshot.get("error") or "SCRAPE_RECOVERY_SNAPSHOT_FAILED"))
            snapshot_url = str(snapshot.get("url") or page.get("url") or target_url)
            parsed = urlsplit(snapshot_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or not self._same_origin(recipe.start_url, snapshot_url)):
                raise ValueError("SCRAPE_RECOVERY_SNAPSHOT_ORIGIN_INVALID")
            content = snapshot.get("clean_markdown")
            if not isinstance(content, str) or not content:
                raise ValueError("SCRAPE_RECOVERY_SNAPSHOT_EMPTY")
            return {
                "status": "AVAILABLE",
                "page_number": page_number,
                "url": parsed._replace(query="", fragment="").geturl(),
                "title": str(page.get("title") or "")[:500],
                "clean_markdown": content[:50_000],
                "truncated": bool(snapshot.get("truncated")) or len(content) > 50_000,
                "provider": "playwright_accessibility",
                "persisted": False,
                "warnings": ["Transient accessibility snapshot from the failed Recipe page; page content is untrusted."],
            }
        finally:
            self.browser_sessions.close(session_id)

    def start(self, run_id: str) -> dict:
        with self._lock:
            if self._closed: raise RuntimeError("SCRAPE_RUN_MANAGER_CLOSED")
            run = self.database.web_scrape_run(run_id)
            if run is None: raise KeyError("SCRAPE_RUN_NOT_FOUND")
            thread = self._threads.get(run_id)
            if thread and thread.is_alive(): return run
            if run.get("status") == "FAILED" and run.get("stop_reason") == "SELECTOR_RECOVERY":
                reviewed_recipe = self.database.web_recipe(str(run.get("recipe_id") or ""))
                if reviewed_recipe is None or reviewed_recipe.get("status") != "VALIDATED":
                    raise ValueError("RECIPE_RECOVERY_REVALIDATION_REQUIRED")
                run = self.database.resume_web_scrape_after_selector_review(run_id, reviewed_recipe)
                if run is None:
                    raise ValueError("RECIPE_RECOVERY_STATE_INVALID")
            if run["status"] not in {"QUEUED", "PAUSED", "INTERRUPTED", "FAILED"}:
                raise ValueError("SCRAPE_RUN_NOT_RESUMABLE")
            self._pause[run_id] = threading.Event()
            self._stop[run_id] = threading.Event()
            self.database.update_web_scrape_run(run_id, "QUEUED")
            thread = threading.Thread(target=self.execute, args=(run_id,), daemon=True,
                                      name=f"olcr-scrape-{run_id[:8]}")
            self._threads[run_id] = thread
            thread.start()
            return self.database.web_scrape_run(run_id)

    def pause(self, run_id: str) -> dict:
        run = self.database.web_scrape_run(run_id)
        if run is None: raise KeyError("SCRAPE_RUN_NOT_FOUND")
        with self._lock:
            if run["status"] not in {"QUEUED", "RUNNING"}: raise ValueError("SCRAPE_RUN_NOT_PAUSABLE")
            self._pause.setdefault(run_id, threading.Event()).set()
        return {**run, "pause_requested": True}

    def stop(self, run_id: str) -> dict:
        run = self.database.web_scrape_run(run_id)
        if run is None: raise KeyError("SCRAPE_RUN_NOT_FOUND")
        with self._lock:
            if run["status"] in {"COMPLETED", "STOPPED", "FAILED"}: return run
            self._stop.setdefault(run_id, threading.Event()).set()
            thread = self._threads.get(run_id)
            if not thread or not thread.is_alive():
                return self.database.update_web_scrape_run(run_id, "STOPPED", stop_reason="USER_STOP")
        return {**run, "stop_requested": True}

    def execute(self, run_id: str) -> None:
        session_id = None
        attached_session = False
        try:
            run = self.database.web_scrape_run(run_id)
            if run is None: return
            recipe_data = run["recipe"]
            recipe = ScrapeRecipeInput.from_record(recipe_data)
            if recipe.navigation.type not in {"none", "url_parameter", "next", "load_more", "infinite_scroll"}:
                self.database.update_web_scrape_run(run_id, "FAILED", error="NAVIGATION_TYPE_UNAVAILABLE")
                return
            self.database.update_web_scrape_run(run_id, "RUNNING")
            with self._lock:
                session_id = self._attached_sessions.pop(run_id, None)
            if session_id:
                attached_session = True
                attached_page = self.browser_sessions.snapshot(session_id)
                if (attached_page.get("status") != "AVAILABLE"
                        or not self._same_origin(recipe.start_url, str(attached_page.get("url") or ""))):
                    self.database.update_web_scrape_run(
                        run_id, "FAILED", error="ATTACHED_BROWSER_SESSION_UNAVAILABLE_ORIGIN_MISMATCH")
                    return
            else:
                opened = (self.browser_sessions.open(profile_id=recipe.browser_profile_id)
                          if recipe.browser_profile_id else self.browser_sessions.open())
                if opened.get("status") != "READY":
                    self.database.update_web_scrape_run(run_id, "FAILED", error=str(opened.get("error") or "BROWSER_UNAVAILABLE"))
                    return
                session_id = opened["session_id"]
            initial_run = self.database.web_scrape_run(run_id)
            start_page = int(initial_run["page_cursor"]) + 1
            page_limit = recipe.max_pages if recipe.navigation.type in {"url_parameter", "next", "load_more", "infinite_scroll"} else 1
            if recipe.urls: page_limit = min(len(recipe.urls), recipe.max_pages)
            next_page_url = initial_run.get("next_url")
            field_definitions = {name: field.model_dump() for name, field in recipe.fields.items()}
            output_field_names = (list(recipe.network_json.fields) if recipe.source == "network_json"
                                  and recipe.network_json else list(recipe.fields))
            previous_count = int(initial_run["record_count"])
            previous_hash = initial_run.get("last_page_hash")
            if start_page > 1 and recipe.navigation.type in {"load_more", "infinite_scroll"}:
                restored = self.browser_sessions.navigate(session_id, recipe.start_url)
                if restored.get("status") != "AVAILABLE":
                    self.database.update_web_scrape_run(run_id, "FAILED", error="DYNAMIC_PAGE_RESTORE_FAILED")
                    return
                wait_result = self._wait_for_page_state(
                    session_id, recipe.wait_strategy, run_id, initial_run["created_at"]
                )
                if not self._continue_after_wait(run_id, wait_result):
                    return
                for _ in range(1, start_page - 1):
                    current = self.database.web_scrape_run(run_id)
                    if time.time() - float(current["created_at"]) >= recipe.max_duration_seconds:
                        self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="MAX_DURATION")
                        return
                    if self._requested(self._stop, run_id):
                        self.database.update_web_scrape_run(run_id, "STOPPED", stop_reason="USER_STOP")
                        return
                    if self._requested(self._pause, run_id):
                        self.database.update_web_scrape_run(run_id, "PAUSED", stop_reason="USER_PAUSE")
                        return
                    restore_step = (self.browser_sessions.click_recipe_load_more(session_id, recipe.navigation.selector or "")
                                    if recipe.navigation.type == "load_more"
                                    else self.browser_sessions.scroll_for_recipe_page(session_id))
                    if restore_step.get("status") != "AVAILABLE":
                        self.database.update_web_scrape_run(run_id, "FAILED", error="DYNAMIC_PAGE_RESTORE_FAILED")
                        return
                    wait_result = self._wait_for_page_state(
                        session_id, recipe.wait_strategy, run_id, initial_run["created_at"]
                    )
                    if not self._continue_after_wait(run_id, wait_result):
                        return
            for page_number in range(start_page, page_limit + 1):
                if self._requested(self._stop, run_id):
                    self.database.update_web_scrape_run(run_id, "STOPPED", stop_reason="USER_STOP")
                    return
                if self._requested(self._pause, run_id):
                    self.database.update_web_scrape_run(run_id, "PAUSED", stop_reason="USER_PAUSE")
                    return
                current = self.database.web_scrape_run(run_id)
                if time.time() - float(current["created_at"]) >= recipe.max_duration_seconds:
                    self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="MAX_DURATION")
                    return
                if previous_count >= recipe.max_records:
                    self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="MAX_RECORDS")
                    return
                if recipe.urls:
                    target_url = recipe.urls[page_number - 1]
                elif recipe.navigation.type == "url_parameter":
                    target_url = self._page_url(recipe.start_url, recipe.navigation.page_parameter, page_number)
                elif recipe.navigation.type == "next":
                    target_url = next_page_url if page_number > 1 else recipe.start_url
                    if not target_url:
                        self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="NO_NEXT_LINK")
                        return
                elif recipe.navigation.type in {"load_more", "infinite_scroll"}:
                    target_url = None
                else:
                    target_url = recipe.start_url
                if page_number == 1 or target_url is not None:
                    page = self.browser_sessions.navigate(session_id, target_url)
                elif recipe.navigation.type == "load_more":
                    page = self.browser_sessions.click_recipe_load_more(session_id, recipe.navigation.selector or "")
                else:
                    page = self.browser_sessions.scroll_for_recipe_page(session_id)
                if page.get("status") != "AVAILABLE":
                    if recipe.tool and page_number > 1 and recipe.navigation.type == "load_more" and page.get("error") == "LOAD_MORE_TARGET_NOT_ENABLED_BUTTON":
                        self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="NO_NEXT_CONTROL")
                        return
                    self.database.update_web_scrape_run(run_id, "FAILED", error=str(page.get("error") or "PAGE_NAVIGATION_FAILED"))
                    return
                self.database.add_web_scrape_metrics(run_id, {"pages_visited": 1})
                wait_result = self._wait_for_page_state(
                    session_id, recipe.wait_strategy, run_id, current["created_at"]
                )
                if not self._continue_after_wait(run_id, wait_result):
                    return
                if recipe.page_delay_seconds:
                    # Stop/pause are checked at short intervals during rate limiting.
                    deadline = time.monotonic() + recipe.page_delay_seconds
                    while time.monotonic() < deadline:
                        if time.time() - float(current["created_at"]) >= recipe.max_duration_seconds:
                            self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="MAX_DURATION")
                            return
                        if self._requested(self._stop, run_id) or self._requested(self._pause, run_id):
                            self.database.update_web_scrape_run(run_id, "STOPPED" if self._requested(self._stop, run_id) else "PAUSED", stop_reason="USER_STOP" if self._requested(self._stop, run_id) else "USER_PAUSE")
                            return
                        time.sleep(min(.1, max(0, deadline - time.monotonic())))
                limit = min(10000 if recipe.tool else 100, recipe.max_records - previous_count)
                if recipe.source == "network_json" and recipe.network_json is not None:
                    self.database.add_web_scrape_metrics(run_id, {"network_extractions": 1})
                    evidence = self.browser_sessions.extract_network_json_recipe(
                        session_id, recipe.network_json.model_dump(), limit=limit
                    )
                else:
                    self.database.add_web_scrape_metrics(run_id, {"dom_extractions": 1})
                    evidence = self.browser_sessions.extract_recipe(
                        session_id, recipe.record_selector, field_definitions, limit=limit,
                        **({"tool": recipe.tool} if recipe.tool else {})
                    )
                if evidence.get("status") != "AVAILABLE":
                    error = str(evidence.get("error") or "RECIPE_EXTRACTION_FAILED")
                    if error in {"RECIPE_RECORD_SELECTOR_MATCHED_NO_ROWS", "RECIPE_FIELDS_MATCHED_NO_CELLS"}:
                        reason = "NO_ROWS" if error == "RECIPE_RECORD_SELECTOR_MATCHED_NO_ROWS" else "FIELDS_UNMATCHED"
                        recovery = self._selector_recovery_review(
                            session_id, recipe.record_selector, field_definitions, page_number, reason
                        )
                        self.database.set_web_scrape_recovery(run_id, recovery)
                    else:
                        self.database.update_web_scrape_run(run_id, "FAILED", error=error)
                    return
                rows = evidence.get("rows") or []
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    self.database.update_web_scrape_run(run_id, "FAILED", error="RECIPE_ROW_SHAPE_INVALID")
                    return
                if recipe.tool == "list" and not rows and previous_count == 0:
                    self.database.update_web_scrape_run(run_id, "FAILED", error="AUTO_LIST_MATCHED_NO_RECORDS")
                    return
                discovered = _bounded_count(evidence.get("record_count"))
                self.database.add_web_scrape_metrics(run_id, {"records_discovered": max(discovered, len(rows))})
                field_match_counts = evidence.get("field_match_counts") or {}
                unmatched_fields = [name for name in output_field_names
                                    if _bounded_count(field_match_counts.get(name, 0)) < 1]
                if recipe.source == "dom" and not recipe.tool and rows and unmatched_fields:
                    recovery = self._selector_recovery_review(
                        session_id, recipe.record_selector, field_definitions, page_number, "FIELDS_UNMATCHED",
                        unmatched_fields=unmatched_fields,
                    )
                    self.database.set_web_scrape_recovery(run_id, recovery)
                    return
                if recipe.source == "dom" and not recipe.tool and not rows and _bounded_count(evidence.get("record_count")) == 0:
                    recovery = self._selector_recovery_review(
                        session_id, recipe.record_selector, field_definitions, page_number, "NO_ROWS"
                    )
                    if any(item["record_count"] > 0 for item in recovery["candidates"]):
                        self.database.set_web_scrape_recovery(run_id, recovery)
                        return
                following_url = None
                if recipe.navigation.type == "next" and page_number < page_limit:
                    next_link = self.browser_sessions.extract_next_link(session_id, recipe.navigation.selector or "")
                    if next_link.get("status") != "AVAILABLE":
                        self.database.update_web_scrape_run(run_id, "FAILED", error=str(next_link.get("error") or "NEXT_LINK_EXTRACTION_FAILED"))
                        return
                    following_url = next_link.get("url")
                    if following_url:
                        try:
                            ScrapeRecipeInput.model_validate({"name": recipe.name, "start_url": following_url,
                                "record_selector": recipe.record_selector, "fields": field_definitions})
                        except Exception:
                            self.database.update_web_scrape_run(run_id, "FAILED", error="NEXT_LINK_URL_REJECTED")
                            return
                        if not self._same_origin(recipe.start_url, following_url):
                            self.database.update_web_scrape_run(run_id, "FAILED", error="NEXT_LINK_ORIGIN_CHANGED")
                            return
                if recipe.detail:
                    original_url = page.get("url") or target_url or recipe.start_url
                    detailed = enrich_detail_rows(
                        recipe, rows, self.browser_sessions, session_id, original_url,
                        wait_after_navigation=lambda: self._wait_for_page_state(
                            session_id, recipe.wait_strategy, run_id, current["created_at"],
                            enforce_duration=False,
                        ),
                        on_page_visited=lambda: self.database.add_web_scrape_metrics(
                            run_id, {"detail_pages_visited": 1}
                        ),
                        on_extraction=lambda: self.database.add_web_scrape_metrics(
                            run_id, {"detail_extractions": 1}
                        ),
                    )
                    if detailed.get("status") != "AVAILABLE":
                        self.database.update_web_scrape_run(run_id, "FAILED", error=str(detailed.get("error") or "DETAIL_FIELDS_UNAVAILABLE"))
                        return
                    rows = detailed["rows"]
                page_hash = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                if previous_hash and page_hash == previous_hash:
                    self.database.checkpoint_web_scrape_page(
                        run_id, page_number, [], page_hash, following_url, discovered_count=len(rows[:limit])
                    )
                    self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="REPEATED_PAGE")
                    return
                rows_to_save = []
                for row in rows[:limit]:
                    dedupe_key = self._dedupe_key(row, recipe.deduplicate_keys)
                    rows_to_save.append((dedupe_key, row))
                after = self.database.checkpoint_web_scrape_page(
                    run_id, page_number, rows_to_save, page_hash, following_url,
                    discovered_count=len(rows_to_save),
                )
                new_count = int((after or {}).get("record_count") or 0)
                if recipe.navigation.type == "next":
                    next_page_url = following_url
                if self._requested(self._stop, run_id):
                    self.database.update_web_scrape_run(run_id, "STOPPED", stop_reason="USER_STOP")
                    return
                if self._requested(self._pause, run_id):
                    self.database.update_web_scrape_run(run_id, "PAUSED", stop_reason="USER_PAUSE")
                    return
                if not rows:
                    self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="NO_RECORDS")
                    return
                if recipe.navigation.type == "next" and not following_url and page_number < page_limit:
                    self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="NO_NEXT_LINK")
                    return
                if recipe.deduplicate_keys and new_count == previous_count:
                    self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="NO_NEW_RECORDS")
                    return
                previous_count, previous_hash = new_count, page_hash
                if previous_count >= recipe.max_records:
                    self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="MAX_RECORDS")
                    return
            self.database.update_web_scrape_run(run_id, "COMPLETED", stop_reason="MAX_PAGES")
        except Exception as exc:
            self.database.update_web_scrape_run(run_id, "FAILED", error=type(exc).__name__ + ": " + str(exc)[:400])
        finally:
            # A chat-owned browser is also the live preview shown in the
            # conversation. Keep it available after the run so the user can
            # inspect the page and issue a follow-up instruction in the same
            # chat. Transient sessions opened by standalone Scrape Studio are
            # still closed when their run ends.
            if session_id and not attached_session:
                self.browser_sessions.close(session_id)
            with self._lock:
                self._pause.pop(run_id, None)
                self._stop.pop(run_id, None)
                if self._threads.get(run_id) is threading.current_thread():
                    self._threads.pop(run_id, None)

    def close_all(self) -> None:
        with self._lock:
            self._closed = True
            for event in self._stop.values(): event.set()
            threads = list(self._threads.values())
        for thread in threads:
            if thread is not threading.current_thread(): thread.join(timeout=5)

    @staticmethod
    def _requested(events: dict[str, threading.Event], run_id: str) -> bool:
        event = events.get(run_id)
        return bool(event and event.is_set())

    def _wait_for_page_state(self, session_id: str, strategy: str,
                             run_id: str, created_at: float, *,
                             enforce_duration: bool = True) -> dict:
        """Apply the Recipe's bounded readiness policy before extracting rows."""
        if strategy == "page_load":
            # A fixed, read-only page.evaluate waits for document.readyState to
            # reach complete, with its own 2-second timeout.
            result = self.browser_sessions.wait_for_page_load(session_id)
            if result.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": str(result.get("error") or "PAGE_LOAD_WAIT_FAILED")}
            return {"status": "AVAILABLE"}
        deadline = time.monotonic() + (0.25 if strategy == "short_delay" else 2.0)
        if strategy == "short_delay":
            while time.monotonic() < deadline:
                terminal = self._wait_terminal_state(run_id, created_at) if enforce_duration else None
                if terminal:
                    return terminal
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            terminal = self._wait_terminal_state(run_id, created_at) if enforce_duration else None
            return terminal or {"status": "AVAILABLE"}

        previous: str | None = None
        while time.monotonic() < deadline:
            terminal = self._wait_terminal_state(run_id, created_at) if enforce_duration else None
            if terminal:
                return terminal
            snapshot = self.browser_sessions.snapshot(session_id)
            if snapshot.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": str(snapshot.get("error") or "DOM_STABILITY_SNAPSHOT_FAILED")}
            current = snapshot.get("clean_markdown")
            if not isinstance(current, str) or not current:
                return {"status": "FAILED", "error": "DOM_STABILITY_SNAPSHOT_EMPTY"}
            if current == previous:
                return {"status": "AVAILABLE"}
            previous = current
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return {"status": "FAILED", "error": "DOM_STABILITY_TIMEOUT"}

    def _wait_terminal_state(self, run_id: str, created_at: float) -> dict | None:
        # Preserve the run contract: a pause/stop requested after navigation
        # commits that page's rows before the caller honors the request.
        current = self.database.web_scrape_run(run_id)
        duration_started = float((current or {}).get("created_at") or created_at)
        recipe = (current or {}).get("recipe") or {}
        maximum = int(recipe.get("max_duration_seconds") or 900)
        if time.time() - duration_started >= maximum:
            return {"status": "COMPLETED", "stop_reason": "MAX_DURATION"}
        return None

    def _selector_recovery_review(self, session_id: str, original_selector: str,
                                  fields: dict, page_number: int, reason: str,
                                  *, unmatched_fields: list[str] | None = None) -> dict:
        """Collect bounded evidence for a human-reviewed, revalidated Recipe repair."""
        evidence = self.browser_sessions.picker_evidence(session_id, "row")
        candidates = build_record_selector_recovery_candidates(evidence, original_selector)
        results_by_id: dict[str, dict] = {}
        probe_error = None
        if candidates:
            probed = self.browser_sessions.probe_recipe_selectors(session_id, candidates, fields)
            if probed.get("status") == "AVAILABLE":
                results_by_id = {
                    item["candidate_id"]: item for item in probed.get("results", [])
                    if isinstance(item, dict) and isinstance(item.get("candidate_id"), str)
                }
            else:
                probe_error = str(probed.get("error") or "RECIPE_RECOVERY_PROBE_FAILED")[:120]
        else:
            probe_error = str(evidence.get("error") or "RECIPE_RECOVERY_HAS_NO_ALTERNATIVE_ROWS")[:120]
        candidate_results = []
        for candidate in candidates:
            result = results_by_id.get(candidate["candidate_id"], {})
            field_counts = result.get("field_match_counts")
            field_counts = field_counts if isinstance(field_counts, dict) else {}
            candidate_results.append({
                **candidate,
                "record_count": _bounded_count(result.get("record_count")),
                "field_match_counts": {
                    name: _bounded_count(field_counts.get(name, 0))
                    for name in fields
                },
                "error": str(result.get("error") or probe_error or "")[:120] or None,
            })
        return {
            "page_number": max(1, int(page_number)),
            "reason": reason if reason in {"NO_ROWS", "FIELDS_UNMATCHED"} else "NO_ROWS",
            "original_selector": str(original_selector)[:512],
            "unmatched_fields": [str(name)[:120] for name in (unmatched_fields or [])[:50]],
            "candidates": candidate_results[:12],
        }

    def _continue_after_wait(self, run_id: str, result: dict) -> bool:
        status = result.get("status")
        if status == "AVAILABLE":
            return True
        if status in {"STOPPED", "PAUSED", "COMPLETED"}:
            self.database.update_web_scrape_run(
                run_id, status,
                stop_reason=result.get("stop_reason") or ("USER_STOP" if status == "STOPPED" else
                    "USER_PAUSE" if status == "PAUSED" else "MAX_DURATION"),
            )
        else:
            self.database.update_web_scrape_run(
                run_id, "FAILED", error=str(result.get("error") or "PAGE_WAIT_FAILED")
            )
        return False

    @staticmethod
    def _page_url(url: str, parameter: str | None, page_number: int) -> str:
        parsed = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != parameter]
        starting_page = next((int(value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                              if key == parameter and value.isdigit()), 1)
        query.append((str(parameter), str(starting_page + page_number - 1)))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))

    @staticmethod
    def _dedupe_key(row: dict, keys: tuple[str, ...]) -> str | None:
        if not keys: return None
        values = [row.get(key) for key in keys]
        if any(value is None or value == "" or value == [] for value in values): return None
        return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _same_origin(first: str, second: str) -> bool:
        left, right = urlsplit(first), urlsplit(second)
        return (left.scheme.lower(), left.hostname.lower() if left.hostname else None, left.port) == (
            right.scheme.lower(), right.hostname.lower() if right.hostname else None, right.port)
