/**
 * Ready-made MCP servers.
 *
 * Adding one by hand means knowing an npm package name, an argument list and
 * the exact spelling of four environment variables — which is a research
 * task, not a setup step. Each entry here fills the form and says, in the
 * words of the service's own settings pages, where to get the key.
 *
 * The Gmail entry is the reason `oauthFile` exists: the OAuth keys file has
 * to land in a directory Faustus controls (`gmail/gcp-oauth.keys.json`),
 * never `~/.gmail-mcp`, so a container or another user cannot read it. The
 * server builds the file from the client id and secret; nothing is uploaded.
 *
 * `providerDropdown` is the second shape: one package, several hosts, and a
 * choice that fills two fields at once — the IMAP/SMTP pair nobody remembers.
 */

export interface OauthFile {
  dir: string;
  filename: string;
}

export interface OauthConfig {
  provider: string;
  keys_file: string;
  token_file: string;
  scopes: string[];
}

export interface ProviderChoice {
  name: string;
  values: Record<string, string>;
}

export interface ProviderDropdown {
  label: string;
  options: ProviderChoice[];
}

export interface McpPreset {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  oauthFile?: OauthFile;
  oauth?: OauthConfig;
  providerDropdown?: ProviderDropdown;
  help?: string;
}

export const MCP_PRESETS: McpPreset[] = [
  {
    name: 'Gmail',
    command: 'npx',
    args: ['-y', '@gongrzhe/server-gmail-autoauth-mcp'],
    env: { GOOGLE_CLIENT_ID: '', GOOGLE_CLIENT_SECRET: '' },
    oauthFile: { dir: 'gmail', filename: 'gcp-oauth.keys.json' },
    oauth: {
      provider: 'google',
      keys_file: 'gmail/gcp-oauth.keys.json',
      token_file: 'gmail/credentials.json',
      scopes: ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/gmail.settings.basic'],
    },
    help: `Setup:
1. Go to console.cloud.google.com > create or select a project
2. APIs & Services > Library > search "Gmail API" > Enable
3. APIs & Services > OAuth consent screen > set up (External is fine)
4. Under Audience, add your Gmail address as a test user
5. APIs & Services > Credentials > + Create Credentials > OAuth Client ID
6. Application type: Desktop App > Create
7. Copy the Client ID and Client Secret into the fields above
8. Click Add the server, then click Authorise
9. Sign in with Google, copy the URL from the error page, paste it back`,
  },
  {
    name: 'Email (IMAP/SMTP)',
    command: 'npx',
    args: ['-y', '@codefuturist/email-mcp', 'stdio'],
    env: { MCP_EMAIL_ADDRESS: '', MCP_EMAIL_PASSWORD: '', MCP_EMAIL_IMAP_HOST: '', MCP_EMAIL_SMTP_HOST: '' },
    providerDropdown: {
      label: 'Provider',
      options: [
        { name: 'Migadu', values: { MCP_EMAIL_IMAP_HOST: 'imap.migadu.com', MCP_EMAIL_SMTP_HOST: 'smtp.migadu.com' } },
        { name: 'Fastmail', values: { MCP_EMAIL_IMAP_HOST: 'imap.fastmail.com', MCP_EMAIL_SMTP_HOST: 'smtp.fastmail.com' } },
        { name: 'Proton Bridge', values: { MCP_EMAIL_IMAP_HOST: '127.0.0.1', MCP_EMAIL_SMTP_HOST: '127.0.0.1' } },
        { name: 'Outlook/Hotmail', values: { MCP_EMAIL_IMAP_HOST: 'outlook.office365.com', MCP_EMAIL_SMTP_HOST: 'smtp.office365.com' } },
        { name: 'Yahoo', values: { MCP_EMAIL_IMAP_HOST: 'imap.mail.yahoo.com', MCP_EMAIL_SMTP_HOST: 'smtp.mail.yahoo.com' } },
        { name: 'iCloud', values: { MCP_EMAIL_IMAP_HOST: 'imap.mail.me.com', MCP_EMAIL_SMTP_HOST: 'smtp.mail.me.com' } },
        { name: 'Zoho', values: { MCP_EMAIL_IMAP_HOST: 'imap.zoho.com', MCP_EMAIL_SMTP_HOST: 'smtp.zoho.com' } },
        { name: 'Custom', values: { MCP_EMAIL_IMAP_HOST: '', MCP_EMAIL_SMTP_HOST: '' } },
      ],
    },
    help: 'Works with any IMAP/SMTP email provider.\n1. Pick your provider from the dropdown (or choose Custom)\n2. Enter your email address and password (or app password)\n3. Click Add the server',
  },
  {
    name: 'CalDAV (Radicale/Nextcloud)',
    command: 'npx',
    args: ['-y', 'caldav-mcp'],
    env: { CALDAV_BASE_URL: 'http://localhost:5232', CALDAV_USERNAME: '', CALDAV_PASSWORD: '' },
    help: 'Works with any CalDAV server (Radicale, Nextcloud, etc.).\n1. Enter your CalDAV server URL (e.g. http://localhost:5232)\n2. Enter your username and password\n3. Click Add the server',
  },
  {
    name: 'Google Calendar',
    command: 'npx',
    args: ['-y', '@cocal/google-calendar-mcp'],
    env: { GOOGLE_OAUTH_CREDENTIALS: '' },
    help: `Setup:
1. Go to console.cloud.google.com > create/select a project
2. APIs & Services > Library > enable Google Calendar API
3. APIs & Services > Credentials > + Create Credentials > OAuth Client ID
4. Application type: Desktop App > Create
5. Click "Download JSON" on the credential you just created
6. Set GOOGLE_OAUTH_CREDENTIALS to the full path of the downloaded JSON file`,
  },
  {
    name: 'Google Drive',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-gdrive'],
    env: {},
    help: 'Google Drive uses browser-based OAuth on first run. No environment variables needed — add it and authorise when prompted.',
  },
  {
    name: 'GitHub',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-github'],
    env: { GITHUB_PERSONAL_ACCESS_TOKEN: '' },
    help: '1. Go to github.com > Settings > Developer Settings > Personal Access Tokens > Fine-grained tokens\n2. Generate a new token with the repo permissions you need\n3. Paste it as GITHUB_PERSONAL_ACCESS_TOKEN',
  },
  {
    name: 'Slack',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-slack'],
    env: { SLACK_BOT_TOKEN: '', SLACK_TEAM_ID: '' },
    help: '1. Go to api.slack.com/apps > Create New App > From Scratch\n2. Add Bot Token Scopes (channels:read, chat:write, etc.)\n3. Install to workspace, copy the Bot User OAuth Token (xoxb-…)\n4. The team ID is in your workspace URL or Slack admin settings',
  },
  {
    name: 'Notion',
    command: 'npx',
    args: ['-y', '@notionhq/notion-mcp-server'],
    env: { OPENAPI_MCP_HEADERS: '' },
    help: '1. Go to notion.so/my-integrations\n2. Create a new integration\n3. Copy the Internal Integration Secret\n4. Share the Notion pages/databases you want reachable with the integration\n5. For OPENAPI_MCP_HEADERS enter:\n   {"Authorization": "Bearer YOUR_SECRET", "Notion-Version": "2022-06-28"}',
  },
  {
    name: 'Linear',
    command: 'npx',
    args: ['-y', 'mcp-linear'],
    env: { LINEAR_API_KEY: '' },
    help: '1. Go to linear.app > Settings > API\n2. Create a Personal API Key\n3. Paste it as LINEAR_API_KEY',
  },
  {
    name: 'Brave Search',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-brave-search'],
    env: { BRAVE_API_KEY: '' },
    help: '1. Go to brave.com/search/api\n2. Sign up for a free plan (2000 queries a month)\n3. Copy your API key',
  },
  {
    name: 'Browser (Playwright)',
    command: 'npx',
    args: ['-y', '@playwright/mcp@latest', '--headless'],
    env: {},
    help: 'Browser automation via Playwright: navigate, click, fill forms and read pages.\nHeadless by default — remove --headless from the arguments to watch it work.\nThe first run installs Chromium on its own.',
  },
  {
    name: 'Filesystem',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-filesystem', '/home'],
    env: {},
    help: 'Change the last argument to the directory the server should have access to. It is the only one it can reach.',
  },
  { name: 'Memory', command: 'npx', args: ['-y', '@modelcontextprotocol/server-memory'], env: {} },
  {
    name: 'Postgres',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-postgres', 'postgresql://user:pass@localhost/db'],
    env: {},
    help: 'Replace the connection string in the arguments with your own.',
  },
  {
    name: 'Todoist',
    command: 'npx',
    args: ['-y', 'todoist-mcp-server'],
    env: { TODOIST_API_TOKEN: '' },
    help: '1. Go to todoist.com > Settings > Integrations > Developer\n2. Copy your API token',
  },
];

export function presetByName(name: string): McpPreset | undefined {
  return MCP_PRESETS.find((p) => p.name === name);
}

/** The form fields a preset fills, as the panel holds them (JSON strings). */
export function fieldsFor(preset: McpPreset): { name: string; command: string; args: string; env: string } {
  return {
    name: preset.name,
    command: preset.command,
    args: JSON.stringify(preset.args),
    env: JSON.stringify(preset.env, null, 0),
  };
}

/**
 * Apply a provider choice to the env JSON the form holds.
 *
 * Returns the env unchanged when it is not valid JSON: the user may be
 * mid-edit, and silently rewriting what they typed is worse than doing
 * nothing.
 */
export function withProvider(envJson: string, choice: ProviderChoice): string {
  let env: Record<string, unknown>;
  try {
    env = JSON.parse(envJson || '{}') as Record<string, unknown>;
    if (!env || typeof env !== 'object' || Array.isArray(env)) return envJson;
  } catch {
    return envJson;
  }
  return JSON.stringify({ ...env, ...choice.values }, null, 0);
}

/**
 * The `oauth_file` payload the add-server route expects: the directory and
 * filename from the preset, plus the client id and secret the user typed
 * into the env fields. The route writes the credentials file itself — the
 * secret never becomes a file on this side.
 */
export function oauthFilePayload(preset: McpPreset, envJson: string): string | null {
  if (!preset.oauthFile) return null;
  let env: Record<string, unknown>;
  try {
    env = JSON.parse(envJson || '{}') as Record<string, unknown>;
  } catch {
    return null;
  }
  const clientId = String(env.GOOGLE_CLIENT_ID ?? '').trim();
  const clientSecret = String(env.GOOGLE_CLIENT_SECRET ?? '').trim();
  if (!clientId || !clientSecret) return null;
  return JSON.stringify({ ...preset.oauthFile, client_id: clientId, client_secret: clientSecret });
}
