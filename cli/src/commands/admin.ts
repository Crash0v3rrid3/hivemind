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
  appProperties?: { hivemindHandle?: string; hivemindMemberEmail?: string };
}

async function driveRequest<T>(token: string, url: string, init: RequestInit): Promise<T> {
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

async function createFolder(
  token: string,
  name: string,
  parentId: string,
  email: string,
): Promise<DriveFile> {
  return driveRequest<DriveFile>(token, `${DRIVE_FILES}?fields=id,name`, {
    method: 'POST',
    body: JSON.stringify({
      name,
      mimeType: FOLDER_MIME,
      parents: [parentId],
      appProperties: { hivemindHandle: name, hivemindMemberEmail: email.toLowerCase() },
    }),
  });
}

interface DrivePermission {
  id: string;
  type: string;
  role: string;
  emailAddress?: string;
  permissionDetails?: { inherited?: boolean }[];
}

async function listAll<T>(token: string, url: URL, key: string): Promise<T[]> {
  const items: T[] = [];
  let nextPage: string | undefined;
  do {
    const page = await driveRequest<Record<string, unknown>>(token, url.toString(), {
      method: 'GET',
    });
    items.push(...((page[key] as T[]) ?? []));
    nextPage = page.nextPageToken as string | undefined;
    if (nextPage) url.searchParams.set('pageToken', nextPage);
  } while (nextPage);
  return items;
}

/** Reuse a unique author folder and reconcile its writer permission on retries. */
export async function provisionUser(
  token: string,
  parentId: string,
  handle: string,
  email: string,
): Promise<DriveFile> {
  validateHandle(handle);
  if (!/^[A-Za-z0-9_-]+$/.test(parentId)) throw new Error('Invalid parent folder id');
  const query = new URL(DRIVE_FILES);
  query.searchParams.set(
    'q',
    `'${parentId}' in parents and name = '${handle}' and mimeType = '${FOLDER_MIME}' and trashed = false`,
  );
  query.searchParams.set('fields', 'nextPageToken,files(id,name,appProperties)');
  const folders = await listAll<DriveFile>(token, query, 'files');
  if (folders.length > 1)
    throw new Error(
      `Ambiguous author folder "${handle}": found ${folders.length}; resolve duplicates before provisioning`,
    );
  const folder = folders[0] ?? (await createFolder(token, handle, parentId, email));
  const binding = folder.appProperties;
  if (
    (binding?.hivemindHandle && binding.hivemindHandle !== handle) ||
    (binding?.hivemindMemberEmail && binding.hivemindMemberEmail !== email.toLowerCase())
  ) {
    throw new Error(
      `Folder ${folder.id} is bound to another member; refuse to reassign author identity`,
    );
  }
  const url = `${DRIVE_FILES}/${encodeURIComponent(folder.id)}/permissions`;
  const permissionsUrl = new URL(url);
  permissionsUrl.searchParams.set(
    'fields',
    'nextPageToken,permissions(id,type,role,emailAddress,permissionDetails)',
  );
  const permissions = await listAll<DrivePermission>(token, permissionsUrl, 'permissions');
  const conflictingWriter = permissions.find(
    (p) =>
      ['writer', 'organizer', 'fileOrganizer'].includes(p.role) &&
      (p.type !== 'user' || p.emailAddress?.toLowerCase() !== email.toLowerCase()),
  );
  if (conflictingWriter) {
    if (conflictingWriter.permissionDetails?.some((detail) => detail.inherited)) {
      throw new Error(
        `Folder ${folder.id} inherits a different writer; remediate the parent folder ACL before provisioning`,
      );
    }
    throw new Error(
      `Folder ${folder.id} already has a different writer; resolve author ownership before provisioning`,
    );
  }
  const permission = permissions.find(
    (p) => p.type === 'user' && p.emailAddress?.toLowerCase() === email.toLowerCase(),
  );
  if (permission && ['writer', 'owner', 'organizer', 'fileOrganizer'].includes(permission.role))
    return folder;
  await driveRequest<unknown>(
    token,
    permission
      ? `${url}/${encodeURIComponent(permission.id)}`
      : `${url}?sendNotificationEmail=true`,
    {
      method: permission ? 'PATCH' : 'POST',
      body: JSON.stringify(
        permission ? { role: 'writer' } : { type: 'user', role: 'writer', emailAddress: email },
      ),
    },
  );
  return folder;
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
        console.log(`Provisioning folder "${handle}" under hive folder ${folderId}...`);
        const folder = await provisionUser(token, folderId, handle, email);
        console.log(`Provisioned ${handle} (${email}) -> folder ${folder.id}`);
        console.log(`Remember to add "${handle}" to the roster (conf or admin roster.json).`);
      } catch (err) {
        console.error(`provisioning failed: ${(err as Error).message}`);
        process.exitCode = 1;
      }
    });
}
