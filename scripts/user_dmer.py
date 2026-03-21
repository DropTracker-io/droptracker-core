"""
Standalone Discord DM Script using interactions library

This script sends direct messages to a list of Discord users, handling:
- Rate limits (Discord's DM rate limit ~5-10/second, we use conservative delays)
- Permission errors (users who have DMs disabled)
- Connection issues with automatic retry
- Per-user personalization with <@user> placeholder replacement

Usage:
    from scripts.user_dmer import send_bulk_dms
    
    results = await send_bulk_dms(
        bot_token="your_token_here",  # or None to use env var
        user_ids=[123456789, 987654321, ...],
        content="Hello <@user>!",  # <@user> will be replaced with actual mention
        embeds=[embed],
        components=[button_row]
    )
"""

import asyncio
import copy
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional, Union

import interactions
from interactions import Embed, ActionRow
from interactions.client.errors import Forbidden, HTTPException, NotFound
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("user_dmer")

# Placeholder that will be replaced with the actual user mention
USER_PLACEHOLDER = "<@user>"


def replace_user_placeholder(obj: Any, user_id: int) -> Any:
    """
    Recursively replace <@user> placeholder with actual Discord mention.
    
    Works with:
    - Strings (content)
    - Embed objects
    - Component objects (ActionRow, ContainerComponent, etc.)
    - Lists of any of the above
    
    Args:
        obj: The object to process (will be deep copied)
        user_id: The Discord user ID to substitute
    
    Returns:
        A new object with placeholders replaced
    """
    mention = f"<@{user_id}>"
    
    if obj is None:
        return None
    
    # Handle strings directly
    if isinstance(obj, str):
        return obj.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention)
    
    # Handle lists/tuples
    if isinstance(obj, (list, tuple)):
        result = [replace_user_placeholder(item, user_id) for item in obj]
        return type(obj)(result) if isinstance(obj, tuple) else result
    
    # Handle Embed objects
    if isinstance(obj, Embed):
        new_embed = copy.deepcopy(obj)
        if new_embed.title:
            new_embed.title = new_embed.title.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention)
        if new_embed.description:
            new_embed.description = new_embed.description.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention)
        if new_embed.footer and new_embed.footer.text:
            new_embed.footer.text = new_embed.footer.text.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention)
        if new_embed.author and new_embed.author.name:
            new_embed.author.name = new_embed.author.name.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention)
        if new_embed.fields:
            for field in new_embed.fields:
                if field.name:
                    field.name = field.name.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention)
                if field.value:
                    field.value = field.value.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention)
        return new_embed
    
    # Handle component objects (deep copy and process recursively)
    if hasattr(obj, '__dict__'):
        new_obj = copy.deepcopy(obj)
        
        # Process common text attributes
        for attr in ['content', 'label', 'placeholder', 'value', 'custom_id']:
            if hasattr(new_obj, attr):
                val = getattr(new_obj, attr)
                if isinstance(val, str):
                    setattr(new_obj, attr, val.replace(USER_PLACEHOLDER, mention).replace("<@name>", mention))
        
        # Process nested components
        if hasattr(new_obj, 'components') and new_obj.components:
            new_obj.components = replace_user_placeholder(new_obj.components, user_id)
        
        # Process children (some components use this)
        if hasattr(new_obj, 'children') and new_obj.children:
            new_obj.children = replace_user_placeholder(new_obj.children, user_id)
        
        return new_obj
    
    # Return unchanged for unhandled types
    return obj


class DMStatus(Enum):
    """Status of a DM send attempt"""
    SUCCESS = "success"
    FORBIDDEN = "forbidden"  # User has DMs disabled or blocked the bot
    NOT_FOUND = "not_found"  # User doesn't exist
    RATE_LIMITED = "rate_limited"  # Hit rate limit, retry exhausted
    FAILED = "failed"  # Other failure


@dataclass
class DMResult:
    """Result of a single DM send attempt"""
    user_id: int
    status: DMStatus
    error_message: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __repr__(self):
        return f"DMResult(user_id={self.user_id}, status={self.status.value}, error={self.error_message})"


@dataclass 
class BulkDMResults:
    """Aggregated results from a bulk DM operation"""
    total: int = 0
    successful: int = 0
    forbidden: int = 0
    not_found: int = 0
    rate_limited: int = 0
    failed: int = 0
    results: List[DMResult] = field(default_factory=list)
    
    def add_result(self, result: DMResult):
        self.results.append(result)
        self.total += 1
        match result.status:
            case DMStatus.SUCCESS:
                self.successful += 1
            case DMStatus.FORBIDDEN:
                self.forbidden += 1
            case DMStatus.NOT_FOUND:
                self.not_found += 1
            case DMStatus.RATE_LIMITED:
                self.rate_limited += 1
            case DMStatus.FAILED:
                self.failed += 1
    
    def get_failed_user_ids(self) -> List[int]:
        """Return list of user IDs that failed (excluding forbidden/not_found)"""
        return [r.user_id for r in self.results 
                if r.status in (DMStatus.RATE_LIMITED, DMStatus.FAILED)]
    
    def get_forbidden_user_ids(self) -> List[int]:
        """Return list of user IDs with DMs disabled"""
        return [r.user_id for r in self.results if r.status == DMStatus.FORBIDDEN]
    
    def summary(self) -> str:
        return (
            f"Bulk DM Results:\n"
            f"  Total: {self.total}\n"
            f"  Successful: {self.successful}\n"
            f"  Forbidden (DMs disabled): {self.forbidden}\n"
            f"  Not Found: {self.not_found}\n"
            f"  Rate Limited: {self.rate_limited}\n"
            f"  Failed: {self.failed}"
        )


async def send_dm_to_user(
    bot: interactions.Client,
    user_id: int,
    content: Optional[str] = None,
    embeds: Optional[List[Embed]] = None,
    components: Optional[List[ActionRow]] = None,
    max_retries: int = 3,
    retry_delay: float = 5.0,
    personalize: bool = True
) -> DMResult:
    """
    Send a DM to a single user with retry logic.
    
    Args:
        bot: The interactions.Client instance
        user_id: Discord user ID to message
        content: Text content of the message (supports <@user> placeholder)
        embeds: List of Embed objects (supports <@user> placeholder in text fields)
        components: List of ActionRow/Container components (supports <@user> placeholder)
        max_retries: Number of retries on rate limit
        retry_delay: Base delay between retries (exponential backoff applied)
        personalize: If True, replaces <@user> placeholders with actual mention
    
    Returns:
        DMResult with status and any error message
    """
    for attempt in range(max_retries):
        try:
            # Fetch the user
            user = await bot.fetch_user(user_id=user_id)
            if not user:
                return DMResult(
                    user_id=user_id,
                    status=DMStatus.NOT_FOUND,
                    error_message="User not found"
                )
            
            # Apply personalization - replace <@user> with actual mention
            if personalize:
                processed_content = replace_user_placeholder(content, user_id) if content else None
                processed_embeds = replace_user_placeholder(embeds, user_id) if embeds else None
                processed_components = replace_user_placeholder(components, user_id) if components else None
            else:
                processed_content = content
                processed_embeds = embeds
                processed_components = components
            
            # Prepare send kwargs
            send_kwargs = {}
            if processed_content:
                send_kwargs["content"] = processed_content
            if processed_embeds:
                send_kwargs["embeds"] = processed_embeds
            if processed_components:
                send_kwargs["components"] = processed_components
            
            # Must have at least one thing to send
            if not send_kwargs:
                return DMResult(
                    user_id=user_id,
                    status=DMStatus.FAILED,
                    error_message="No content, embeds, or components provided"
                )
            
            # Send the DM
            await user.send(**send_kwargs)
            
            return DMResult(
                user_id=user_id,
                status=DMStatus.SUCCESS
            )
            
        except Forbidden as e:
            # User has DMs disabled or has blocked the bot
            return DMResult(
                user_id=user_id,
                status=DMStatus.FORBIDDEN,
                error_message=f"Cannot send DM: {str(e)}"
            )
            
        except NotFound as e:
            # User doesn't exist
            return DMResult(
                user_id=user_id,
                status=DMStatus.NOT_FOUND,
                error_message=f"User not found: {str(e)}"
            )
            
        except HTTPException as e:
            # Check if rate limited
            if e.status == 429:
                if attempt < max_retries - 1:
                    # Get retry_after from the response if available
                    retry_after = getattr(e, 'retry_after', retry_delay * (2 ** attempt))
                    logger.warning(
                        f"Rate limited on user {user_id}, attempt {attempt + 1}/{max_retries}. "
                        f"Waiting {retry_after}s..."
                    )
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    return DMResult(
                        user_id=user_id,
                        status=DMStatus.RATE_LIMITED,
                        error_message=f"Rate limited after {max_retries} attempts"
                    )
            else:
                return DMResult(
                    user_id=user_id,
                    status=DMStatus.FAILED,
                    error_message=f"HTTP error {e.status}: {str(e)}"
                )
                
        except Exception as e:
            logger.error(f"Unexpected error sending DM to {user_id}: {e}")
            return DMResult(
                user_id=user_id,
                status=DMStatus.FAILED,
                error_message=f"Unexpected error: {str(e)}"
            )
    
    # Should not reach here, but just in case
    return DMResult(
        user_id=user_id,
        status=DMStatus.FAILED,
        error_message="Max retries exhausted"
    )


async def send_bulk_dms(
    user_ids: List[int],
    content: Optional[str] = None,
    embeds: Optional[List[Embed]] = None,
    components: Optional[List[ActionRow]] = None,
    bot_token: Optional[str] = None,
    bot: Optional[interactions.Client] = None,
    delay_between_dms: float = 0.5,
    max_retries_per_user: int = 3,
    progress_callback: Optional[callable] = None,
    personalize: bool = True
) -> BulkDMResults:
    """
    Send DMs to a list of users with rate limiting and error handling.
    
    Args:
        user_ids: List of Discord user IDs to message
        content: Text content of the message (optional if embeds/components provided)
                 Supports <@user> placeholder for personalization
        embeds: List of Embed objects (optional, supports <@user> placeholder)
        components: List of ActionRow/Container components (optional, supports <@user> placeholder)
        bot_token: Discord bot token. If not provided, uses DM_BOT_TOKEN or BOT_TOKEN env var
        bot: Existing interactions.Client instance. If provided, bot_token is ignored.
        delay_between_dms: Delay in seconds between each DM (default 0.5s for safety)
        max_retries_per_user: Max retries per user on rate limit
        progress_callback: Optional async callback(current, total, result) for progress updates
        personalize: If True, replaces <@user> placeholders with actual user mentions
    
    Returns:
        BulkDMResults with aggregated results
    
    Example:
        results = await send_bulk_dms(
            user_ids=[123456789, 987654321],
            content="Hello <@user>!",  # Each user gets their own mention
            embeds=[my_embed]
        )
        print(results.summary())
    """
    results = BulkDMResults()
    
    if not user_ids:
        logger.warning("No user IDs provided")
        return results
    
    if not content and not embeds and not components:
        logger.error("Must provide at least one of: content, embeds, components")
        return results
    
    # Determine if we need to create and manage our own bot
    owns_bot = bot is None
    
    if owns_bot:
        # Resolve bot token
        token = bot_token or os.getenv("DM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
        if not token:
            logger.error("No bot token provided and DM_BOT_TOKEN/BOT_TOKEN env vars not set")
            return results
        
        # Create bot instance - only need minimal intents for DMs
        bot = interactions.Client(
            intents=interactions.Intents.DIRECT_MESSAGES,
            send_command_traceback=False
        )
        
        # Start the bot
        logger.info("Starting bot...")
        try:
            await bot.login(token)
        except Exception as e:
            logger.error(f"Failed to login bot: {e}")
            return results
    
    try:
        # Process each user
        total = len(user_ids)
        logger.info(f"Starting bulk DM to {total} users...")
        
        for i, user_id in enumerate(user_ids, 1):
            result = await send_dm_to_user(
                bot=bot,
                user_id=user_id,
                content=content,
                embeds=embeds,
                components=components,
                max_retries=max_retries_per_user,
                personalize=personalize
            )
            
            results.add_result(result)
            
            # Log progress
            status_emoji = "✓" if result.status == DMStatus.SUCCESS else "✗"
            logger.info(f"[{i}/{total}] {status_emoji} User {user_id}: {result.status.value}")
            
            # Progress callback
            if progress_callback:
                try:
                    await progress_callback(i, total, result)
                except Exception as e:
                    logger.warning(f"Progress callback error: {e}")
            
            # Delay between DMs (skip on last one)
            if i < total:
                await asyncio.sleep(delay_between_dms)
        
        logger.info(f"Bulk DM complete. {results.summary()}")
        
    finally:
        # Clean up if we created the bot
        if owns_bot and bot:
            try:
                await bot.stop()
            except Exception as e:
                logger.warning(f"Error stopping bot: {e}")
    
    return results


async def send_bulk_dms_with_existing_bot(
    bot: interactions.Client,
    user_ids: List[int],
    content: Optional[str] = None,
    embeds: Optional[List[Embed]] = None,
    components: Optional[List[ActionRow]] = None,
    delay_between_dms: float = 0.5,
    max_retries_per_user: int = 3,
    progress_callback: Optional[callable] = None,
    personalize: bool = True
) -> BulkDMResults:
    """
    Convenience wrapper for send_bulk_dms when you have an existing bot instance.
    
    This is useful when calling from within an existing bot's command/event handler.
    
    Args:
        bot: Existing interactions.Client instance
        user_ids: List of Discord user IDs to message
        content: Text content of the message (supports <@user> placeholder)
        embeds: List of Embed objects (supports <@user> placeholder)
        components: List of ActionRow components (supports <@user> placeholder)
        delay_between_dms: Delay in seconds between each DM
        max_retries_per_user: Max retries per user on rate limit
        progress_callback: Optional async callback(current, total, result) for progress
        personalize: If True, replaces <@user> placeholders with actual user mentions
    
    Returns:
        BulkDMResults with aggregated results
    """
    return await send_bulk_dms(
        user_ids=user_ids,
        content=content,
        embeds=embeds,
        components=components,
        bot=bot,
        delay_between_dms=delay_between_dms,
        max_retries_per_user=max_retries_per_user,
        progress_callback=progress_callback,
        personalize=personalize
    )


# CLI / Standalone execution
async def main():
    """Example usage / CLI entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Send bulk DMs via Discord bot")
    parser.add_argument(
        "--users", "-u",
        nargs="+",
        type=int,
        required=True,
        help="Space-separated list of Discord user IDs"
    )
    parser.add_argument(
        "--message", "-m",
        type=str,
        required=True,
        help="Message content to send"
    )
    parser.add_argument(
        "--token", "-t",
        type=str,
        default=None,
        help="Bot token (defaults to DM_BOT_TOKEN or BOT_TOKEN env var)"
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=0.5,
        help="Delay between DMs in seconds (default: 0.5)"
    )
    
    args = parser.parse_args()
    
    results = await send_bulk_dms(
        user_ids=args.users,
        content=args.message,
        bot_token=args.token,
        delay_between_dms=args.delay
    )
    
    print("\n" + results.summary())
    
    if results.forbidden:
        print(f"\nUsers with DMs disabled: {results.get_forbidden_user_ids()}")
    
    if results.get_failed_user_ids():
        print(f"\nFailed user IDs (retriable): {results.get_failed_user_ids()}")


if __name__ == "__main__":
    asyncio.run(main())
