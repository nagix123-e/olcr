# OLCR permissions

The composer shield reads and writes `/api/permissions`. Choices are global to the local backend, persisted as the `permission_level` application setting, and restored at startup. The default is `automatic`; saving a level does not rebuild Runtime, change model selection, or replace an existing task queue. Legacy full-settings clients which omit this field preserve the current level.

| Level | Behavior |
| --- | --- |
| `confirm` | Execution entrypoints request permission before dispatch, including chat (which can select network and filesystem tools). Coding plans wait for approval before implementation. Existing explicit Git review remains in use. |
| `automatic` | Existing execution, protected-scope, Git preference and tool-risk behavior is preserved. |
| `full` | No OLCR permission approval or filesystem allowlist/workspace sandbox is imposed on supported file operations. Protected coding plans and scope expansions proceed, Git uses its existing approved executor automatically, network/capture capability switches do not block access, and sensitive native button actions do not require human confirmation. |

Full access grants authority to use existing executors; it does not invent unsupported tools or bypass operating-system permissions, missing credentials, file preimage checks, operation-schema validation, rollback, task identity checks, explicit user instructions (including plan-only requests), or result verification. The user can still stop/pause execution. Package ownership and task/verifier contracts continue to describe how the existing coding pipeline executes work.

Permission changes preserve the stored capability switches and retrieval roots; leaving full mode restores their effect. Full mode lifts direct-file read/write roots and implementation-operation path restrictions without changing the roots used for automatic file enumeration/indexing. It does not start scanning the whole computer.

In confirm mode the backend returns `409 PERMISSION_CONFIRMATION_REQUIRED` **before** routing or side effects. The frontend asks the user, approves/rejects `/api/permissions/approvals/{id}`, then retries the identical request once using `X-OLCR-Approval`. Grants expire in five minutes and bind method, URL and exact body bytes; changing the request or replaying a consumed grant cannot authorize it. Stop, polling, configuration and result verification remain available. Full and automatic requests do not add this round trip. Streaming uses the same handshake before opening its event stream.

Selecting full also releases existing permission-only coding scope waits, while keeping human plan-only, clarification, explicit pause and failed-verification states intact. No background model/provider reload is required.

Tests: `backend/tests/test_permissions.py`, `frontend/src/PermissionConnection.test.tsx`, `frontend/src/permissionFetch.test.ts`, plus existing coding/Git/runtime/composer/stream tests.
