/**
 * `hivemind admin provision-user <handle> <email>` — admin-only provisioning.
 *
 * Creates the per-member folder `<hive>/<handle>/` via the Google Drive API and
 * grants the member write access to it (everyone else keeps read-only via the
 * hive-wide share). Authorship in Hivemind is derived from folder ownership, so
 * provisioning is exactly: create folder + share it writer to the member.
 *
 * Requires an OAuth access token with Drive scope, supplied via --token or the
 * GOOGLE_OAUTH_ACCESS_TOKEN environment variable. No token is ever written to disk.
 */
import type { Command } from 'commander';
import { ConfigError, loadResolvedConfig, validateHandle } from '../config.js';

const DRIVE_FILES = 'https://www.googleapis.com/drive/v3/files';
const FOLDER_MIME = 'application/vnd.google-apps.folder';
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

interface DriveFile {
  id: string;
  name: string;
}

async function driveRequest<T>(
  token: string,
  url: string,
  init: RequestInit,
): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Drive API ${res.status} ${res.statusText}: ${text}`);
  }
  return (await res.json()) as T;
}

async function createFolder(token: string, name: string, parentId: string): Promise<DriveFile> {
  return driveRequest<DriveFile>(token, `${DRIVE_FILES}?fields=id,name`, {
    method: 'POST',
    body: JSON.stringify({ name, mimeType: FOLDER_MIME, parents: [parentId] }),
  });
}

async function shareFolder(token: string, fileId: string, email: string): Promise<void> {
  await driveRequest<unknown>(
    token,
    `${DRIVE_FILES}/${fileId}/permissions?sendNotificationEmail=true`,
    {
      method: 'POST',
      body: JSON.stringify({ type: 'user', role: 'writer', emailAddress: email }),
    },
  );
}

export function register(program: Command): void {
  const admin = program.command('admin').description('Admin-only provisioning commands');

  admin
    .command('provision-user <handle> <email>')
    .description('Create <hive>/<handle>/ in Drive and grant the member write access')
    .option('--token <token>', 'Google OAuth access token (Drive scope)')
    .action(async (handle: string, email: string, opts: { token?: string }) => {
      let folderId: string;
      try {
        const cfg = loadResolvedConfig();
        if (!cfg.folderId) {
          throw new ConfigError(
            'admin provision-user needs the Drive folder id — set gdrive_folder_url in ' +
              '~/.hivemind.conf (add it even for the gdrive_desktop backend to provision members).',
          );
        }
        folderId = cfg.folderId;
        validateHandle(handle);
      } catch (err) {
        if (err instanceof ConfigError) {
          console.error(`config error: ${err.message}`);
          process.exitCode = 1;
          return;
        }
        throw err;
      }

      if (!EMAIL_RE.test(email)) {
        console.error(`invalid email: ${email}`);
        process.exitCode = 1;
        return;
      }

      const token = opts.token ?? process.env.GOOGLE_OAUTH_ACCESS_TOKEN;
      if (!token) {
        console.error(
          'no Google OAuth access token — pass --token <token> or set ' +
            'GOOGLE_OAUTH_ACCESS_TOKEN (needs Drive scope).',
        );
        process.exitCode = 1;
        return;
      }

      try {
        console.log(`Creating folder "${handle}" under hive folder ${folderId}...`);
        const folder = await createFolder(token, handle, folderId);
        console.log(`Created folder id=${folder.id}`);
        console.log(`Granting write access to ${email}...`);
        await shareFolder(token, folder.id, email);
        console.log(`Provisioned ${handle} (${email}) -> folder ${folder.id}`);
        console.log(`Remember to add "${handle}" to the roster (conf or admin roster.json).`);
      } catch (err) {
        console.error(`provisioning failed: ${(err as Error).message}`);
        process.exitCode = 1;
      }
    });
}
