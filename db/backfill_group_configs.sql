-- Backfill default configurations for groups missing config entries.
--
-- Copies any config keys from group 1 (the default template) into groups
-- that don't already have that key. Special cases:
--   clan_name   -> uses the group's actual name from the groups table
--   authed_users -> defaults to an empty JSON array (no authed users known)
--
-- Safe to run multiple times: the NOT EXISTS check prevents duplicates.

INSERT INTO group_configurations (group_id, config_key, config_value, updated_at)
SELECT
    g.group_id,
    dc.config_key,
    CASE
        WHEN dc.config_key = 'clan_name'    THEN g.group_name
        WHEN dc.config_key = 'authed_users' THEN '[]'
        ELSE dc.config_value
    END AS config_value,
    NOW() AS updated_at
FROM groups g
CROSS JOIN group_configurations dc
WHERE dc.group_id = 1
  AND g.group_id != 1
  AND NOT EXISTS (
      SELECT 1
      FROM group_configurations gc
      WHERE gc.group_id = g.group_id
        AND gc.config_key = dc.config_key
  );
