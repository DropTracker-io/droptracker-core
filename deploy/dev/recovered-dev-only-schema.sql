-- DDL of dev-only tables (feat/state-sync + components-V2 pilot).

-- Their alembic files lived only in dtdev alembic/versions (gitignored) and were

-- lost to a prod rsync --delete on 2026-08-18; chain was repointed to web89a and

-- upgraded to the prod head. Regenerate migrations from this when the branch lands.

CREATE TABLE `group_component_layouts` (
  `group_id` int(11) NOT NULL,
  `notification_type` varchar(32) NOT NULL,
  `layout` longtext NOT NULL,
  `active` tinyint(1) NOT NULL DEFAULT 0,
  `updated_at` datetime NOT NULL DEFAULT current_timestamp(),
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`group_id`,`notification_type`),
  CONSTRAINT `group_component_layouts_ibfk_1` FOREIGN KEY (`group_id`) REFERENCES `groups` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `player_state` (
  `player_id` int(11) NOT NULL,
  `account_type` smallint(6) DEFAULT NULL,
  `combat_level` smallint(6) DEFAULT NULL,
  `clog_slots` int(11) DEFAULT NULL,
  `clog_slots_total` int(11) DEFAULT NULL,
  `manifest_version` varchar(32) DEFAULT NULL,
  `last_sync_source` varchar(32) DEFAULT NULL,
  `last_synced_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp(),
  `model_fingerprint` varchar(32) DEFAULT NULL,
  PRIMARY KEY (`player_id`),
  CONSTRAINT `player_state_ibfk_1` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `player_quest_states` (
  `player_id` int(11) NOT NULL,
  `quest_id` int(11) NOT NULL,
  `state` smallint(6) NOT NULL DEFAULT 0,
  `updated_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`player_id`,`quest_id`),
  CONSTRAINT `player_quest_states_ibfk_1` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;