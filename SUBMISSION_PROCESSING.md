# Submission Processing Documentation

This document outlines how the DropTracker application processes incoming submissions for drops, collection logs, combat achievements, and personal bests.

## Overview

The submission system processes four main types of data:
1. **Drops** - Loot drops from NPCs/bosses
2. **Collection Logs** - Collection log item completions
3. **Combat Achievements** - Combat achievement task completions
4. **Personal Bests** - Boss kill time records

## Authentication System

### Player Authentication
All submissions require authentication through the `check_auth()` function:

**Parameters:**
- `player_name` (String) - In-game character name
- `account_hash` (String) - Unique account verification hash

**Return Values:**
- `(True, True)` - Player exists and hash matches (authenticated)
- `(True, False)` - Player exists but hash doesn't match (failed auth)
- `(False, False)` - Player doesn't exist

**Authentication Flow:**
1. Query player by name
2. If player doesn't exist, return `(False, False)`
3. If player exists but no account_hash stored, update with provided hash
4. If hash exists and matches, return `(True, True)`
5. If hash exists but doesn't match, return `(True, False)`

### Player Creation
If a player doesn't exist, the system attempts to create them via `create_player()`:

**Process:**
1. Validate account_hash (minimum 5 characters)
2. Query WiseOldMan API for player data
3. Check if player already exists by WOM ID or account hash
4. Handle name changes if player exists with different name
5. Create new player record with WOM data
6. Generate notifications for new player creation

## Drop Processing (`drop_processor`)

### Required Data Fields
```json
{
  "source": "NPC name",           // or "npc_name"
  "value": 1000000,               // GP value per item
  "item_id": 12345,               // or "id"
  "item_name": "Item name",       // or "item"
  "quantity": 1,                  // Number of items
  "player_name": "Player",        // or "player"
  "acc_hash": "hash_string",      // Account verification hash
  "kill_count": 100,              // Optional kill count
  "image_url": "url",             // Optional image URL
  "attachment_url": "url",        // Optional attachment URL
  "attachment_type": "image/png", // Optional attachment type
  "downloaded": false             // Whether image is pre-downloaded
}
```

### Processing Flow
1. **Validation & Authentication**
   - Validate player authentication
   - Verify item exists in database (create if missing)
   - Verify NPC exists in database (create if missing)

2. **Value Verification**
   - For drops over 1M GP, verify item can drop from specified NPC
   - Prevents fake submissions of valuable items

3. **Database Operations**
   - Create Drop record
   - Update player Redis cache
   - Handle image download/processing

4. **Notification Processing**
   - Get player's groups
   - Check each group's minimum notification value
   - Create notifications for qualifying drops
   - Handle direct message preferences

### Drop Notification Thresholds
- Each group has a `minimum_value_to_notify` configuration
- Default threshold: 2,500,000 GP
- Only drops meeting/exceeding threshold generate notifications

## Collection Log Processing (`clog_processor`)

### Required Data Fields
```json
{
  "player_name": "Player",        // or "player"
  "acc_hash": "hash_string",      // Account verification hash
  "item_name": "Item name",       // or "item"
  "source": "NPC name",           // Source of the item
  "reported_slots": 500,          // Total collection log slots
  "kc": 100,                      // Optional kill count
  "attachment_url": "url",        // Optional attachment URL
  "attachment_type": "image/png", // Optional attachment type
  "downloaded": false,            // Whether image is pre-downloaded
  "image_url": "url"              // Optional direct image URL
}
```

### Processing Flow
1. **Validation**
   - Authenticate player
   - Validate item exists (create if missing)
   - Validate NPC exists (create if missing)

2. **Duplicate Check**
   - Query for existing collection log entry
   - Only process if item not already logged

3. **Database Operations**
   - Create CollectionLogEntry record
   - Process image if provided
   - Commit transaction

4. **Notification Processing**
   - Check if groups have `notify_clogs` enabled
   - Create notifications for enabled groups
   - Handle DM preferences

## Combat Achievement Processing (`ca_processor`)

### Required Data Fields
```json
{
  "player_name": "Player",        // Player name
  "acc_hash": "hash_string",      // Account verification hash
  "points": 5,                    // Points awarded for task
  "total_points": 500,            // Total CA points
  "completed": "Hard",            // Completed tier
  "task": "Task name",            // Task name
  "tier": "hard",                 // Task tier
  "attachment_url": "url",        // Optional attachment URL
  "attachment_type": "image/png", // Optional attachment type
  "downloaded": false,            // Whether image is pre-downloaded
  "image_url": "url"              // Optional direct image URL
}
```

### Processing Flow
1. **Validation**
   - Authenticate player
   - Check for duplicate task completion

2. **Database Operations**
   - Create CombatAchievementEntry if new
   - Process image if provided

3. **Notification Processing**
   - Check if groups have `notify_cas` enabled
   - Verify task tier meets minimum notification tier
   - Create notifications for qualifying achievements

### Tier System
Tiers in order: `easy` → `medium` → `hard` → `elite` → `master` → `grandmaster`

Groups can set minimum tier for notifications via `min_ca_tier_to_notify` config.

## Personal Best Processing (`pb_processor`)

### Required Data Fields
```json
{
  "player_name": "Player",          // Player name
  "acc_hash": "hash_string",        // Account verification hash
  "npc_name": "Boss name",          // or "boss_name"
  "current_time_ms": 120000,        // Current kill time in milliseconds
  "personal_best_ms": 150000,       // Previous PB in milliseconds
  "team_size": "Solo",              // Team size (Solo, 2-5, etc.)
  "is_new_pb": true,                // Whether this is a new PB
  "attachment_url": "url",          // Optional attachment URL
  "attachment_type": "image/png",   // Optional attachment type
  "downloaded": false,              // Whether image is pre-downloaded
  "image_url": "url"                // Optional direct image URL
}
```

### Processing Flow
1. **Validation**
   - Authenticate player
   - Validate NPC exists
   - Determine actual time used

2. **Database Operations**
   - Query existing PB for player/NPC/team size
   - Update existing or create new PB record
   - Process image if new PB

3. **Notification Processing**
   - Only process if `is_new_pb` is true
   - Check if groups have `notify_pbs` enabled
   - Create notifications for new personal bests

## Notification System

### Notification Types
- `drop` - Drop notifications
- `clog` - Collection log notifications  
- `ca` - Combat achievement notifications
- `pb` - Personal best notifications
- `dm_drop` - Direct message drop notifications
- `dm_clog` - Direct message collection log notifications
- `dm_ca` - Direct message CA notifications
- `dm_pb` - Direct message PB notifications
- `new_player` - New player creation
- `name_change` - Player name change
- `new_npc` - New NPC discovered
- `new_item` - New item discovered

### Notification Queue System
Notifications are queued in the `notification_queue` table with:
- Duplicate prevention via SHA256 hashing
- Group-specific notification tracking
- JSON data storage for notification details

### Direct Message Preferences
Users can configure DM preferences via UserConfiguration:
- `dm_drops` - Receive drop DMs
- `dm_clogs` - Receive collection log DMs
- `dm_cas` - Receive combat achievement DMs
- `dm_pbs` - Receive personal best DMs
- `dm_account_changes` - Receive account change notifications

## Image Processing

### Image Handling
- Images can be provided via `attachment_url` or direct `image_url`
- System downloads and processes images via `download_player_image()`
- Supports various file types based on `attachment_type`
- Images are stored with structured naming: `{type}_{player_id}_{name}_{timestamp}`

### Image Types
- Drop images: `drop_{player_id}_{item_name}_{timestamp}`
- Collection log: `clog_{player_id}_{item_name}_{timestamp}`
- Combat achievement: `ca_{player_id}_{task_name}_{timestamp}`
- Personal best: `pb_{player_id}_{boss_name}_{timestamp}`

## Group Configuration

### Key Configuration Options
- `minimum_value_to_notify` - Minimum GP value for drop notifications (default: 2,500,000)
- `notify_clogs` - Enable collection log notifications (true/false)
- `notify_cas` - Enable combat achievement notifications (true/false)
- `notify_pbs` - Enable personal best notifications (true/false)
- `min_ca_tier_to_notify` - Minimum CA tier for notifications
- `authed_users` - JSON array of authorized Discord user IDs

## External Integrations

### WiseOldMan Integration
- Player validation via WOM API
- Fetches player stats and collection log data
- Handles name changes and account updates

### XenForo Integration
- Creates forum entries for significant submissions
- Tracks submissions across web platform

### Redis Integration
- Caches player totals and leaderboard data
- Updates player statistics in real-time
- Stores temporary notification tracking

## Error Handling

### Common Error Scenarios
1. **Invalid Authentication** - Player exists but hash doesn't match
2. **Missing Items/NPCs** - Items or NPCs not in database
3. **Duplicate Submissions** - Already processed submissions
4. **Image Processing Failures** - Failed image downloads
5. **Database Conflicts** - Transaction rollback scenarios

### Validation Checks
- Account hash minimum length (5 characters)
- Item/NPC existence verification
- Drop value vs NPC source validation (for high-value drops)
- Duplicate prevention at notification level

## Performance Considerations

### Caching Strategy
- Player list caching (`player_list` dictionary)
- NPC list caching (`npc_list` dictionary)
- Redis for real-time statistics

### Batch Processing
- Notification deduplication
- Image processing optimization
- Database transaction management

### Session Management
- Support for external session injection
- Proper transaction handling and rollback
- Connection pooling for database operations 