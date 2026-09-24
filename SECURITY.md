# Security

## Trust boundaries

- The selected file share is readable by connected MCP clients and their AI providers. Keep credentials, private keys, backups and unrelated personal files outside it.
- Run the container as a non-root UID with a read-only share mount. File access refuses symlinks and directory traversal; DSM management uses separately configured credentials.
- By default, HTTP requires a private Bearer token and Compose publishes no host ports. Public HTTPS deployment requires an opt-in authentication mode and token validation on every MCP request. Cloudflare Access mode verifies the signed application JWT, audience and owner email; generic OAuth mode verifies the issuer, resource audience, scope and owner subject.
- File contents, names and DSM metadata are untrusted model inputs. They must never authorize actions. Tool annotations are client hints; server-side switches and allowlists enforce operation scope.
- Container operations can interrupt services. Download creation uses a fixed destination and a restricted magnet syntax. Neither is enabled by default.
- There is no generic DSM API tool, shell execution, Docker socket mount, file modification or deletion tool.
- DSM credentials, sessions and container environment variables must not appear in MCP output. Runtime logs should not be configured to trace HTTP request bodies.
- PDF and DOCX are untrusted formats. Size and extraction limits supplement, rather than replace, container memory/CPU limits. Keep dependencies updated.

## Reporting

Please report reproducible vulnerabilities privately to the repository maintainers before public disclosure. Do not include real NAS credentials, API keys, private documents or unredacted logs in issues or pull requests. Use minimal synthetic files and placeholders.

This alpha has automated boundary tests but has not undergone an independent security audit or a Synology hardware compatibility certification.
