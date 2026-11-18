"""Signed macOS build verification and Apple notarization stages."""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

from ..coding_tasks import ReleaseIntent, ReleasePreflight, build_release_preflight
from ..provider_research import ProviderEvidence
from ..release_workflow import ReleaseRequest
from .base import CommandResult, CommandSpec, ReleaseCommandRunner


_SUBMISSION_ID = re.compile(r"^[A-Za-z0-9-]{8,100}$")
_KEYCHAIN_PROFILE = re.compile(r"^[A-Za-z0-9_. -]{1,100}$")
_SIGNING_OUTPUT_MAX = 64 * 1024
_REFERENCE_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class BuildStageResult:
    """A local build stage outcome; this type has no published state."""

    status: Literal["NOTARIZED", "BLOCKED", "UNCERTAIN", "FAILED"]
    provider_state: str
    checks: tuple[Mapping[str, str], ...]
    summary: str
    submission_id: str | None = None
    artifact_path: Path | None = None
    retry_requires_inspection: bool = False


def _metadata(request: ReleaseRequest) -> Mapping[str, object]:
    return request.metadata if isinstance(request.metadata, Mapping) else {}


def _evidence_value(reference: object, key: str, default=None):
    if isinstance(reference, Mapping):
        return reference.get(key, default)
    return getattr(reference, key, default)


def _artifact_path(request: ReleaseRequest) -> Path | None:
    if not isinstance(request.artifact_paths, (tuple, list)) or len(request.artifact_paths) != 1:
        return None
    try:
        root = Path(request.project_root).expanduser().resolve(strict=True)
        candidate = Path(request.artifact_paths[0])
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not (resolved.is_dir() and resolved.suffix.casefold() == ".app"):
            return None
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


class MacOSNotarizationStage:
    """Verify Developer ID signing, notarize, staple, and validate one artifact."""

    provider_id = "apple_macos"

    def __init__(
        self,
        *,
        runner: ReleaseCommandRunner | None = None,
        credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runner = runner or ReleaseCommandRunner(credential_provider=credential_provider)
        self.credential_provider = credential_provider or getattr(self.runner, "credential_provider", None)
        self.clock = clock

    def _keychain_profile(self) -> str | None:
        if self.credential_provider is None:
            return None
        try:
            supplied = self.credential_provider("apple_macos")
        except Exception:
            return None
        if not isinstance(supplied, Mapping):
            return None
        value = supplied.get("APPLE_NOTARY_KEYCHAIN_PROFILE") or supplied.get("keychain_profile")
        if not isinstance(value, str) or not _KEYCHAIN_PROFILE.fullmatch(value):
            return None
        return value

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        metadata = _metadata(request)
        authorization = request.authorization
        resolved = bool(
            request.provider_id == self.provider_id
            and isinstance(request.app_or_package, str) and request.app_or_package.strip()
            and isinstance(request.version, str) and request.version.strip()
            and isinstance(request.channel, str) and request.channel.strip()
        )
        intent = ReleaseIntent(
            requested=getattr(authorization, "explicit_intent", False) is True,
            provider_id=self.provider_id if resolved else None,
            app_or_package=request.app_or_package if resolved else None,
            version=request.version if resolved else None,
            channel=request.channel if resolved else None,
            blockers=[] if resolved else ["Apple macOS release target is not resolved."],
        )
        references = getattr(authorization, "current_references", ())
        if not isinstance(references, (tuple, list)):
            references = ()
        required = metadata.get("required_attestations", ())
        completed = metadata.get("completed_attestations", ())
        common = build_release_preflight(
            intent,
            current_references=references,
            external_access_enabled=getattr(authorization, "external_access_enabled", False) is True,
            agreement_accepted_by_user=metadata.get("agreement_accepted_by_user") is True,
            credentials_configured=(metadata.get("credentials_configured") is True
                                    and self._keychain_profile() is not None),
            local_build_validated=metadata.get("local_build_validated") is True,
            paid_plan_choice_required=metadata.get("paid_plan_choice_required") is True,
            paid_plan_selected_by_user=metadata.get("paid_plan_selected_by_user") is True,
            required_attestations=tuple(item for item in required if isinstance(item, str))
            if isinstance(required, (tuple, list)) else (),
            completed_attestations=tuple(item for item in completed if isinstance(item, str))
            if isinstance(completed, (tuple, list)) else (),
            now=self.clock(),
        )
        checks = [dict(row) for row in common.checks]
        blockers = list(common.blockers)

        signing_refs = [
            reference for reference in references
            if _evidence_value(reference, "provider_id") == self.provider_id
            and _evidence_value(reference, "category") == "signing"
            and _evidence_value(reference, "officiality") == "VERIFIED"
        ]
        current_signing_reference = False
        for reference in signing_refs:
            try:
                age = self.clock() - float(_evidence_value(reference, "accessed_at"))
            except (TypeError, ValueError):
                continue
            if 0 <= age <= _REFERENCE_MAX_AGE_SECONDS:
                current_signing_reference = True
                break
        if not current_signing_reference:
            message = "Current verified Apple notarization requirements are required."
            checks.append({"name": "notarization_requirements", "status": "BLOCKED", "message": message})
            blockers.append(message)
        else:
            checks.append({"name": "notarization_requirements", "status": "PASS",
                           "message": "Current verified Apple notarization requirements are available."})

        artifact = _artifact_path(request)
        if artifact is None:
            message = "One signed .app bundle inside the workspace is required."
            checks.append({"name": "artifact", "status": "BLOCKED", "message": message})
            blockers.append(message)
        else:
            checks.append({"name": "artifact", "status": "PASS", "message": "Workspace artifact is confined and readable."})

        for key, label in (("signed_build", "signed_build"),
                           ("hardened_runtime", "hardened_runtime"),
                           ("secure_timestamp", "secure_timestamp")):
            passed = metadata.get(key) is True
            message = (f"{label.replace('_', ' ').capitalize()} requirements need user confirmation."
                       if not passed else f"{label.replace('_', ' ').capitalize()} requirement is confirmed.")
            checks.append({"name": label, "status": "PASS" if passed else "BLOCKED", "message": message})
            if not passed:
                blockers.append(message)
        if request.provider_id != self.provider_id:
            message = "Configured notarization stage does not match the requested provider."
            checks.append({"name": "provider", "status": "BLOCKED", "message": message})
            blockers.append(message)
        return ReleasePreflight("BLOCKED" if blockers else "READY", checks,
                                list(dict.fromkeys(blockers)))

    @staticmethod
    def _json_payload(result: CommandResult) -> Mapping[str, object] | None:
        if not isinstance(result.stdout, str) or len(result.stdout) > _SIGNING_OUTPUT_MAX:
            return None
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    @staticmethod
    def _submission(payload: Mapping[str, object] | None) -> tuple[str | None, str | None]:
        if payload is None:
            return None, None
        submission_id = payload.get("id")
        status = payload.get("status")
        if not isinstance(submission_id, str) or not _SUBMISSION_ID.fullmatch(submission_id):
            submission_id = None
        if not isinstance(status, str):
            status = None
        return submission_id, status

    def _command(
        self,
        operation: tuple[str, ...],
        path: Path,
        *,
        timeout: int = 300,
        include_path: bool = True,
    ) -> CommandResult:
        arguments = (*operation, *((str(path),) if include_path else ()))
        if include_path and operation[:2] == ("notarytool", "submit"):
            arguments = (*operation[:2], str(path), *operation[2:])
        return self.runner.run(CommandSpec(
            executable="xcrun" if operation[0] in {"notarytool", "stapler"} else "codesign",
            argv=arguments,
            cwd=path.parent,
            environment_profile="apple_notary",
            timeout_seconds=timeout,
        ))

    def _submit_and_staple(
        self,
        artifact: Path,
        archive: Path | None,
        profile: str,
        checks: tuple[Mapping[str, str], ...],
        submission_id: str | None,
    ) -> BuildStageResult:
        submission_status: str | None = None
        if submission_id is not None:
            info = self._command(("notarytool", "info", submission_id, "--keychain-profile", profile,
                                  "--output-format", "json"), artifact, timeout=60, include_path=False)
            if info.timed_out or info.error_code or info.returncode != 0:
                return BuildStageResult("UNCERTAIN", "SUBMISSION_INSPECTION_FAILED", checks,
                                        "Apple notarization status could not be inspected; inspect before retrying.",
                                        submission_id, artifact, True)
            confirmed_id, submission_status = self._submission(self._json_payload(info))
            if confirmed_id != submission_id or submission_status is None:
                return BuildStageResult("UNCERTAIN", "SUBMISSION_INSPECTION_INVALID", checks,
                                        "Apple returned an unrecognized notarization status; inspect before retrying.",
                                        submission_id, artifact, True)
        else:
            if archive is None:
                return BuildStageResult("BLOCKED", "NOTARIZATION_ARCHIVE_UNAVAILABLE", checks,
                                        "A notarization upload archive could not be created.",
                                        artifact_path=artifact)
            submit = self._command(("notarytool", "submit", "--keychain-profile", profile,
                                    "--wait", "--output-format", "json"), archive)
            submission_id, submission_status = self._submission(self._json_payload(submit))
            if submit.timed_out or submit.error_code:
                if submission_id is None:
                    return BuildStageResult("UNCERTAIN", "SUBMISSION_ID_UNAVAILABLE", checks,
                                            "Notarization submission timed out without a readable ID; inspect before retrying.",
                                            None, artifact, True)
                # A timed out submit may already have reached Apple. Read it before any retry.
                info = self._command(("notarytool", "info", submission_id, "--keychain-profile", profile,
                                      "--output-format", "json"), artifact, timeout=60, include_path=False)
                if info.timed_out or info.error_code or info.returncode != 0:
                    return BuildStageResult("UNCERTAIN", "SUBMISSION_INSPECTION_FAILED", checks,
                                            "Apple notarization status could not be inspected; inspect before retrying.",
                                            submission_id, artifact, True)
                confirmed_id, submission_status = self._submission(self._json_payload(info))
                if confirmed_id != submission_id or submission_status is None:
                    return BuildStageResult("UNCERTAIN", "SUBMISSION_INSPECTION_INVALID", checks,
                                            "Apple returned an unrecognized notarization status; inspect before retrying.",
                                            submission_id, artifact, True)
            elif submit.returncode != 0:
                return BuildStageResult("FAILED", "NOTARIZATION_REJECTED", checks,
                                        "Apple rejected the notarization submission.", submission_id, artifact)
            if submission_id is None or submission_status is None:
                return BuildStageResult("UNCERTAIN", "SUBMISSION_RESPONSE_INVALID", checks,
                                        "Apple notarization submission could not be confirmed; inspect before retrying.",
                                        submission_id, artifact, True)

        if submission_status.casefold() in {"in progress", "submitted", "processing"}:
            return BuildStageResult("UNCERTAIN", "NOTARIZATION_IN_PROGRESS", checks,
                                    "Apple is still processing notarization; inspect the submission before retrying.",
                                    submission_id, artifact, True)
        if submission_status.casefold() != "accepted":
            return BuildStageResult("FAILED", "NOTARIZATION_REJECTED", checks,
                                    "Apple did not accept the notarization submission.", submission_id, artifact)

        staple = self._command(("stapler", "staple"), artifact, timeout=120)
        if staple.returncode != 0 or staple.timed_out or staple.error_code:
            return BuildStageResult("UNCERTAIN", "STAPLE_UNCONFIRMED", checks,
                                    "Notarization was accepted, but stapling could not be confirmed.",
                                    submission_id, artifact, True)
        validate = self._command(("stapler", "validate"), artifact, timeout=120)
        if validate.returncode != 0 or validate.timed_out or validate.error_code:
            return BuildStageResult("UNCERTAIN", "STAPLE_VALIDATION_UNCONFIRMED", checks,
                                    "The notarization ticket could not be validated on the artifact.",
                                    submission_id, artifact, True)
        return BuildStageResult("NOTARIZED", "NOTARIZATION_ACCEPTED_AND_STAPLED", checks,
                                "Apple notarization was accepted and the ticket was validated on the app bundle.",
                                submission_id, artifact)

    def run(self, request: ReleaseRequest) -> BuildStageResult:
        preflight = self.preflight(request)
        checks = tuple(dict(row) for row in preflight.checks)
        if preflight.status != "READY":
            return BuildStageResult("BLOCKED", "PREFLIGHT_BLOCKED", checks,
                                    preflight.blockers[0] if preflight.blockers else
                                    "Apple macOS notarization preflight is blocked.")
        artifact = _artifact_path(request)
        profile = self._keychain_profile()
        if artifact is None or profile is None:
            return BuildStageResult("BLOCKED", "CONFIGURATION_UNAVAILABLE", checks,
                                    "Apple notarization configuration is unavailable.")

        verify = self._command(("--verify", "--deep", "--strict", "--verbose=2"), artifact)
        if verify.returncode != 0 or verify.timed_out or verify.error_code:
            return BuildStageResult("FAILED", "SIGNATURE_INVALID", checks,
                                    "The macOS artifact signature could not be verified.", artifact_path=artifact)
        display = self._command(("--display", "--verbose=4"), artifact)
        display_text = (display.stdout + "\n" + display.stderr)[:_SIGNING_OUTPUT_MAX]
        if (display.returncode != 0 or display.timed_out or display.error_code
                or "Authority=Developer ID" not in display_text
                or not re.search(r"flags=.*\(runtime\)", display_text)
                or not re.search(r"(?m)^Timestamp=\S+", display_text)):
            return BuildStageResult("BLOCKED", "SIGNING_REQUIREMENTS_UNMET", checks,
                                    "The artifact must use Developer ID signing, Hardened Runtime, and a secure timestamp.",
                                    artifact_path=artifact)

        metadata = _metadata(request)
        previous_id = metadata.get("notarization_submission_id")
        submission_id = previous_id if isinstance(previous_id, str) and _SUBMISSION_ID.fullmatch(previous_id) else None
        if submission_id is not None:
            return self._submit_and_staple(artifact, None, profile, checks, submission_id)
        with tempfile.TemporaryDirectory(prefix="olcr-apple-notary-") as temporary_directory:
            archive = Path(temporary_directory) / "notarization.zip"
            packaged = self.runner.run(CommandSpec(
                executable="ditto",
                argv=("-c", "-k", "--sequesterRsrc", "--keepParent", str(artifact), str(archive)),
                cwd=artifact.parent,
                environment_profile="apple_notary",
                timeout_seconds=300,
            ))
            if packaged.returncode != 0 or packaged.timed_out or packaged.error_code or not archive.is_file():
                return BuildStageResult("FAILED", "NOTARIZATION_ARCHIVE_FAILED", checks,
                                        "A notarization upload archive could not be created safely.",
                                        artifact_path=artifact)
            return self._submit_and_staple(artifact, archive, profile, checks, None)


__all__ = ["BuildStageResult", "MacOSNotarizationStage"]
