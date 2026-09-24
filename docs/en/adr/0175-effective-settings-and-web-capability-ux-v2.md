# ADR 0175: Effective Settings and Capability-Driven Web UX V2

[简体中文](../../zh-CN/adr/0175-effective-settings-and-web-capability-ux-v2.md) · **English**

- Status: Accepted
- Date: 2026-09-24
- Scope: TUI Settings navigation, preference provenance, runtime reload, and Web Search profile selection

## Context

Agent preferences accumulated across input, execution, request, context, Web,
development, security, and background-task controls. The Settings forms showed
inheritance and raw stored values too prominently, while a Web Search mode did
not let users select a trusted executable profile. Runtime-affecting saves were
described as next-launch changes even though the application already had a
controlled composition reload seam.

The interface must expose what is effective and where it came from, while
leaving provider capability, permission, sandbox, and runtime composition
decisions with their existing owners.

## Decision

### Navigation and provenance

Settings are grouped into Appearance, Models & Connection, Agent, Web,
Development, Permissions & Security, and Advanced. Normal forms show the
effective value with one provenance label: Default, User, Project, or CLI.
Explicit CLI values are visible and cannot be misrepresented as saved
preferences. The advanced overview continues to omit verification-command
contents.

The user scope applies to all projects. The Project scope is the existing
workspace-keyed preference scope stored under the user's state directory. It
does not use SessionProject identity and never writes preferences into a code
repository. Optional fields are additive within the existing version-1 JSON
format; no database or preference-schema migration is required.

### Web Search choices

The normal form provides Off, Auto, and Custom. Auto delegates to the existing
capability-aware runtime resolver. Custom persists both the custom mode and an
exact provider profile. The selectable profile list is projected from the
active runtime's credential-free capability inspection and includes only
profiles with an available credential and a trusted executable Search backend.
Compatibility is never guessed from provider names.

When no executable profile exists, Settings shows the typed unavailable reason
and, if the provider settings store is available, a Manage Providers entry.
Saving a custom selection is validated against the current candidate list.
Runtime composition still resolves the route and registers the model-visible
web_search tool through the existing resolver and registry. Runtime denial or
an unavailable route remains fail-closed. Web Fetch keeps the simple Off/Auto
form; Advanced retains all raw routing controls.

The status projection displays effective path, bounded profile/model labels,
and typed reason. It does not display endpoints, credentials, or provider
errors. Tool schemas and their ordering are unchanged.

### Saving and runtime lifetime

Runtime-affecting preference changes use the existing controlled TUI reload
exit code. Before exiting, the TUI captures its current session ID. Bootstrap
closes the old composition, reloads configuration and preferences, and passes
that ID through the established resume flow. The current turn is never replayed.
A running turn blocks a runtime-affecting save before persistence. Interface-only
preferences update the live TUI without rebuilding Runtime.

### Boundaries

Preference composition continues through ApplicationSettings and the bootstrap
composition root. Provider routes, capability checks, credentials, tool
allowlists, permission policy, workspace access, and sandbox policy remain
authoritative. The Settings screen receives only a safe capability projection;
it receives no secret-bearing route adapter or arbitrary state-file access.

## Consequences

- Users can see the active value and its provenance before editing.
- Custom Web Search selects a configured, currently executable route and
  survives a runtime reload without losing the active Session.
- Auto and Custom remain fail-closed when credentials, backend capability, or
  tool permission is absent.
- Existing JSON settings remain readable, and noninteractive CLI/ACP behavior
  is unchanged.
- Provider-specific cache options, a new provider-management workflow, and
  additional Web Fetch modes are outside this decision.

## Verification

Regression coverage checks preference-layer precedence and provenance,
workspace overrides that clear an inherited Search profile, effective-value
forms, Custom profile persistence and reload signaling, unavailable-provider
messaging, session-ID handoff, and the Custom preference's route through
Runtime capability inspection into the web_search tool registry.
