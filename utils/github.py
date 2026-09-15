import asyncio
import bisect
import hashlib
import os
import re
import aiohttp
import github
import time
from collections import Counter, namedtuple
from github import Github
# NOTE: never import the module-global scoped session here. This module runs in
# long-lived processes (player-updates' github loop, in both the asyncio main
# thread and to_thread workers); a read on the scoped session autobegins a
# transaction that nothing ever commits, which held an idle InnoDB transaction
# (and its metadata locks) open for the entire service lifetime — 20h+ in the
# 2026-07-16 incident. Use short-lived `with Session()` blocks instead.
from db.models import GroupConfiguration, Webhook, NewWebhook, Session
from dotenv import load_dotenv
import json
from utils.encrypter import encrypt_webhook, decrypt_webhook
from utils.plugin_urls import webhook_credentials
from datetime import datetime, timedelta
from db.app_logger import AppLogger
load_dotenv()

app_logger = AppLogger()

total_hooks = 0

updates = []

# Published dated files: {YYYYMMDD}.json / {YYYYMMDD}-1.json (webhook chunks)
# and {YYYYMMDD}-k.txt (encryption key). The updater creates today's and
# tomorrow's on each run and prunes anything older than STALE_AFTER_DAYS —
# clients only ever read today's files, so week-old ones are dead weight that
# previously accumulated forever (800+ files by 2026-07).
DATED_FILE_RE = re.compile(r"^(\d{8})(?:-1)?\.json$|^(\d{8})-k\.txt$")
STALE_AFTER_DAYS = 7


def _git_blob_sha(content: str) -> str:
    """Git blob sha1 for text content — lets deterministic files (item id
    lists, news) be change-compared against a directory listing without
    fetching each file's body."""
    data = content.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def summarize_publish(files_to_update, deletions, webhook_files_changed, webhook_check=None):
    """Human-readable change lines for one publish run, for the automation
    status channel. An empty list means the run changed nothing."""
    changes = []
    if files_to_update:
        names = [path.rsplit("/", 1)[-1] for path, _content in files_to_update]
        shown = ", ".join(names[:6]) + (f", +{len(names) - 6} more" if len(names) > 6 else "")
        changes.append(f"Committed {len(names)} file(s): {shown}")
    if webhook_files_changed:
        changes.append(f"{webhook_files_changed} webhook file(s) rotated")
    if deletions:
        changes.append(f"Pruned {len(deletions)} stale dated file(s)")
    check = webhook_check or {}
    if check.get("degraded"):
        changes.append(
            f"Webhook check untrusted: {check.get('inconclusive', 0)} of {check.get('tested', 0)} probes "
            "inconclusive (Discord or network trouble) — nothing deleted, webhook files unchanged")
    deleted = check.get("deleted", 0)
    if deleted:
        changes.append(f"Deleted {deleted} confirmed-dead webhook(s) (of {check.get('tested', 0)} tested)")
    struck = check.get("struck", 0)
    if struck:
        changes.append(
            f"Flagged {struck} webhook(s) answering Unknown Webhook: unpublished, "
            f"deleted if still dead after {DEAD_CONFIRM_SECONDS // 3600}h")
    return changes


def _stale_dated_paths(paths, today_str: str, keep_days: int = STALE_AFTER_DAYS):
    """The dated content files (see DATED_FILE_RE) more than ``keep_days``
    before ``today_str`` (YYYYMMDD). Non-dated paths are never returned."""
    cutoff = datetime.strptime(today_str, "%Y%m%d") - timedelta(days=keep_days)
    stale = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        match = DATED_FILE_RE.match(name)
        if not match:
            continue
        date_str = match.group(1) or match.group(2)
        try:
            file_date = datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            continue
        if file_date < cutoff:
            stale.append(path)
    return stale

class GithubPagesUpdater:
    def __init__(self):
        """
        Initialize the GitHubPagesUpdater.
        """
        load_dotenv()  # Load environment variables

        # GitHub Token and Repo Info
        self.github_token = os.getenv("GITHUB_TOKEN")  # Load GitHub token from .env
        repo_name = "droptracker-io/droptracker-io.github.io"  # GitHub repository name
        self.new_file = "content/core.json"
        self.branch = "main"
        # Initialize GitHub API
        self.github = Github(self.github_token)
        self.repo = self.github.get_repo(repo_name)

        # Log the repo and file path for verification
        # print(f"Repo: {repo_name}")

    def fetch_webhooks_from_database(self, limit=120, urls=None):
        """
        Fetch the webhook URLs from the database and format them as a list of URLs.

        Args:
            limit: Maximum number of webhooks to fetch
            urls: The exact webhook urls to publish, in order — the liveness
                check's publishable set. Skips the table read.

        Returns:
            list of encrypted webhooks
        """
        try:
            if urls is not None:
                main_urls = list(urls)
            else:
                with Session() as s:
                    # Deterministic order: an unordered LIMIT can shuffle which
                    # rows are picked between runs, which would read as a webhook
                    # "change" and trigger a pointless publish.
                    main_urls = [
                        w.webhook_url
                        for w in s.query(Webhook).order_by(Webhook.webhook_id.asc()).limit(limit).all()
                        if w.webhook_url
                    ]
            main_encrypted = []

            # Try to encrypt each webhook, skipping any that fail
            for url in main_urls:
                try:
                    encrypted = encrypt_webhook(url)
                    main_encrypted.append(encrypted)
                except Exception as e:
                    # Never log the url: it carries the webhook's token.
                    print(f"Failed to encrypt a webhook url: {e}")
            
            if not main_encrypted:
                raise ValueError("No webhooks could be encrypted. Check encryption key configuration.")
            
            return main_encrypted
        except Exception as e:
            print(f"Error fetching webhook URLs from the database: {e}")
            # Check if this is an encryption key error
            if "Fernet key must be 32 url-safe base64-encoded bytes" in str(e):
                print("Encryption key error detected. Attempting to generate a valid key...")
                with Session() as session:
                    # Try to update the encryption key
                    encryption_config = session.query(GroupConfiguration).where(
                        GroupConfiguration.group_id == 2,
                        GroupConfiguration.config_key == "encryption-gh"
                    ).first()
                    
                    if encryption_config:
                        new_key = self._generate_fernet_key()
                        encryption_config.config_value = new_key
                        session.commit()
                        print(f"Updated encryption key to: {new_key}")
                    else:
                        # Create a new encryption key config if it doesn't exist
                        new_key = self._generate_fernet_key()
                        new_config = GroupConfiguration(
                            group_id=2,
                            config_key="encryption-gh",
                            config_value=new_key
                        )
                        session.add(new_config)
                        session.commit()
                        print(f"Created new encryption key: {new_key}")
            
            # Re-raise the exception to be handled by the caller
            raise

    async def update_github_pages(self, watchdog=None):
        """Run one publish cycle; returns human-readable change lines (empty
        list when nothing changed)."""
        global total_hooks

        with Session() as s:
            total_hooks = s.query(Webhook).count()

        # The liveness check decides which webhooks get published and deletes
        # only confirmed-dead rows (see check_pool_webhooks). If it fails or
        # can't be trusted, the published webhook files are left as they are.
        try:
            webhook_check = await check_pool_webhooks()
        except Exception as e:
            print(f"Error checking webhooks: {e}")
            webhook_check = None
        publish_urls = webhook_check.get("publish_urls") if webhook_check else None
        changes = await asyncio.to_thread(self._update_github_pages, publish_urls)
        return (changes or []) + summarize_publish([], [], 0, webhook_check)

    def _webhook_set_changed(self, content_file, new_chunk) -> bool:
        """True when a published webhook file's DECRYPTED url set differs from
        the candidate chunk. Ciphertexts can't be compared directly — Fernet
        re-encryption produces different bytes for identical urls every run,
        which is exactly the bug that used to commit \"changes\" every cycle."""
        if content_file is None:
            return True
        try:
            old_list = json.loads(content_file.decoded_content.decode("utf-8"))
        except Exception:
            return True
        if not isinstance(old_list, list) or len(old_list) != len(new_chunk):
            return True

        def _decrypt_all(encrypted_list):
            out = set()
            for entry in encrypted_list:
                try:
                    out.add(decrypt_webhook(entry))
                except Exception:
                    return None
            return out

        old_set = _decrypt_all(old_list)
        new_set = _decrypt_all(new_chunk)
        return old_set is None or new_set is None or old_set != new_set

    def _item_list_contents(self):
        """The deterministic plugin id-list files: ``valued_items.txt`` (active
        value-override ids — including name-only override rows resolved to ids
        via the items table — always force-screenshotted),
        ``untradeable_items.txt`` (curated notable untradeables, toggle-gated)
        and ``server_loot_npc_ids.txt`` (npcs whose loot RuneLite only reports
        via ServerNpcLoot — see scripts/export_server_loot_npcs.py; publishing
        it means a new server-loot boss no longer needs a plugin release).
        Publishing them here keeps all three in lockstep with the database /
        curated source without a manual content-repo commit."""
        out = []
        try:
            from utils.value_overrides import active_item_ids

            ids = active_item_ids()
            if ids:
                out.append(("content/valued_items.txt", ",".join(str(i) for i in ids)))
        except Exception as e:
            print(f"Failed to build valued_items.txt content: {e}")
        try:
            from scripts.export_untradeable_items import NOTABLE_UNTRADEABLES

            ids = sorted({int(item_id) for item_id, _name in NOTABLE_UNTRADEABLES})
            if ids:
                out.append(("content/untradeable_items.txt", ",".join(str(i) for i in ids)))
        except Exception as e:
            print(f"Failed to build untradeable_items.txt content: {e}")
        try:
            from scripts.export_server_loot_npcs import build_content

            content = build_content()
            if content:
                out.append(("content/server_loot_npc_ids.txt", content))
        except Exception as e:
            print(f"Failed to build server_loot_npc_ids.txt content: {e}")
        return out

    def _update_github_pages(self, publish_urls=None):
        """
        Publish the latest webhook/news/key/item-list content, committing ONLY
        when something actually changed. One ``content/`` listing replaces the
        recursive repo walks the old implementation did on every run, and every
        file is change-gated (decrypt-compare for webhook files, blob-sha
        compare for deterministic text) so a no-change cycle makes zero commits
        and triggers zero GitHub Pages builds. Stale dated files are pruned in
        the same commit.

        ``publish_urls`` is the liveness check's ordered publishable set. None
        (no trusted check this run) leaves the webhook files untouched; the
        other files still publish.

        Returns the ``summarize_publish`` change lines for the run (empty list
        when nothing was committed).
        """
        try:
            listing = {f.path: f for f in self.repo.get_contents("content", ref=self.branch)}
        except github.GithubException as e:
            print(f"Failed to list content/: {e}")
            listing = {}

        files_to_update = []

        news_file = self._prepare_news_update(listing)
        if news_file:
            files_to_update.append(news_file)

        encryption_key_file = self._prepare_encryption_key_update(listing)
        if encryption_key_file:
            files_to_update.append(encryption_key_file)

        encrypted_webhooks = []
        if publish_urls is None:
            print("No trusted webhook check this run; leaving webhook files unchanged.")
        else:
            if publish_urls:
                try:
                    encrypted_webhooks = self.fetch_webhooks_from_database(urls=publish_urls)
                except Exception as e:
                    print(f"Error encrypting webhook URLs: {e}")
            if len(encrypted_webhooks) < 30:
                print("Generated list is too short:", len(encrypted_webhooks), "- leaving webhook files unchanged.")
                encrypted_webhooks = []

        now = datetime.now()
        today_str = now.strftime("%Y%m%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y%m%d")

        webhook_files_changed = 0
        if encrypted_webhooks:
            chunk_size = 40
            webhook_chunks = [encrypted_webhooks[i:i + chunk_size]
                              for i in range(0, len(encrypted_webhooks), chunk_size)]

            # {date}.json is what UrlManager.loadEndpoints reads; tomorrow's copy
            # covers the midnight rollover. {date}-1.json is the replenishment set
            # fetchNewList falls back to when the primary set is failing — it was
            # never published before this rewrite, so that path 404'd.
            primary_chunk = webhook_chunks[1] if len(webhook_chunks) > 1 else webhook_chunks[0]
            targets = {
                "content/core.json": webhook_chunks[0],
                f"content/{today_str}.json": primary_chunk,
                f"content/{tomorrow_str}.json": primary_chunk,
            }
            if len(webhook_chunks) > 2:
                targets[f"content/{today_str}-1.json"] = webhook_chunks[2]
                targets[f"content/{tomorrow_str}-1.json"] = webhook_chunks[2]

            for file_path, chunk in targets.items():
                if self._webhook_set_changed(listing.get(file_path), chunk):
                    files_to_update.append((file_path, json.dumps(chunk, indent=4)))
                    webhook_files_changed += 1

            # Mirror the published set in the database, but only when it moved.
            if webhook_files_changed:
                with Session() as s:
                    s.query(NewWebhook).delete()
                    for webhook_hash in encrypted_webhooks:
                        s.add(NewWebhook(webhook_hash=webhook_hash))
                    s.commit()

        for file_path, content in self._item_list_contents():
            existing = listing.get(file_path)
            if existing is None or existing.sha != _git_blob_sha(content):
                files_to_update.append((file_path, content))

        deletions = _stale_dated_paths(listing.keys(), today_str)

        if not files_to_update and not deletions:
            print("GitHub Pages content unchanged; skipping commit.")
            return []

        print(f"Committing {len(files_to_update)} file update(s)"
              + (f" + {len(deletions)} stale deletion(s)" if deletions else "")
              + f" ({webhook_files_changed} webhook file(s) changed).")
        self.update_multiple_files(
            files_to_update,
            commit_message="Update published content (webhooks/news/key/item lists).",
            branch=self.branch,
            deletions=deletions,
        )
        return summarize_publish(files_to_update, deletions, webhook_files_changed)

    def _prepare_news_update(self, listing=None):
        """
        Prepare the news file update but don't commit it yet.
        Returns a tuple of (file_path, content) if an update is needed, None otherwise.
        ``listing`` is the pre-fetched ``content/`` directory dict; change
        detection compares blob shas so no per-file fetch is needed.
        """
        with Session() as session:
            current_news_data = session.query(GroupConfiguration).where(GroupConfiguration.group_id == 2,
                                                                        GroupConfiguration.config_key == "news-gh").first()
            if current_news_data:
                news_content = f"{current_news_data.config_value}" if current_news_data.config_value and current_news_data.config_value != "" else current_news_data.long_value if current_news_data.long_value and current_news_data.long_value != "" else ""
                news_file_path = "content/news.txt"

                existing = (listing or {}).get(news_file_path)
                if listing is None:
                    try:
                        existing = self.repo.get_contents(news_file_path, ref=self.branch)
                    except github.GithubException as e:
                        if e.status != 404:
                            print(f"Error checking news file: {e}")
                            return None
                        existing = None
                if existing is None or existing.sha != _git_blob_sha(news_content):
                    print(f"News content has changed. Updating {news_file_path}")
                    return (news_file_path, news_content)
                return None
        return None

    def _prepare_encryption_key_update(self, listing=None):
        """
        Prepare the encryption key file update but don't commit it yet.
        Returns a tuple of (file_path, content) if an update is needed, None otherwise.
        ``listing`` is the pre-fetched ``content/`` directory dict (existence
        checks only — key files are never rewritten once created).
        """
        with Session() as session:
            current_encryption_key = session.query(GroupConfiguration).where(GroupConfiguration.group_id == 2,
                                                                            GroupConfiguration.config_key == "encryption-gh").first()
            if current_encryption_key:
                # Get current date and tomorrow's date
                current_date = datetime.now()
                tomorrow_date = current_date + timedelta(days=1)
                
                # Format dates for filenames
                current_date_str = current_date.strftime("%Y%m%d")
                tomorrow_date_str = tomorrow_date.strftime("%Y%m%d")
                
                # Create paths for both dates
                current_key_file = f"content/{current_date_str}-k.txt"
                tomorrow_key_file = f"content/{tomorrow_date_str}-k.txt"
                
                encryption_key_content = current_encryption_key.config_value
                
                # Validate the encryption key format
                if not self._is_valid_fernet_key(encryption_key_content):
                    # Generate a new valid key if the current one is invalid
                    new_key = self._generate_fernet_key()
                    print(f"Invalid encryption key detected. Generated new key: {new_key}")
                    
                    # Update the key in the database
                    current_encryption_key.config_value = new_key
                    session.commit()
                    
                    encryption_key_content = new_key
                
                def _exists(path):
                    if listing is not None:
                        return path in listing
                    try:
                        self.repo.get_contents(path, ref=self.branch)
                        return True
                    except github.GithubException as e:
                        if e.status == 404:
                            return False
                        raise

                try:
                    if not _exists(current_key_file):
                        print(f"Creating today's encryption key file: {current_key_file}")
                        return (current_key_file, encryption_key_content)
                    if not _exists(tomorrow_key_file):
                        print(f"Creating tomorrow's encryption key file: {tomorrow_key_file}")
                        return (tomorrow_key_file, encryption_key_content)
                except github.GithubException as e:
                    print(f"Error checking encryption key file: {e}")
                    return None
        return None

    def _is_valid_fernet_key(self, key):
        """
        Check if a key is a valid Fernet key (32 url-safe base64-encoded bytes).
        
        :param key: The key to validate
        :return: True if valid, False otherwise
        """
        import base64
        try:
            # A valid Fernet key is 32 bytes, base64-encoded
            decoded = base64.urlsafe_b64decode(key.encode('utf-8') + b'=' * (4 - len(key) % 4))
            return len(decoded) == 32
        except Exception:
            return False

    def _generate_fernet_key(self):
        """
        Generate a valid Fernet key (32 url-safe base64-encoded bytes).
        
        :return: A valid Fernet key as a string
        """
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode('utf-8')

    def update_news(self):
        """
        Update the news.txt file in the content directory.
        This is a standalone method that can be called independently.
        """
        news_file = self._prepare_news_update()
        if news_file:
            self.update_file(news_file[0], news_file[1])

    def update_encryption_key(self):
        """
        Updates the current encryption key.
        This is a standalone method that can be called independently.
        """
        encryption_key_file = self._prepare_encryption_key_update()
        if encryption_key_file:
            self.update_file(encryption_key_file[0], encryption_key_file[1])

    def update_file(self, file_path, new_content):
        """
        Update a single file in the repository, but only if the content has changed.
        
        :param file_path: Path to the file in the repository
        :param new_content: New content for the file
        :return: True if the file was updated, False otherwise
        """
        try:
            # Check if file exists
            try:
                file = self.repo.get_contents(file_path, ref=self.branch)
                exists = True
                old_content = file.decoded_content.decode('utf-8')
            except github.GithubException as e:
                if e.status == 404:
                    exists = False
                    old_content = ""
                else:
                    raise
            
            # Only update if content has changed
            if not exists or old_content != new_content:
                print(f"Updating file: {file_path}")
                
                if exists:
                    # Update existing file
                    self.repo.update_file(
                        path=file_path,
                        message=f"Update {file_path}",
                        content=new_content,
                        sha=file.sha,
                        branch=self.branch
                    )
                else:
                    # Create new file
                    self.repo.create_file(
                        path=file_path,
                        message=f"Create {file_path}",
                        content=new_content,
                        branch=self.branch
                    )
                return True
            else:
                print(f"No changes detected for {file_path}. Skipping update.")
                return False
            
        except github.GithubException as e:
            print(f"Failed to update file {file_path}: {e}")
            return False
        except Exception as e:
            print(f"Unexpected error updating {file_path}: {e}")
            return False

    def update_multiple_files(self, files_to_update, commit_message, branch="main", deletions=None):
        """
        Update multiple files in a single commit to avoid multiple GitHub Pages builds.

        :param files_to_update: List of tuples (file_path, new_content)
        :param commit_message: Commit message for the update
        :param branch: Branch to update (default: main)
        :param deletions: Optional list of file paths to delete in the same commit
        """
        repo = self.repo

        # 1. Get the latest commit and tree
        ref = repo.get_git_ref(f"heads/{branch}")
        latest_commit = repo.get_git_commit(ref.object.sha)
        base_tree = repo.get_git_tree(latest_commit.tree.sha)

        # 2. Create blobs for each file (sha=None in a tree element deletes the path)
        element_list = []
        for file_path, new_content in files_to_update:
            blob = repo.create_git_blob(new_content, "utf-8")
            element = github.InputGitTreeElement(
                path=file_path,
                mode="100644",
                type="blob",
                sha=blob.sha
            )
            element_list.append(element)
        for file_path in (deletions or []):
            element_list.append(github.InputGitTreeElement(
                path=file_path,
                mode="100644",
                type="blob",
                sha=None
            ))
        if not element_list:
            return

        # 3. Create a new tree
        new_tree = repo.create_git_tree(element_list, base_tree)

        # 4. Create a new commit
        new_commit = repo.create_git_commit(commit_message, new_tree, [latest_commit])

        # 5. Update the branch reference
        ref.edit(new_commit.sha)


# --- Webhook liveness ---------------------------------------------------------
# Every plugin on the Discord-webhook transport posts to the published set, so a
# wrongly deleted row takes a working webhook out of circulation. The check that
# lived here deleted on ANY non-2xx/3xx answer or aiohttp error, and only ever
# probed the first 120 rows. On 2026-09-15 it ran inside Discord's "Session
# Unavailability" incident and deleted all 120 published webhooks — every one
# still answered 200 afterwards — while 599 long-dead rows sat untested behind
# them. The rules now:
#   * Only Discord's own "Unknown Webhook" answer (404, JSON code 10015) means
#     dead. 429s, 5xx, timeouts, connection errors and anything else are
#     inconclusive.
#   * A run with too many inconclusive probes is Discord or network trouble, not
#     dead webhooks: it deletes nothing, records nothing and leaves the published
#     webhook files alone.
#   * A dead answer strikes the row, which stops it being published. The row is
#     deleted only when a probe at least DEAD_CONFIRM_SECONDS later is still
#     dead, so an incident answering 10015 for live webhooks can't delete them
#     unless it outlasts that window. A live answer clears the strike.
#   * Besides the rows being published, every run probes the next
#     ROTATION_BATCH rows of the table, so dead rows can't hide behind live ones.

UNKNOWN_WEBHOOK_CODE = 10015
PUBLISH_COUNT = 120
MAX_PUBLISH_PROBES = 360
ROTATION_BATCH = 60
DEAD_CONFIRM_SECONDS = 6 * 60 * 60
INCONCLUSIVE_MIN = 5
INCONCLUSIVE_RATIO = 0.10
PROBE_DELAY_SECONDS = 0.25
PROBE_TIMEOUT_SECONDS = 10
DEAD_STRIKES_KEY = "github_pages:webhook_dead_strikes"  # hash: row id -> unix time of first dead answer
ROTATION_CURSOR_KEY = "github_pages:webhook_rotation_cursor"  # json [webhook_id, row id] last probed

ALIVE = "alive"
DEAD = "dead"
INCONCLUSIVE = "inconclusive"

PoolRow = namedtuple("PoolRow", "id webhook_id url")


def pool_sort_key(row):
    """Publish order: webhook snowflake, then row id."""
    return (row.webhook_id or "", row.id)


def _url_webhook_id(url):
    credentials = webhook_credentials(url)
    return credentials.split("/", 1)[0] if credentials else None


def classify_probe(status, payload, expected_id=None):
    """ALIVE, DEAD or INCONCLUSIVE for one webhook GET.

    ``payload`` is the decoded JSON body, or None when there wasn't one. A 2xx
    only counts as alive when the body is the webhook itself, so an edge or
    proxy answering 200 with an error page can't vouch for it."""
    if status is not None and 200 <= status < 300:
        if isinstance(payload, dict) and (expected_id is None or str(payload.get("id")) == str(expected_id)):
            return ALIVE
        return INCONCLUSIVE
    if status == 404 and isinstance(payload, dict) and payload.get("code") == UNKNOWN_WEBHOOK_CODE:
        return DEAD
    return INCONCLUSIVE


async def probe_webhook(http_session, url):
    """GET one webhook URL. Returns ``(verdict, detail)``, detail being the HTTP
    status or the exception name. Never raises — a timeout is
    asyncio.TimeoutError, not aiohttp.ClientError."""
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS)) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = None
            return classify_probe(response.status, payload, _url_webhook_id(url)), str(response.status)
    except Exception as e:
        return INCONCLUSIVE, type(e).__name__


def run_is_degraded(probed, inconclusive):
    """Too many inconclusive probes to trust anything this run concluded."""
    return inconclusive > max(INCONCLUSIVE_MIN, INCONCLUSIVE_RATIO * probed)


def plan_strikes(verdicts, strikes, now, confirm_seconds=DEAD_CONFIRM_SECONDS):
    """What a trusted run does with its verdicts.

    ``verdicts`` maps row id -> verdict; ``strikes`` maps row id -> unix time of
    that row's first dead answer. Returns ``(delete_ids, new_strikes,
    cleared_ids)``: dead with a strike at least ``confirm_seconds`` old ->
    delete; dead without a strike -> strike now; alive -> clear any strike.
    Inconclusive changes nothing."""
    delete_ids, new_strikes, cleared_ids = [], {}, []
    for row_id, verdict in verdicts.items():
        struck_at = strikes.get(row_id)
        if verdict == DEAD:
            if struck_at is None:
                new_strikes[row_id] = now
            elif now - struck_at >= confirm_seconds:
                delete_ids.append(row_id)
        elif verdict == ALIVE and struck_at is not None:
            cleared_ids.append(row_id)
    return delete_ids, new_strikes, cleared_ids


def rotation_slice(rows, cursor, exclude_ids, batch=ROTATION_BATCH):
    """The next ``batch`` rows after ``cursor`` in publish order, wrapping around
    and skipping ``exclude_ids``. ``rows`` must be sorted by pool_sort_key and
    ``cursor`` is the sort key of the last row an earlier run probed (None to
    start at the top). Returns ``(rows, new_cursor)``."""
    if not rows or batch <= 0:
        return [], cursor
    start = bisect.bisect_right([pool_sort_key(row) for row in rows], tuple(cursor)) if cursor else 0
    picked = []
    for row in rows[start:] + rows[:start]:
        if len(picked) >= batch:
            break
        if row.id not in exclude_ids:
            picked.append(row)
    return picked, (pool_sort_key(picked[-1]) if picked else cursor)


def _load_pool_rows():
    """Every webhooks row with a usable URL, as plain tuples in publish order
    (short session: nothing is held open while probing)."""
    with Session() as s:
        rows = [
            PoolRow(r.id, str(r.webhook_id) if r.webhook_id else None, r.webhook_url)
            for r in s.query(Webhook.id, Webhook.webhook_id, Webhook.webhook_url).all()
        ]
    return sorted((row for row in rows if webhook_credentials(row.url)), key=pool_sort_key)


def _redis_conn():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception as e:
        print(f"Webhook check: Redis unavailable ({e})")
        return None


def _read_strikes(conn):
    strikes = {}
    for key, value in (conn.hgetall(DEAD_STRIKES_KEY) or {}).items():
        try:
            strikes[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return strikes


def _read_cursor(conn):
    try:
        webhook_id, row_id = json.loads(conn.get(ROTATION_CURSOR_KEY))
        return (str(webhook_id), int(row_id))
    except Exception:
        return None


def _delete_rows(rows):
    """Delete confirmed-dead rows, matched on id AND url so a row that changed
    since it was probed survives. Returns how many went."""
    deleted = 0
    with Session() as s:
        for row in rows:
            deleted += s.query(Webhook).filter(
                Webhook.id == row.id, Webhook.webhook_url == row.url
            ).delete(synchronize_session=False)
        s.commit()
    return deleted


async def check_pool_webhooks():
    """Probe the webhooks about to be published plus a rotating slice of the
    rest, and apply the rules above. Returns the run report read by
    summarize_publish; ``publish_urls`` is the ordered list to publish, or None
    when the webhook files must stay as they are."""
    rows = _load_pool_rows()
    conn = _redis_conn()
    strikes = {}
    if conn is not None:
        try:
            strikes = _read_strikes(conn)
        except Exception as e:
            print(f"Webhook check: couldn't read strikes ({e}); nothing will be deleted this run")
            conn = None

    verdicts, answers, publishable, rotation, new_cursor = {}, Counter(), [], [], None
    async with aiohttp.ClientSession() as http_session:
        async def probe(row):
            if verdicts:
                await asyncio.sleep(PROBE_DELAY_SECONDS)
            verdict, detail = await probe_webhook(http_session, row.url)
            verdicts[row.id] = verdict
            answers[detail] += 1
            return verdict

        for row in rows:
            if len(publishable) >= PUBLISH_COUNT or len(verdicts) >= MAX_PUBLISH_PROBES:
                break
            verdict = await probe(row)
            # A struck row stays unpublished until a probe says it's alive.
            if verdict == ALIVE or (verdict == INCONCLUSIVE and row.id not in strikes):
                publishable.append(row)

        if conn is not None:
            rotation, new_cursor = rotation_slice(rows, _read_cursor(conn), set(verdicts), ROTATION_BATCH)
            for row in rotation:
                await probe(row)

    probed = len(verdicts)
    inconclusive = sum(1 for verdict in verdicts.values() if verdict == INCONCLUSIVE)
    report = {
        "tested": probed,
        "alive": sum(1 for verdict in verdicts.values() if verdict == ALIVE),
        "dead": sum(1 for verdict in verdicts.values() if verdict == DEAD),
        "inconclusive": inconclusive,
        "degraded": run_is_degraded(probed, inconclusive),
        "struck": 0,
        "deleted": 0,
        "publish_urls": [row.url for row in publishable],
    }

    if report["degraded"]:
        report["publish_urls"] = None
    elif conn is not None:
        delete_ids, new_strikes, cleared_ids = plan_strikes(verdicts, strikes, time.time(), DEAD_CONFIRM_SECONDS)
        by_id = {row.id: row for row in rows}
        if delete_ids:
            try:
                report["deleted"] = _delete_rows([by_id[row_id] for row_id in delete_ids])
                for row_id in delete_ids:
                    print(f"Deleted dead webhook {by_id[row_id].webhook_id} (row {row_id}): "
                          f"Unknown Webhook since {datetime.fromtimestamp(strikes[row_id]):%Y-%m-%d %H:%M}")
            except Exception as e:
                print(f"Webhook check: deleting confirmed-dead rows failed ({e}); will retry next run")
                delete_ids = []
        try:
            if new_strikes:
                conn.hset(DEAD_STRIKES_KEY, mapping={str(k): str(v) for k, v in new_strikes.items()})
            stale_ids = [row_id for row_id in strikes if row_id not in by_id]
            dropped = [str(row_id) for row_id in (*cleared_ids, *delete_ids, *stale_ids)]
            if dropped:
                conn.hdel(DEAD_STRIKES_KEY, *dropped)
            if new_cursor is not None:
                conn.set(ROTATION_CURSOR_KEY, json.dumps(list(new_cursor)))
            report["struck"] = len(new_strikes)
        except Exception as e:
            print(f"Webhook check: couldn't save strikes ({e})")

    print(
        f"Webhook check: probed {probed} ({report['alive']} alive, {report['dead']} dead, "
        f"{inconclusive} inconclusive; answers {dict(answers)}), {len(rotation)} of them from rotation; "
        f"publishable {len(publishable)}, struck {report['struck']}, deleted {report['deleted']}"
        + (" — UNTRUSTED: nothing deleted, webhook files left unchanged" if report["degraded"] else "")
    )
    return report
