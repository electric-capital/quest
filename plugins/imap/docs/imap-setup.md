# IMAP Setup

How to let users connect IMAP mailboxes for the `plugins/imap` plugin. See [imap-api.md](imap-api.md) for what the integration does and how it is built.

## Requirements

- The Quest server must reach each mail server directly on its IMAP port (993 or 143 unless the admin line says otherwise).
- The server must offer TLS: implicit TLS (`ssl`) or STARTTLS (`starttls`) with a certificate the Quest server's system CA store trusts for that host name, and TLS 1.2 or newer. Plaintext IMAP and certificates the system trust store does not trust (e.g. self-signed) are not supported, and verification cannot be switched off.
- The account must allow password login over IMAP. Providers that require OAuth for IMAP (see below) cannot be used with this plugin.

## Admin: IMAP servers

Settings > Service Credentials > IMAP mailbox (admin only) holds one field, **IMAP servers**: one allowed server per line, `host[:port] [ssl|starttls]`. The connector does not appear in users' Data Connections until the list has at least one valid line. Saving rewrites each line into its canonical form `host:port mode`, which is exactly what users see in their server select.

| Line | Saved as | Notes |
|------|----------|-------|
| `imap.example.com` | `imap.example.com:993 ssl` | Bare host = implicit TLS on 993 |
| `imap.example.com:993` | `imap.example.com:993 ssl` | Duplicate of the line above if both are listed |
| `mail.example.org:143` | `mail.example.org:143 starttls` | Port 143 without a mode means STARTTLS |
| `mail.example.org starttls` | `mail.example.org:143 starttls` | STARTTLS without a port means 143 |
| `mail.example.org:1993` | `mail.example.org:1993 ssl` | Any other port without a mode means implicit TLS |
| `mail.example.org:1143 starttls` | `mail.example.org:1143 starttls` | Explicit port and mode |
| `10.0.0.25`, `[2001:db8::25]:993` | as given, with port and mode added | IP literals; IPv6 must be bracketed |

Rejected lines (the error names the line number): a scheme (`imaps://imap.example.com`), a path, a user name (`user@imap.example.com`), an unbracketed IPv6 address, a mode other than `ssl` / `starttls` (e.g. `plain`; there is no plaintext mode), a port outside 1-65535, a non-ASCII host name (write the `xn--` punycode form), and two lines naming the same host, port and mode. The same host may be listed more than once with different ports or modes.

Common providers:

- **Gmail / Google Workspace**: `imap.gmail.com`. IMAP access must be allowed for the account (for Workspace, also by the Workspace admin). Accounts with 2-Step Verification sign in with an **app password** generated in the Google account's security settings, not the normal password. A Workspace admin may disallow app passwords or require OAuth for IMAP; such accounts cannot use this plugin. Folders appear as `[Gmail]/All Mail`, `[Gmail]/Sent Mail`, and so on.
- **Fastmail**: `imap.fastmail.com`. Fastmail requires an **app password** with IMAP access, created in Fastmail's privacy and security settings.
- **iCloud Mail**: `imap.mail.me.com`. Requires an app-specific password created at account.apple.com.
- **Microsoft 365 / Outlook.com** (`outlook.office365.com`): Microsoft has turned off basic (password) authentication for IMAP in Exchange Online and, as far as we know, for personal Outlook.com accounts as well, so a password login is expected to fail with "Login failed". For Microsoft 365 work accounts in the deployment's tenant, use the Microsoft 365 plugin ([m365-mail-api.md](../../m365/docs/m365-mail-api.md)), which uses OAuth through a single-tenant Entra registration. Personal Outlook.com mailboxes are outside that tenant, so Quest likely has no supported way to read them.
- **Company mail servers** (Dovecot, Cyrus, Exchange on-premises with IMAP enabled, ...): list the host the users' mail clients use; add a `starttls` line if the server only offers STARTTLS on 143.

Removing a line takes effect on the next tool call: every connection that uses it stops working (`imap_host_not_allowed`) and its Data Connections card shows "Server no longer allowed". The connections are not deleted; putting the same server configuration back (in any spelling that canonicalises to the same line) makes them work again.

## User: adding mailboxes

Settings > Data Connections > "+ Add Connection" > **IMAP mailbox**:

1. **IMAP server**: pick one of the admin's lines.
2. **Username**: usually the full email address. If it has no `@`, the connection's account becomes `username@host`.
3. **Password**: the account password, or the provider's app password.
4. Optional description, e.g. "work inbox" or "receipts only".

**Connect** logs in live before anything is stored, so a wrong password or an unreachable server is reported immediately and nothing is saved. The stored password is encrypted and never shown again.

Each mailbox is its own card, named by its account address (lower-case). A user can add up to ten. The first one becomes the **Default** (deleting the default promotes another); **Make default** on another card changes it. Descriptions can be edited inline on the card ("Add description").

- The model uses the default mailbox unless it passes a `connection` argument naming another account. With two or more mailboxes, the model's instructions list every account with its description, so a description such as "personal" or "client X" helps it pick the right one; it may also ask.
- **Reconnect** re-enters the password only (for example after rotating an app password) and logs in again before saving.
- To move a mailbox to a different server, or re-add one after its server was removed and re-listed under another line, **Disconnect** the card and add it again: adding an account that already exists is refused with "This account is already connected." and a **Show it** button that jumps to the existing card.

## Troubleshooting

Connect / Reconnect errors (the form shows the message):

- *"Login failed: ..."*: the server rejected the credentials. Check the username, use an app password where the provider requires one, and check that IMAP is enabled for the account. Exchange Online answers this way for password logins (see above).
- *"Login failed: The username or password contains non-ASCII characters and the server does not offer AUTHENTICATE PLAIN."* / *"Login failed: The server disables LOGIN and does not offer AUTHENTICATE PLAIN."*: the server offers no login method Quest can use with these credentials.
- *"Could not connect to host:port: ..."*: DNS, routing or firewall problem, or a TLS failure on an `ssl` line. *"certificate verify failed: ..."* in the message means the server's certificate is not trusted for that host name (self-signed, expired, or the line names a different host than the certificate). *"STARTTLS failed with host:port: ..."* is the same for a `starttls` line.
- *"host:port does not offer STARTTLS; plaintext IMAP is not supported."*: the admin line says `starttls` but the server does not advertise it; ask the admin to list the server's implicit-TLS port instead.
- *"The server did not answer in time."* (18 s): the server accepted no connection or stalled; often a firewall dropping the port, or a `ssl` / `starttls` mode that does not match the port.
- *"Server not allowed"* or *"Not one of the allowed choices."*: the admin list changed while the form was open; reload Settings.
- *"This username cannot be used as an account name (...)"*: the resulting account would be empty, longer than 320 characters, or contain whitespace, control characters, a double quote or a backtick.
- *"Too many attempts; try again in N seconds."*: more than 10 connect attempts in 60 seconds (N comes from the server's `Retry-After`); the limit is per user and protects the mail server against password guessing through Quest.

Tool errors the model reports in chat:

- `imap_host_not_allowed` / card shows "Server no longer allowed": the admin removed the server's line. Re-add the line, or add the mailbox again with a listed server. **Reconnect** on such a card fails with "Some fields need attention. server: Not one of the allowed choices."; **Disconnect** it and add it again instead.
- `imap_auth_failed`: the stored password stopped working (changed, or the app password was revoked). Use **Reconnect** with the new password.
- `imap_connect_failed` / `imap_timeout`: the server was unreachable or slow for this call (20 s per network operation, 60 s per tool call). Retrying later usually works; persistent failures point at network or certificate changes on the server.
- `connection_not_found`: the model named an account that is not connected; the error lists the existing ones.
