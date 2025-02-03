import os
import github
import schedule
import time
from github import Github
from sqlalchemy.orm import Session
from db.models import Webhook, NewWebhook, session as db_sesh
from dotenv import load_dotenv
from interactions import IntervalTrigger, Task
import json
from utils.encrypter import encrypt_webhook, decrypt_webhook

load_dotenv()

class GithubPagesUpdater:
    def __init__(self):
        """
        Initialize the GitHubPagesUpdater.
        """
        load_dotenv()  # Load environment variables

        # GitHub Token and Repo Info
        self.github_token = os.getenv("GITHUB_TOKEN")  # Load GitHub token from .env
        repo_name = "joelhalen/joelhalen.github.io"  # GitHub repository name
        self.file_name = "webhooks.json"  # The name of the file you're looking for
        self.new_file = "crypt.json"
        self.branch = "main"
        # Initialize GitHub API
        self.github = Github(self.github_token)
        self.repo = self.github.get_repo(repo_name)

        # Log the repo and file path for verification
        # print(f"Repo: {repo_name}")

    def fetch_webhooks_from_database(self):
        """
        Fetch the webhook URLs from the database and format them as a list of URLs.
        """
        webhooks = db_sesh.query(Webhook.webhook_url).all()
        webhook_urls = [webhook[0] for webhook in webhooks]
        # Encrypt webhooks and decode to strings since JSON can't handle bytes
        encrypted_webhooks = [encrypt_webhook(webhook) for webhook in webhook_urls]
        return webhook_urls, encrypted_webhooks

    def find_files_by_name(self, path="", file_name=""):
        """
        Search the repository for files with the specified file name.
        :param path: The path to start searching from (empty for root).
        :param file_name: The name of the file to search for.
        :return: A list of file paths matching the specified file name.
        """
        matching_files = []

        try:
            # print(f"Searching for files named '{file_name}' in repository: {self.repo.full_name}, path: {path or 'root'}")
            contents = self.repo.get_contents(path)  # Get contents of the specified path

            for content_file in contents:
                if content_file.type == "dir":
                    # Recursively search in subdirectories
                    matching_files += self.find_files_by_name(content_file.path, file_name)
                elif content_file.name == file_name:
                    # If the file name matches, add to the list
                    print(f"Found file: {content_file.path}")
                    matching_files.append(content_file.path)

        except github.GithubException as e:
            # print(f"Failed to search files: {e}")
            if isinstance(e, github.GithubException):
                print(f"Error response from GitHub: {e.data}")

        return matching_files

    def list_repo_files(self, path=""):
        """
        List all files in the repository starting from a specific path.
        :param path: The path in the repository to start listing files from (e.g., 'docs/'). Leave empty for root.
        """
        try:
            # print(f"Listing files in repository: {self.repo.full_name}, path: {path or 'root'}")
            contents = self.repo.get_contents(path)  # Get contents of the specified path
            if not contents:
                print("No files found.")
                return
            
            for content_file in contents:
                if content_file.type == "dir":
                    # print(f"Directory: {content_file.path}")
                    # Recursively list files in subdirectories
                    self.list_repo_files(content_file.path)
                # print(f"File: {content_file.path}")

        except Exception as e:
            print(f"Failed to list files: {e}")
            if isinstance(e, github.GithubException):
                # print(f"Error response from GitHub: {e.data}")
                pass

    def update_github_pages(self):
        """
        Fetch the latest webhooks from the database and update all matching GitHub Pages files.
        """
        # print(f"Updating GitHub Pages in repo: {self.repo.full_name}, file: {self.file_name}")
        
        # List repository files to debug the file path issue
        self.list_repo_files()  # List all files from the root
        
        # Fetch webhook URLs from the database
        webhook_urls, encrypted_webhooks = self.fetch_webhooks_from_database()
        
        # Create the JSON content for both files
        json_content = json.dumps(webhook_urls, indent=4)  # Plain webhooks
        encrypted_json_content = json.dumps(encrypted_webhooks, indent=4)  # Encrypted webhooks

        # Specify the branch you're working on
        branch = "main"  # Ensure this is the correct branch in your repository
        # print(f"Attempting to update file on branch: {branch}")
        for file_path in self.find_files_by_name(file_name=self.new_file):
            try:
                # Get the file's current contents and metadata
                file = self.repo.get_contents(file_path, ref=branch)
                sha = file.sha  # File identifier for updating
                old_content = file.decoded_content.decode('utf-8')
                # Update the file with new content
                self.repo.update_file(
                    path=file_path,
                    message=f"Auto-updating {self.new_file} based on changes to the database.",
                    content=encrypted_json_content,
                    sha=sha,
                    branch=branch
                )
            except github.GithubException as e:
                print(f"Failed to update GitHub Pages for {file_path}: {e}")
                print(f"Error response from GitHub: {e.data}")
            except Exception as e:
                raise e
                
        # # for file_path in self.find_files_by_name(file_name=self.file_name):
        #     try:
        #         # Get the file's current contents and metadata
        #         file = self.repo.get_contents(file_path, ref=branch)
        #         sha = file.sha  # File identifier for updating
        #         old_content = file.decoded_content.decode('utf-8')

        #         # Debugging: check file details
        #         # print(f"Old content length for {file_path}: {len(old_content)}")
        #         # print(f"New content length for {file_path}: {len(json_content)}")
        #         # print(f"Updating file at path: {file_path} with sha: {sha}")

        #         # Check if the content needs to be updated
        #         if old_content == json_content:
        #             # print(f"No changes to commit for {file_path}.")
        #             continue

        #         # Update the file with new content
        #         self.repo.update_file(
        #             path=f"{file_path}",
        #             message=f"Auto-updating {self.file_name} based on changes to the database.",
        #             content=json_content,
        #             sha=sha,
        #             branch=branch  # Ensure the correct branch is used
        #         )
        #         # print(f"Updated {file_path} with new content.")

        #     except github.GithubException as e:
        #         print(f"Failed to update GitHub Pages for {file_path}: {e}")
        #         print(f"Error response from GitHub: {e.data}")
        #     except Exception as e:
        #         raise e

        for webhook in webhook_urls:
            encrypted_webhook = encrypt_webhook(webhook)
            stored = False
            for existing_webhook in db_sesh.query(NewWebhook.webhook_hash).all():
                if encrypted_webhook == existing_webhook[0]:
                    stored = True
                    break
            if not stored:
                db_sesh.add(NewWebhook(webhook_hash=encrypted_webhook))
        db_sesh.commit()

    @Task.create(IntervalTrigger(minutes=30))
    async def schedule_updates(self):
        self.update_github_pages()
