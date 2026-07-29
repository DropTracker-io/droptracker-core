import asyncio
import hashlib
import os
import re
import aiohttp
import github
import time
from github import Github
# NOTE: never import the module-global scoped session here. This module runs in
# long-lived processes (player-updates' github loop, in both the asyncio main
# thread and to_thread workers); a read on the scoped session autobegins a
# transaction that nothing ever commits, which held an idle InnoDB transaction
# (and its metadata locks) open for the entire service lifetime — 20h+ in the
# 2026-07-16 incident. Use short-lived `with Session()` blocks instead.
from db.models import GroupConfiguration, Webhook, NewWebhook, Session, WebhookPendingDeletion
from dotenv import load_dotenv
import json
from utils.encrypter import encrypt_webhook, decrypt_webhook
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

    def fetch_webhooks_from_database(self, limit=120):
        """
        Fetch the webhook URLs from the database and format them as a list of URLs.

        Args:
            limit: Maximum number of webhooks to fetch

        Returns:
            list of encrypted webhooks
        """
        try:
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
                    print(f"Failed to encrypt webhook {url}: {e}")
            
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
        global total_hooks

        with Session() as s:
            total_hooks = s.query(Webhook).count()

        # Liveness-check the webhooks we are about to publish (dead ones are
        # deleted from the DB, so the fetch below only sees working hooks).
        await check_limited_webhooks(120, watchdog)
        await asyncio.to_thread(self._update_github_pages)

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
        value-override ids, always force-screenshotted), ``untradeable_items.txt``
        (curated notable untradeables, toggle-gated) and
        ``server_loot_npc_ids.txt`` (npcs whose loot RuneLite only reports via
        ServerNpcLoot — see scripts/export_server_loot_npcs.py; publishing it
        means a new server-loot boss no longer needs a plugin release).
        Publishing them here keeps all three in lockstep with the database /
        curated source without a manual content-repo commit."""
        out = []
        try:
            from db.models import ItemValueOverride

            with Session() as s:
                rows = (
                    s.query(ItemValueOverride.item_id)
                    .filter(ItemValueOverride.active.is_(True),
                            ItemValueOverride.item_id.isnot(None))
                    .all()
                )
            ids = sorted({int(r[0]) for r in rows})
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

    def _update_github_pages(self):
        """
        Publish the latest webhook/news/key/item-list content, committing ONLY
        when something actually changed. One ``content/`` listing replaces the
        recursive repo walks the old implementation did on every run, and every
        file is change-gated (decrypt-compare for webhook files, blob-sha
        compare for deterministic text) so a no-change cycle makes zero commits
        and triggers zero GitHub Pages builds. Stale dated files are pruned in
        the same commit.
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

        try:
            encrypted_webhooks = self.fetch_webhooks_from_database(limit=120)
        except Exception as e:
            print(f"Error fetching webhook URLs from the database: {e}")
            return

        if len(encrypted_webhooks) < 30:
            print("Generated list is too short:", len(encrypted_webhooks))
            return

        chunk_size = 40
        webhook_chunks = [encrypted_webhooks[i:i + chunk_size]
                          for i in range(0, len(encrypted_webhooks), chunk_size)]

        now = datetime.now()
        today_str = now.strftime("%Y%m%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y%m%d")

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

        webhook_files_changed = 0
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
            return

        print(f"Committing {len(files_to_update)} file update(s)"
              + (f" + {len(deletions)} stale deletion(s)" if deletions else "")
              + f" ({webhook_files_changed} webhook file(s) changed).")
        self.update_multiple_files(
            files_to_update,
            commit_message="Update published content (webhooks/news/key/item lists).",
            branch=self.branch,
            deletions=deletions,
        )

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


async def test_webhook(webhook, session):
    """Test a single webhook and return its status"""
    try:
        start_time = time.time()
        if len(str(webhook.webhook_url)) < 5:
            return {
                'webhook_id': webhook.webhook_id if hasattr(webhook, 'webhook_id') else 'pending_deletion',
                'url': webhook.webhook_url,
                'status': 'Error',
                'elapsed': 0,
                'ok': False
            }
        async with session.get(webhook.webhook_url, timeout=10) as response:
            elapsed = time.time() - start_time
            status = response.status
            return {
                'webhook_id': webhook.webhook_id if hasattr(webhook, 'webhook_id') else 'pending_deletion',
                'url': webhook.webhook_url,
                'status': status,
                'elapsed': elapsed,
                'ok': 200 <= status < 400
            }
    except aiohttp.ClientError as e:
        return {
            'webhook_id': webhook.webhook_id if hasattr(webhook, 'webhook_id') else 'pending_deletion',
            'url': webhook.webhook_url,
            'status': 'Error',
            'error': str(e),
            'ok': False
        }


async def test_all_webhooks():
    with Session() as session:
        """Test all webhooks with a delay between requests"""
        webhooks = session.query(Webhook).all()
        secondary = session.query(WebhookPendingDeletion).all()
        all_webhooks = secondary + webhooks
        
        print(f"Testing {len(all_webhooks)} webhooks...")
        
        results = []
        passed = 0
        failed = 0
        async with aiohttp.ClientSession() as http_session:
            for i, webhook in enumerate(all_webhooks):
                #print(f"Testing webhook {i+1}/{len(all_webhooks)}: {webhook.webhook_url}...")
                result = await test_webhook(webhook, http_session)
                results.append(result)
                
                # Print result immediately
                if result['ok']:
                    passed += 1
                else:
                    failed += 1
                    ## Remove it from the database
                    session.delete(webhook)
                    session.commit()
                
                # Add delay between requests (2 seconds)
                if i < len(all_webhooks) - 1:  # Don't delay after the last request
                    await asyncio.sleep(0.25)
        
        #print(f"Checked {len(all_webhooks)} webhooks: {passed} passed, {failed} failed")

async def check_limited_webhooks(limit=80, watchdog=None):
    """
    Check only a limited number of webhooks to ensure they're working before updating GitHub Pages.
    This removes non-working webhooks from the database.
    
    Args:
        limit: Maximum number of webhooks to check
        watchdog: SystemdWatchdog instance to notify during long operations
    """
    print(f"Checking up to {limit} webhooks before GitHub update...")
    try:
        with Session() as session:
            webhooks = session.query(Webhook).limit(limit).all()
            
            print(f"Testing {len(webhooks)} webhooks...")
            
            results = []
            passed = 0
            failed = 0
            async with aiohttp.ClientSession() as http_session:
                for i, webhook in enumerate(webhooks):
                    #print(f"Testing webhook {i+1}/{len(webhooks)}: {webhook.webhook_url}...")
                    result = await test_webhook(webhook, http_session)
                    results.append(result)
                    
                    # Print result immediatelyif i % 10 == 0:
                    #print(f"Checked {i+1}/{len(webhooks)} webhooks: so far, {passed} passed, {failed} failed")
                        
                    if result['ok']:
                        passed += 1
                    else:
                        failed += 1
                        ## Remove it from the database
                        session.delete(webhook)
                        session.commit()
                    
                    # The watchdog is automatically notified by the SystemdWatchdog heartbeat loop
                    # No manual notification needed
                    
                    # Add delay between requests
                    if i < len(webhooks) - 1:  # Don't delay after the last request
                        await asyncio.sleep(0.25)
            
            #print(f"Checked {len(webhooks)} webhooks: {passed} passed, {failed} failed")
        print("Limited webhook check completed")
    except Exception as e:
        print(f"Error checking webhooks: {e}")

async def check_webhooks():
    """
    Check all webhooks to ensure they're working before updating GitHub Pages.
    This removes non-working webhooks from the database.
    """
    print("Checking webhooks before GitHub update...")
    await test_all_webhooks()
    print("Webhook check completed")