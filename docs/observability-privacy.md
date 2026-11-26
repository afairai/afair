# Content-free error diagnostics

`configure_logging()` applies the privacy processor after exception and stack
formatting, immediately before both JSON and console rendering. Short strings
are as private as long ones: truncation is not redaction. Free-form strings,
provider error objects, exception messages, request identifiers, vault event and
entity identifiers, content hashes, credentials, prompts and response bodies are
masked. The same policy descends into mappings and nested sequences and refuses
to stringify arbitrary objects. Recursion is bounded.

Keep diagnostic event names, logger names, worker names, component names and stage
names code-owned: never interpolate user content into them. These bounded symbol
fields, known status values, numbers, booleans and nulls remain available for
incident triage. Fields with sensitive names are always masked, regardless of
value type. Unknown string fields fail closed. Do not use user content as a
mapping key or encode content as numeric diagnostics.

Sentry is opt-in through `SENTRY_DSN`; an unset DSN sends nothing. With it set,
only errors are enabled. Performance tracing is disabled because provider spans
and transaction descriptions may contain vault data. The final error hook rebuilds
the envelope from permitted fields, masks messages, log parameters, fingerprints,
request and user data, recursively sanitizes contexts, extras, tags and breadcrumbs,
and drops unknown SDK extension fields. Exception class/module and source function,
module, line and column survive; exception values, frame locals, source snippets,
filenames and absolute paths do not. SDK event ids identify incidents, not vault
memories. Breadcrumbs are sanitized both at capture and again at error dispatch.
The narrow `uvicorn.error` client-disconnect noise filter runs before sanitization.

Investigate with code-owned event names, failure classes, source locations, status
codes, counts and timings. Inspect private evidence through the authenticated
vault, never by attaching it to an error report. This boundary covers configured
structlog output and Sentry reports; it does not make direct `print`, arbitrary
standard-library logging handlers or external provider logging safe. Never send
vault data to those paths.

Regression tests exercise the configured production and console processor chains,
formatted provider exceptions, incident fields and nested sequences using synthetic
sentinels only. No live vault, provider credentials or Sentry connection is needed.
