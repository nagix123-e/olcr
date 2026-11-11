# File conversion with ConvertX

Open Settings → Chat capabilities and turn **Convert** ON. It is OFF by default.
OLCR downloads its private conversion environment only after this opt-in. Setup
progress and retry are shown beside the existing capability switch. Docker,
Homebrew, terminal commands, and administrator installation are not required.
The first download is several GB and needs an internet connection; conversions
then run locally. macOS 13+ on Apple Silicon and Intel are supported.
Initial setup requires at least 12 GB of free disk space.

Turning Convert OFF cancels pending setup and stops the private VM. Downloaded
tools are retained for reuse. With Convert OFF, attaching a file, sending a
conversion request, opening Settings, and launching OLCR do not download tools.
With the saved preference ON, a later conversion can restart the cached runtime.

Runtime files live in the OLCR app-support directory under `cx/`. The signed
upstream Lima binary (2.0.3) and Ubuntu 24.04 image (20261017) are SHA256-verified.
The upstream ConvertX v0.19.0 OCI image is fixed by its registry digest. The VM
uses Apple's built-in virtualization, rootless containerd/nerdctl, no host
directory mounts, and forwards only the conversion port to host loopback.
It is stopped when the backend exits. Temporary uploads expire after one hour
of service operation. The setup log is `cx/runtime.log`.

Attach a file and send `pdfに変えて`, `.pngに変換して`, or `Convert this to docx`.
OLCR preserves the original attachment bytes (up to 50 MB), selects the first
matching converter advertised by ConvertX, and performs a mechanical conversion.
No language model receives the conversion payload. This also works in custom-agent
conversations and conversations without a project.

Results are saved directly in the project's configured workspace directory.
Without a workspace, OLCR saves to `~/Downloads`. An invalid configured workspace
produces an error rather than silently choosing another destination. Existing files
are preserved: collisions receive ` (1)`, ` (2)`, etc. Multiple output files are
saved separately. The reply lists the saved absolute paths. Unsupported conversion
pairs and conversion failures are reported without generating substitute content.
Output is limited to 250 MB per request; conversion has a five-minute deadline.

For an existing local ConvertX instance, set `OLCR_CONVERTX_URL` in the backend's
environment to its loopback HTTP origin (no path). It must have
`ALLOW_UNAUTHENTICATED=true` and `HTTP_ALLOWED=true`. Remote origins are rejected
so attachments stay on the local machine. The adapter uses upstream's cookie-based
web routes, not an official versioned REST API, so verify compatibility before
upgrading the pinned service.

Upstream source and license: https://github.com/C4illin/ConvertX (AGPL-3.0).
ConvertX and its converters are automatically acquired upstream components running in the private OLCR environment. Their upstream licenses remain applicable.
