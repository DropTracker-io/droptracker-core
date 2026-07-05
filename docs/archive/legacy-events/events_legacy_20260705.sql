/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19  Distrib 10.11.14-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: localhost    Database: data
-- ------------------------------------------------------
-- Server version	10.11.14-MariaDB-0+deb12u2

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `events`
--

DROP TABLE IF EXISTS `events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `events` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `description` text DEFAULT NULL,
  `start_date` int(11) DEFAULT NULL,
  `status` varchar(255) NOT NULL,
  `author_id` int(11) NOT NULL,
  `group_id` int(11) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT '0000-00-00 00:00:00',
  `event_type` varchar(255) NOT NULL,
  `banner_image` varchar(255) DEFAULT NULL,
  `title` varchar(255) NOT NULL,
  `end_date` int(11) DEFAULT NULL,
  `max_participants` int(11) DEFAULT NULL,
  `team_size` int(11) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `author_id` (`author_id`),
  KEY `group_id` (`group_id`),
  CONSTRAINT `events_ibfk_1` FOREIGN KEY (`author_id`) REFERENCES `users` (`user_id`),
  CONSTRAINT `events_ibfk_2` FOREIGN KEY (`group_id`) REFERENCES `groups` (`group_id`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `events`
--

LOCK TABLES `events` WRITE;
/*!40000 ALTER TABLE `events` DISABLE KEYS */;
INSERT INTO `events` VALUES
(3,'A globally-available bingo competition, with team sizes of 6.',NULL,'draft',0,2,'2025-05-31 17:27:30','0000-00-00 00:00:00','bingo','https://www.droptracker.io/img/bingo_board_62.png','Global Bingo (#1)',NULL,600,6);
/*!40000 ALTER TABLE `events` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_tasks`
--

DROP TABLE IF EXISTS `event_tasks`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_tasks` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `name` varchar(100) NOT NULL,
  `description` varchar(500) DEFAULT NULL,
  `difficulty` varchar(50) DEFAULT NULL,
  `points` int(11) NOT NULL,
  `required_items` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`required_items`)),
  `date_added` datetime NOT NULL,
  `date_updated` datetime NOT NULL,
  `task_config` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`task_config`)),
  `is_active` tinyint(1) NOT NULL,
  `task_type` enum('item_collection','kc_target','xp_target','ehp_target','ehb_target','loot_value','kill_time','custom') NOT NULL,
  PRIMARY KEY (`id`),
  KEY `event_id` (`event_id`),
  CONSTRAINT `event_tasks_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=58 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_tasks`
--

LOCK TABLES `event_tasks` WRITE;
/*!40000 ALTER TABLE `event_tasks` DISABLE KEYS */;
INSERT INTO `event_tasks` VALUES
(1,3,'Chambers of Xeric Farmer','Collect 100,000,000 GP in loot from Chambers of Xeric.','3',10,'[]','2025-06-04 15:32:16','2025-06-04 15:46:36','{\"target_value\":100000000,\"source_npcs\":[\"Chambers of Xeric\",\"Chambers of Xeric Challenge Mode\"]}',1,'kc_target'),
(2,3,'Abyssal whip','Obtain an abyssal whip.','1',1,'[]','2025-06-04 15:32:16','2025-06-04 15:46:35','{\"requires\":\"any\",\"required_items\":{\"Abyssal whip\":1}}',1,'item_collection'),
(3,3,'Godsword','Create on full godsword including three unique shards and the hilt.','1',5,'[]','2025-06-04 15:32:16','2025-06-04 15:46:32','{\"requires\":\"set\",\"sets\":[[\"Armadyl hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Saradomin hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Bandos hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Zamorak hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Ancient hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"]]}',0,'item_collection'),
(4,3,'Build a Noxious Halberd','Build a noxious halberb by obtaining all three components from Araxxor.','4',100,'[]','2025-06-04 15:32:16','2025-06-04 15:46:34','{\"requires\":\"set\",\"sets\":[[\"Noxious pommel\",\"Noxious blade\",\"Noxious point\"]]}',1,'item_collection'),
(5,3,'Agile Adventurer','Obtain 10,000,000 experience in agility.','4',200,'[]','2025-06-04 15:32:16','2025-06-04 15:46:35','{\"skill_name\":\"agility\",\"target_xp\":10000000}',1,'loot_value'),
(6,3,'Complete a Voidwaker','Obtain a Voidwaker Gem, Hilt, and Blade from the corresponding Wilderness Bosses','5',500,'[]','2025-06-04 15:32:16','2025-06-04 15:46:32','{\"requires\":\"all\",\"required_items\":{\"Voidwaker gem\":1,\"Voidwaker hilt\":1,\"Voidwaker blade\":1}}',0,'item_collection'),
(7,3,'Godwars Points I','Defeat Godwars Dungeon bosses until you reach the required points.','4',20,'[]','2025-06-04 15:32:16','2025-06-04 15:46:32','{\"requires\":\"points\",\"items\":{\"Godsword shard 1\":\"1\",\"Godsword shard 2\":\"1\",\"Godsword shard 3\":\"1\",\"Armadyl chestplate\":\"3\",\"Armadyl chainskirt\":\"3\",\"Armadyl helmet\":\"2\",\"Armadyl hilt\":\"5\",\"Bandos boots\":\"2\",\"Bandos chestplate\":\"3\",\"Bandos tassets\":\"3\",\"Bandos hilt\":\"5\",\"Zamorak hilt\":\"5\",\"Zamorakian spear\":\"3\",\"Steam battlestaff\":\"2\",\"Staff of the dead\":\"3\",\"Armadyl crossbow\":\"3\",\"Saradomin hilt\":\"5\",\"Saradomin sword\":\"2\",\"Saradomin\'s light\":\"3\",\"Ancient hilt\":\"5\",\"Torva full helm (damaged)\":\"3\",\"Torva platebody (damaged)\":\"3\",\"Torva platelegs (damaged)\":\"3\",\"Nihil horn\":\"5\",\"Zaryte vambraces\":\"4\"}}',0,'item_collection'),
(8,3,'Brimstone ring','Obtain all required pieces from Hydra to assemble a Brimstone ring.','3',20,'[]','2025-06-04 15:32:16','2025-06-04 15:46:32','{\"requires\":\"all\",\"required_items\":{\"Hydra\'s eye\":\"1\",\"Hydra\'s heart\":\"1\",\"Hydra\'s fang\":\"1\"}}',0,'item_collection'),
(9,3,'Health is a Priority','Obtain 50,000,000 Hitpoints experience.','2',10,'[]','2025-06-04 15:32:16','2025-06-04 15:46:32','{\"skill_name\":\"hitpoints\",\"target_xp\":50000000}',0,'xp_target'),
(10,3,'Build a ZCB','Obtain an Armadyl Crossbow from Commander Zilyana and a Nihil Horn from Nex','5',250,'[]','2025-06-04 15:32:16','2025-06-04 15:46:36','{\"requires\":\"all\",\"required_items\":{\"Armadyl crossbow\":1,\"Nihil horn\":1}}',1,'item_collection'),
(11,3,'Dragon hunter lance','Obtain a Zamorakian spear and Hydra\'s claw to assemble a Dragon hunter lance.','4',20,'[]','2025-06-04 15:32:16','2025-06-04 15:46:33','{\"requires\":\"all\",\"required_items\":{\"Hydra\'s claw\":\"1\",\"Zamorakian spear\":\"1\"}}',0,'item_collection'),
(12,3,'Agile Adventurer','Obtain 10,000,000 experience in agility.','4',200,'[]','2025-06-04 15:32:16','2025-06-04 15:46:33','{\"skill_name\":\"agility\",\"target_xp\":10000000}',0,'xp_target'),
(13,3,'Complete a Voidwaker','Obtain a Voidwaker Gem, Hilt, and Blade from the corresponding Wilderness Bosses','5',500,'[]','2025-06-04 15:32:16','2025-06-04 15:46:34','{\"requires\":\"all\",\"required_items\":{\"Voidwaker gem\":1,\"Voidwaker hilt\":1,\"Voidwaker blade\":1}}',1,'item_collection'),
(14,3,'Cash Money','Obtain 250m of drops from any source','2',200,'[]','2025-06-04 15:32:16','2025-06-04 15:46:35','{\"target_value\":250000000}',1,'item_collection'),
(15,3,'Zulrah Unique','Obtain any of the four unique drops from zulrah, or a mutagen.','2',10,'[]','2025-06-04 15:32:16','2025-06-04 15:46:33','{\"requires\":\"any\",\"required_items\":{\"Uncut onyx\":\"1\",\"Serpentine visage\":\"1\",\"Magic fang\":\"1\",\"Tanzanite fang\":\"1\",\"Magma mutagen\":\"1\",\"Tanzanite mutagen\":\"1\"}}',0,'item_collection'),
(16,3,'Cash Money','Obtain 250m of drops from any source','2',200,'[]','2025-06-04 15:32:16','2025-06-04 15:46:33','{\"target_value\":250000000}',0,'loot_value'),
(17,3,'Snake Exterminator','Slay 500 Zulrah.','2',10,'[]','2025-06-04 15:32:16','2025-06-04 15:46:33','{\"target_kc\":500,\"source_npcs\":[\"Zulrah\"]}',1,'item_collection'),
(18,3,'Clue Master','Obtain a 3rd age or gilded piece as a reward from clue scrolls.','3',10,'[]','2025-06-04 15:32:16','2025-06-04 15:46:35','{\"requires\":\"any\",\"required_items\":{\"Gilded chainbody\":\"1\",\"Gilded platelegs\":\"1\",\"Gilded full helm\":\"1\",\"Gilded med helm\":\"1\",\"Gilded hasta\":\"1\",\"Gilded boots\":\"1\",\"Gilded spade\":\"1\",\"Gilded axe\":\"1\",\"Gilded pickaxe\":\"1\",\"Gilded spear\":\"1\",\"Gilded scimitar\":\"1\",\"Gilded 2h sword\":\"1\",\"Gilded sq shield\":\"1\",\"Gilded kiteshield\":\"1\",\"Gilded plateskirt\":\"1\",\"Gilded coif\":\"1\",\"Gilded d\'hide body\":\"1\",\"Gilded d\'hide chaps\":\"1\",\"Gilded d\'hide vambraces\":\"1\",\"3rd age full helmet\":\"1\",\"3rd age platebody\":\"1\",\"3rd age platelegs\":\"1\",\"3rd age kiteshield\":\"1\",\"3rd age plateskirt\":\"1\",\"3rd age longsword\":\"1\",\"3rd age mage hat\":\"1\",\"3rd age robe top\":\"1\",\"3rd age robe\":\"1\",\"3rd age amulet\":\"1\",\"3rd age wand\":\"1\",\"3rd age range coif\":\"1\",\"3rd age range top\":\"1\",\"3rd age range legs\":\"1\",\"3rd age vambraces\":\"1\",\"3rd age bow\":\"1\",\"3rd age druidic robe top\":\"1\",\"3rd age druidic robe bottoms\":\"1\",\"3rd age druidic cloak\":\"1\",\"3rd age druidic staff\":\"1\",\"3rd age axe\":\"1\",\"3rd age felling axe\":\"1\",\"3rd age pickaxe\":\"1\",\"3rd age cloak\":\"1\"}}',1,'item_collection'),
(19,3,'Chambers of Xeric Farmer','Collect 100,000,000 GP in loot from Chambers of Xeric.','3',10,'[]','2025-06-04 15:32:16','2025-06-04 15:46:33','{\"target_value\":100000000,\"source_npcs\":[\"Chambers of Xeric\",\"Chambers of Xeric Challenge Mode\"]}',0,'loot_value'),
(20,3,'Twisted bow, Scythe or Shadow','Obtain a Twisted bow, Scythe of vitur or Tumeken\'s shadow from raids.','4',10,'[]','2025-06-04 15:32:17','2025-06-04 15:46:31','{\"requires\":\"any\",\"required_items\":{\"Scythe of vitur (uncharged)\":\"1\",\"Twisted bow\":\"1\",\"Tumeken\'s shadow (uncharged)\":\"1\"}}',0,'item_collection'),
(21,3,'Mining Specialist','Obtain 5,000,000 mining experience.','2',10,'[]','2025-06-04 15:32:17','2025-06-04 15:46:31','{\"skill_name\":\"mining\",\"target_xp\":5000000}',0,'xp_target'),
(22,3,'Twisted bow, Scythe or Shadow','Obtain a Twisted bow, Scythe of vitur or Tumeken\'s shadow from raids.','4',10,'[]','2025-06-04 15:32:17','2025-06-04 15:46:36','{\"requires\":\"any\",\"required_items\":{\"Scythe of vitur (uncharged)\":\"1\",\"Twisted bow\":\"1\",\"Tumeken\'s shadow (uncharged)\":\"1\"}}',1,'item_collection'),
(23,3,'Complete a Godsword','Obtain any of the five godsword hilts, and each of the three blade parts required to assemble a complete godsword.','2',15,'[]','2025-06-04 15:32:17','2025-06-04 15:46:31','{\"requires\":\"set\",\"sets\":[[\"Bandos hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Armadyl hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Ancient hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Zamorak hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Saradomin hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"]]}',0,'item_collection'),
(24,3,'Twisted Fashion','Receive any ancestral piece, and a twisted ancestral color kit from Chambers of Xeric.','3',10,'[]','2025-06-04 15:32:17','2025-06-04 15:46:35','{\"requires\":\"set\",\"sets\":[[\"Ancestral robe bottom\",\"Twisted ancestral colour kit\"],[\"Ancestral hat\",\"Twisted ancestral colour kit\"],[\"Ancestral robe top\",\"Twisted ancestral colour kit\"]]}',1,'item_collection'),
(25,3,'Theatre of Blood Weapon','Obtain one of the weapons as a reward from the Theatre of Blood.','3',10,'[]','2025-06-04 15:32:17','2025-06-04 15:46:34','{\"requires\":\"any\",\"required_items\":{\"Scythe of vitur (uncharged)\":\"1\",\"Ghrazi rapier\":\"1\",\"Sanguinesti staff (uncharged)\":\"1\"}}',1,'item_collection'),
(26,3,'Champion Cape','Receive ALL champion scrolls available in-game.','5',50,'[]','2025-06-04 15:38:02','2025-06-04 15:46:31','{\"requires\":\"all\",\"required_items\":{\"Earth warrior champion scroll\":\"1\",\"Ghoul champion scroll\":\"1\",\"Giant champion scroll\":\"1\",\"Goblin champion scroll\":\"1\",\"Hobgoblin champion scroll\":\"1\",\"Imp champion scroll\":\"1\",\"Skeleton champion scroll\":\"1\",\"Zombie champion scroll\":\"1\",\"Jogre champion scroll\":\"1\",\"Lesser demon champion scroll\":\"1\"}}',0,'item_collection'),
(27,3,'Godsword','Create on full godsword including three unique shards and the hilt.','1',5,'[]','2025-06-04 15:38:02','2025-06-04 15:46:31','{\"requires\":\"set\",\"sets\":[[\"Armadyl hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Saradomin hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Bandos hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Zamorak hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Ancient hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"]]}',0,'item_collection'),
(28,3,'Godwars Farmer I','Collect 100,000,000 in loot from Godwars Dungeon (excluding Nex)','2',25,'[]','2025-06-04 15:38:02','2025-06-04 15:46:31','{\"target_value\":100000000,\"source_npcs\":[\"General Graardor\",\"Sergeant Steelwill\",\"Sergeant Strongstack\",\"Sergeant Grimspike\",\"Kree\'arra\",\"Flight Kilisa\",\"Flockleader Geerin\",\"Wingman Skree\",\"K\'ril Tsutsaroth\",\"Commander Zilyana\",\"Balfrug Kreeyath\",\"Tstanon Karlak\",\"Zakl\'n Gritch\",\"Starlight\",\"Bree\",\"Growler\"]}',0,'loot_value'),
(29,3,'Godwars Points I','Defeat Godwars Dungeon bosses until you reach the required points.','4',20,'[]','2025-06-04 15:38:03','2025-06-04 15:46:31','{\"requires\":\"points\",\"items\":{\"Godsword shard 1\":\"1\",\"Godsword shard 2\":\"1\",\"Godsword shard 3\":\"1\",\"Armadyl chestplate\":\"3\",\"Armadyl chainskirt\":\"3\",\"Armadyl helmet\":\"2\",\"Armadyl hilt\":\"5\",\"Bandos boots\":\"2\",\"Bandos chestplate\":\"3\",\"Bandos tassets\":\"3\",\"Bandos hilt\":\"5\",\"Zamorak hilt\":\"5\",\"Zamorakian spear\":\"3\",\"Steam battlestaff\":\"2\",\"Staff of the dead\":\"3\",\"Armadyl crossbow\":\"3\",\"Saradomin hilt\":\"5\",\"Saradomin sword\":\"2\",\"Saradomin\'s light\":\"3\",\"Ancient hilt\":\"5\",\"Torva full helm (damaged)\":\"3\",\"Torva platebody (damaged)\":\"3\",\"Torva platelegs (damaged)\":\"3\",\"Nihil horn\":\"5\",\"Zaryte vambraces\":\"4\"}}',0,'item_collection'),
(30,3,'Brimstone ring','Obtain all required pieces from Hydra to assemble a Brimstone ring.','3',20,'[]','2025-06-04 15:38:03','2025-06-04 15:46:31','{\"requires\":\"all\",\"required_items\":{\"Hydra\'s eye\":\"1\",\"Hydra\'s heart\":\"1\",\"Hydra\'s fang\":\"1\"}}',0,'item_collection'),
(31,3,'Health is a Priority','Obtain 50,000,000 Hitpoints experience.','2',10,'[]','2025-06-04 15:38:03','2025-06-04 15:46:31','{\"skill_name\":\"hitpoints\",\"target_xp\":50000000}',0,'xp_target'),
(32,3,'Dragon hunter lance','Obtain a Zamorakian spear and Hydra\'s claw to assemble a Dragon hunter lance.','4',20,'[]','2025-06-04 15:38:03','2025-06-04 15:46:31','{\"requires\":\"all\",\"required_items\":{\"Hydra\'s claw\":\"1\",\"Zamorakian spear\":\"1\"}}',0,'item_collection'),
(33,3,'Complete a Godsword','Obtain any of the five godsword hilts, and each of the three blade parts required to assemble a complete godsword.','2',15,'[]','2025-06-04 15:38:48','2025-06-04 15:46:36','{\"requires\":\"set\",\"sets\":[[\"Bandos hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Armadyl hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Ancient hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Zamorak hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Saradomin hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"]]}',1,'item_collection'),
(34,3,'Dragon hunter lance','Obtain a Zamorakian spear and Hydra\'s claw to assemble a Dragon hunter lance.','4',20,'[]','2025-06-04 15:38:48','2025-06-04 15:46:34','{\"requires\":\"all\",\"required_items\":{\"Hydra\'s claw\":\"1\",\"Zamorakian spear\":\"1\"}}',1,'item_collection'),
(35,3,'Godwars Farmer I','Collect 100,000,000 in loot from Godwars Dungeon (excluding Nex)','2',25,'[]','2025-06-04 15:38:49','2025-06-04 15:46:30','{\"target_value\":100000000,\"source_npcs\":[\"General Graardor\",\"Sergeant Steelwill\",\"Sergeant Strongstack\",\"Sergeant Grimspike\",\"Kree\'arra\",\"Flight Kilisa\",\"Flockleader Geerin\",\"Wingman Skree\",\"K\'ril Tsutsaroth\",\"Commander Zilyana\",\"Balfrug Kreeyath\",\"Tstanon Karlak\",\"Zakl\'n Gritch\",\"Starlight\",\"Bree\",\"Growler\"]}',0,'loot_value'),
(36,3,'Godwars Points I','Defeat Godwars Dungeon bosses until you reach the required points.','4',20,'[]','2025-06-04 15:38:49','2025-06-04 15:46:30','{\"requires\":\"points\",\"items\":{\"Godsword shard 1\":\"1\",\"Godsword shard 2\":\"1\",\"Godsword shard 3\":\"1\",\"Armadyl chestplate\":\"3\",\"Armadyl chainskirt\":\"3\",\"Armadyl helmet\":\"2\",\"Armadyl hilt\":\"5\",\"Bandos boots\":\"2\",\"Bandos chestplate\":\"3\",\"Bandos tassets\":\"3\",\"Bandos hilt\":\"5\",\"Zamorak hilt\":\"5\",\"Zamorakian spear\":\"3\",\"Steam battlestaff\":\"2\",\"Staff of the dead\":\"3\",\"Armadyl crossbow\":\"3\",\"Saradomin hilt\":\"5\",\"Saradomin sword\":\"2\",\"Saradomin\'s light\":\"3\",\"Ancient hilt\":\"5\",\"Torva full helm (damaged)\":\"3\",\"Torva platebody (damaged)\":\"3\",\"Torva platelegs (damaged)\":\"3\",\"Nihil horn\":\"5\",\"Zaryte vambraces\":\"4\"}}',0,'item_collection'),
(37,3,'Brimstone ring','Obtain all required pieces from Hydra to assemble a Brimstone ring.','3',20,'[]','2025-06-04 15:38:49','2025-06-04 15:46:30','{\"requires\":\"all\",\"required_items\":{\"Hydra\'s eye\":\"1\",\"Hydra\'s heart\":\"1\",\"Hydra\'s fang\":\"1\"}}',0,'item_collection'),
(38,3,'Health is a Priority','Obtain 50,000,000 Hitpoints experience.','2',10,'[]','2025-06-04 15:38:49','2025-06-04 15:46:30','{\"skill_name\":\"hitpoints\",\"target_xp\":50000000}',0,'xp_target'),
(39,3,'Champion Cape','Receive ALL champion scrolls available in-game.','5',50,'[]','2025-06-04 15:38:49','2025-06-04 15:46:33','{\"requires\":\"all\",\"required_items\":{\"Earth warrior champion scroll\":\"1\",\"Ghoul champion scroll\":\"1\",\"Giant champion scroll\":\"1\",\"Goblin champion scroll\":\"1\",\"Hobgoblin champion scroll\":\"1\",\"Imp champion scroll\":\"1\",\"Skeleton champion scroll\":\"1\",\"Zombie champion scroll\":\"1\",\"Jogre champion scroll\":\"1\",\"Lesser demon champion scroll\":\"1\"}}',1,'item_collection'),
(40,3,'Godsword','Create on full godsword including three unique shards and the hilt.','1',5,'[]','2025-06-04 15:39:33','2025-06-04 15:46:30','{\"requires\":\"set\",\"sets\":[[\"Armadyl hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Saradomin hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Bandos hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Zamorak hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Ancient hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"]]}',0,'item_collection'),
(41,3,'Mining Specialist','Obtain 5,000,000 mining experience.','2',10,'[]','2025-06-04 15:39:33','2025-06-04 15:46:36','{\"skill_name\":\"mining\",\"target_xp\":5000000}',1,'loot_value'),
(42,3,'Godwars Points I','Defeat Godwars Dungeon bosses until you reach the required points.','4',20,'[]','2025-06-04 15:39:34','2025-06-04 15:46:30','{\"requires\":\"points\",\"items\":{\"Godsword shard 1\":\"1\",\"Godsword shard 2\":\"1\",\"Godsword shard 3\":\"1\",\"Armadyl chestplate\":\"3\",\"Armadyl chainskirt\":\"3\",\"Armadyl helmet\":\"2\",\"Armadyl hilt\":\"5\",\"Bandos boots\":\"2\",\"Bandos chestplate\":\"3\",\"Bandos tassets\":\"3\",\"Bandos hilt\":\"5\",\"Zamorak hilt\":\"5\",\"Zamorakian spear\":\"3\",\"Steam battlestaff\":\"2\",\"Staff of the dead\":\"3\",\"Armadyl crossbow\":\"3\",\"Saradomin hilt\":\"5\",\"Saradomin sword\":\"2\",\"Saradomin\'s light\":\"3\",\"Ancient hilt\":\"5\",\"Torva full helm (damaged)\":\"3\",\"Torva platebody (damaged)\":\"3\",\"Torva platelegs (damaged)\":\"3\",\"Nihil horn\":\"5\",\"Zaryte vambraces\":\"4\"}}',0,'item_collection'),
(43,3,'Magic fang','Obtain a magic fang from Zulrah.','1',10,'[]','2025-06-04 15:39:34','2025-06-04 15:46:36','{\"requires\":\"all\",\"required_items\":{\"Magic fang\":\"1\"}}',1,'item_collection'),
(44,3,'Zulrah Unique','Obtain any of the four unique drops from zulrah, or a mutagen.','2',10,'[]','2025-06-04 15:39:34','2025-06-04 15:46:35','{\"requires\":\"any\",\"required_items\":{\"Uncut onyx\":\"1\",\"Serpentine visage\":\"1\",\"Magic fang\":\"1\",\"Tanzanite fang\":\"1\",\"Magma mutagen\":\"1\",\"Tanzanite mutagen\":\"1\"}}',1,'xp_target'),
(45,3,'Reading Specialist','Obtain all three elemental books (tome of water, tome of fire, time of earth)','2',10,'[]','2025-06-04 15:39:34','2025-06-04 15:46:36','{\"requires\":\"all\",\"required_items\":{\"Tome of water (empty)\":\"1\",\"Tome of earth (empty)\":\"1\",\"Tome of fire (empty)\":\"1\"}}',1,'item_collection'),
(46,3,'Brimstone ring','Obtain all required pieces from Hydra to assemble a Brimstone ring.','3',20,'[]','2025-06-04 15:39:35','2025-06-04 15:46:34','{\"requires\":\"all\",\"required_items\":{\"Hydra\'s eye\":\"1\",\"Hydra\'s heart\":\"1\",\"Hydra\'s fang\":\"1\"}}',1,'item_collection'),
(47,3,'Mining Specialist','Obtain 5,000,000 mining experience.','2',10,'[]','2025-06-04 15:39:35','2025-06-04 15:46:30','{\"skill_name\":\"mining\",\"target_xp\":5000000}',0,'xp_target'),
(48,3,'Magic fang','Obtain a magic fang from Zulrah.','1',10,'[]','2025-06-04 15:39:35','2025-06-04 15:46:30','{\"requires\":\"all\",\"required_items\":{\"Magic fang\":\"1\"}}',0,'item_collection'),
(49,3,'Godsword','Create on full godsword including three unique shards and the hilt.','1',5,'[]','2025-06-04 15:39:35','2025-06-04 15:46:34','{\"requires\":\"set\",\"sets\":[[\"Armadyl hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Saradomin hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Bandos hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Zamorak hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"],[\"Ancient hilt\",\"Godsword shard 1\",\"Godsword shard 2\",\"Godsword shard 3\"]]}',1,'item_collection'),
(50,3,'Godwars Farmer I','Collect 100,000,000 in loot from Godwars Dungeon (excluding Nex)','2',25,'[]','2025-06-04 15:45:33','2025-06-04 15:46:29','{\"target_value\":100000000,\"source_npcs\":[\"General Graardor\",\"Sergeant Steelwill\",\"Sergeant Strongstack\",\"Sergeant Grimspike\",\"Kree\'arra\",\"Flight Kilisa\",\"Flockleader Geerin\",\"Wingman Skree\",\"K\'ril Tsutsaroth\",\"Commander Zilyana\",\"Balfrug Kreeyath\",\"Tstanon Karlak\",\"Zakl\'n Gritch\",\"Starlight\",\"Bree\",\"Growler\"]}',0,'loot_value'),
(51,3,'Godwars Points I','Defeat Godwars Dungeon bosses until you reach the required points.','4',20,'[]','2025-06-04 15:45:33','2025-06-04 15:46:29','{\"requires\":\"points\",\"items\":{\"Godsword shard 1\":\"1\",\"Godsword shard 2\":\"1\",\"Godsword shard 3\":\"1\",\"Armadyl chestplate\":\"3\",\"Armadyl chainskirt\":\"3\",\"Armadyl helmet\":\"2\",\"Armadyl hilt\":\"5\",\"Bandos boots\":\"2\",\"Bandos chestplate\":\"3\",\"Bandos tassets\":\"3\",\"Bandos hilt\":\"5\",\"Zamorak hilt\":\"5\",\"Zamorakian spear\":\"3\",\"Steam battlestaff\":\"2\",\"Staff of the dead\":\"3\",\"Armadyl crossbow\":\"3\",\"Saradomin hilt\":\"5\",\"Saradomin sword\":\"2\",\"Saradomin\'s light\":\"3\",\"Ancient hilt\":\"5\",\"Torva full helm (damaged)\":\"3\",\"Torva platebody (damaged)\":\"3\",\"Torva platelegs (damaged)\":\"3\",\"Nihil horn\":\"5\",\"Zaryte vambraces\":\"4\"}}',0,'item_collection'),
(52,3,'Health is a Priority','Obtain 50,000,000 Hitpoints experience.','2',10,'[]','2025-06-04 15:45:33','2025-06-04 15:46:29','{\"skill_name\":\"hitpoints\",\"target_xp\":50000000}',0,'xp_target'),
(53,3,'Godwars Points I','Defeat Godwars Dungeon bosses until you reach the required points.','4',20,'[]','2025-06-04 15:45:33','2025-06-04 15:46:34','{\"requires\":\"points\",\"items\":{\"Godsword shard 1\":\"1\",\"Godsword shard 2\":\"1\",\"Godsword shard 3\":\"1\",\"Armadyl chestplate\":\"3\",\"Armadyl chainskirt\":\"3\",\"Armadyl helmet\":\"2\",\"Armadyl hilt\":\"5\",\"Bandos boots\":\"2\",\"Bandos chestplate\":\"3\",\"Bandos tassets\":\"3\",\"Bandos hilt\":\"5\",\"Zamorak hilt\":\"5\",\"Zamorakian spear\":\"3\",\"Steam battlestaff\":\"2\",\"Staff of the dead\":\"3\",\"Armadyl crossbow\":\"3\",\"Saradomin hilt\":\"5\",\"Saradomin sword\":\"2\",\"Saradomin\'s light\":\"3\",\"Ancient hilt\":\"5\",\"Torva full helm (damaged)\":\"3\",\"Torva platebody (damaged)\":\"3\",\"Torva platelegs (damaged)\":\"3\",\"Nihil horn\":\"5\",\"Zaryte vambraces\":\"4\"}}',1,'item_collection'),
(54,3,'Godwars Farmer I','Collect 100,000,000 in loot from Godwars Dungeon (excluding Nex)','2',25,'[]','2025-06-04 15:46:34','2025-06-04 15:46:34','{\"target_value\":100000000,\"source_npcs\":[\"General Graardor\",\"Sergeant Steelwill\",\"Sergeant Strongstack\",\"Sergeant Grimspike\",\"Kree\'arra\",\"Flight Kilisa\",\"Flockleader Geerin\",\"Wingman Skree\",\"K\'ril Tsutsaroth\",\"Commander Zilyana\",\"Balfrug Kreeyath\",\"Tstanon Karlak\",\"Zakl\'n Gritch\",\"Starlight\",\"Bree\",\"Growler\"]}',1,'loot_value'),
(55,3,'Health is a Priority','Obtain 50,000,000 Hitpoints experience.','2',10,'[]','2025-06-04 15:46:34','2025-06-04 15:46:34','{\"skill_name\":\"hitpoints\",\"target_xp\":50000000}',1,'xp_target'),
(56,3,'Special Catch','Obtain a Golden Tench from Aerial fishing.','3',10,'[]','2025-06-04 15:46:35','2025-06-04 15:46:35','{\"requires\":\"any\",\"required_items\":{\"Golden tench\":\"1\"}}',1,'item_collection'),
(57,3,'Health is a Priority','Obtain 50,000,000 Hitpoints experience.','2',10,'[]','2025-06-04 17:26:19','2025-06-04 17:26:19','{\"skill_name\":\"hitpoints\",\"target_xp\":50000000}',1,'xp_target');
/*!40000 ALTER TABLE `event_tasks` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_teams`
--

DROP TABLE IF EXISTS `event_teams`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_teams` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `current_location` varchar(255) DEFAULT NULL,
  `previous_location` varchar(255) DEFAULT NULL,
  `name` varchar(255) NOT NULL,
  `points` int(11) NOT NULL,
  `gold` int(11) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT '0000-00-00 00:00:00',
  `current_task` int(11) DEFAULT NULL,
  `task_progress` int(11) DEFAULT NULL,
  `mercy_rule` datetime DEFAULT NULL,
  `mercy_count` int(11) NOT NULL,
  `turn_number` int(11) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `event_id` (`event_id`),
  CONSTRAINT `event_teams_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=14 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_teams`
--

LOCK TABLES `event_teams` WRITE;
/*!40000 ALTER TABLE `event_teams` DISABLE KEYS */;
INSERT INTO `event_teams` VALUES
(9,3,NULL,NULL,'Alpha',0,100,'2025-06-01 14:55:10','2025-06-01 18:55:10',NULL,NULL,NULL,0,1),
(10,3,NULL,NULL,'Bravo',0,100,'2025-06-01 18:54:28','2025-06-01 18:54:28',NULL,NULL,NULL,0,1),
(11,3,NULL,NULL,'Charlie',0,100,'2025-06-01 18:54:33','2025-06-01 18:54:33',NULL,NULL,NULL,0,1),
(12,3,'','','Delta',0,100,'2025-06-04 13:33:04','2025-06-04 17:33:04',0,0,NULL,0,1);
/*!40000 ALTER TABLE `event_teams` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_participants`
--

DROP TABLE IF EXISTS `event_participants`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_participants` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `user_id` int(11) NOT NULL,
  `player_id` int(11) NOT NULL,
  `team_id` int(11) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT '0000-00-00 00:00:00',
  `points` int(11) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `event_id` (`event_id`),
  KEY `user_id` (`user_id`),
  KEY `player_id` (`player_id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `event_participants_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`),
  CONSTRAINT `event_participants_ibfk_2` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`),
  CONSTRAINT `event_participants_ibfk_3` FOREIGN KEY (`player_id`) REFERENCES `players` (`player_id`),
  CONSTRAINT `event_participants_ibfk_4` FOREIGN KEY (`team_id`) REFERENCES `event_teams` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_participants`
--

LOCK TABLES `event_participants` WRITE;
/*!40000 ALTER TABLE `event_participants` DISABLE KEYS */;
INSERT INTO `event_participants` VALUES
(2,3,0,1,9,'2025-06-04 14:20:14','0000-00-00 00:00:00',100);
/*!40000 ALTER TABLE `event_participants` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_configurations`
--

DROP TABLE IF EXISTS `event_configurations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_configurations` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `config_key` varchar(255) NOT NULL,
  `config_value` varchar(255) NOT NULL,
  `long_value` text DEFAULT NULL,
  `update_number` int(11) NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `event_id` (`event_id`),
  CONSTRAINT `event_configurations_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_configurations`
--

LOCK TABLES `event_configurations` WRITE;
/*!40000 ALTER TABLE `event_configurations` DISABLE KEYS */;
INSERT INTO `event_configurations` VALUES
(1,3,'event_notice_channel_id','1378807189459959978',NULL,8,'2025-06-01 14:07:02','2025-06-02 09:44:24'),
(2,3,'team_selection_method','random',NULL,1,'2025-06-01 18:53:03','2025-06-02 09:44:24');
/*!40000 ALTER TABLE `event_configurations` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_items`
--

DROP TABLE IF EXISTS `event_items`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_items` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `description` text DEFAULT NULL,
  `cooldown` int(11) NOT NULL,
  `event_id` int(11) NOT NULL,
  `cost` int(11) NOT NULL,
  `effect` text DEFAULT NULL,
  `emoji` varchar(255) DEFAULT NULL,
  `item_type` varchar(255) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT '0000-00-00 00:00:00',
  `effect_long` text DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `event_id` (`event_id`),
  CONSTRAINT `event_items_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_items`
--

LOCK TABLES `event_items` WRITE;
/*!40000 ALTER TABLE `event_items` DISABLE KEYS */;
/*!40000 ALTER TABLE `event_items` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_notifications`
--

DROP TABLE IF EXISTS `event_notifications`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_notifications` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `notification_type` varchar(50) NOT NULL,
  `group_id` int(11) NOT NULL,
  `message` text NOT NULL,
  `data` text DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `processed_at` datetime DEFAULT NULL,
  `status` varchar(20) NOT NULL,
  `error_message` text DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uix_event_notification_unique` (`event_id`,`notification_type`,`group_id`,`data`) USING HASH,
  KEY `event_id` (`event_id`),
  KEY `group_id` (`group_id`),
  CONSTRAINT `event_notifications_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`),
  CONSTRAINT `event_notifications_ibfk_2` FOREIGN KEY (`group_id`) REFERENCES `groups` (`group_id`)
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_notifications`
--

LOCK TABLES `event_notifications` WRITE;
/*!40000 ALTER TABLE `event_notifications` DISABLE KEYS */;
INSERT INTO `event_notifications` VALUES
(1,3,'player_invite_message',2,'Admin sent player invite button to join 3',NULL,'2025-06-01 15:32:18',NULL,'failed','the JSON object must be str, bytes or bytearray, not NoneType'),
(2,3,'player_invite_message',2,'Admin sent player invite button to join 3',NULL,'2025-06-01 15:34:02',NULL,'failed','type object \'EventConfigModel\' has no attribute \'config_type\''),
(3,3,'player_invite_message',2,'Admin sent player invite button to join 3',NULL,'2025-06-01 15:34:51',NULL,'failed','ActionRow.__init__() got an unexpected keyword argument \'components\''),
(4,3,'player_invite_message',2,'Admin sent player invite button to join 3',NULL,'2025-06-01 15:35:24','2025-06-01 15:35:27','sent',NULL),
(5,3,'player_invite_message',2,'Admin sent player invite button to join 3',NULL,'2025-06-04 13:43:01',NULL,'failed','\'NoneType\' object has no attribute \'timestamp\''),
(6,3,'player_invite_message',2,'Admin sent player invite button to join 3',NULL,'2025-06-04 13:45:01','2025-06-04 13:45:04','sent',NULL);
/*!40000 ALTER TABLE `event_notifications` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_team_cooldowns`
--

DROP TABLE IF EXISTS `event_team_cooldowns`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_team_cooldowns` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `team_id` int(11) NOT NULL,
  `cooldown_name` varchar(255) NOT NULL,
  `remaining_turns` int(11) NOT NULL,
  `expiry_date` datetime NOT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `event_team_cooldowns_ibfk_1` FOREIGN KEY (`team_id`) REFERENCES `event_teams` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_team_cooldowns`
--

LOCK TABLES `event_team_cooldowns` WRITE;
/*!40000 ALTER TABLE `event_team_cooldowns` DISABLE KEYS */;
/*!40000 ALTER TABLE `event_team_cooldowns` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_team_effects`
--

DROP TABLE IF EXISTS `event_team_effects`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_team_effects` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `team_id` int(11) NOT NULL,
  `effect_name` varchar(255) NOT NULL,
  `remaining_turns` int(11) NOT NULL,
  `expiry_date` datetime NOT NULL,
  `effect_data` text DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `event_team_effects_ibfk_1` FOREIGN KEY (`team_id`) REFERENCES `event_teams` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_team_effects`
--

LOCK TABLES `event_team_effects` WRITE;
/*!40000 ALTER TABLE `event_team_effects` DISABLE KEYS */;
/*!40000 ALTER TABLE `event_team_effects` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `event_team_inventory`
--

DROP TABLE IF EXISTS `event_team_inventory`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `event_team_inventory` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event_team_id` int(11) NOT NULL,
  `item_id` int(11) NOT NULL,
  `quantity` int(11) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT '0000-00-00 00:00:00',
  PRIMARY KEY (`id`),
  KEY `event_team_id` (`event_team_id`),
  KEY `item_id` (`item_id`),
  CONSTRAINT `event_team_inventory_ibfk_1` FOREIGN KEY (`event_team_id`) REFERENCES `event_teams` (`id`),
  CONSTRAINT `event_team_inventory_ibfk_2` FOREIGN KEY (`item_id`) REFERENCES `event_items` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `event_team_inventory`
--

LOCK TABLES `event_team_inventory` WRITE;
/*!40000 ALTER TABLE `event_team_inventory` DISABLE KEYS */;
/*!40000 ALTER TABLE `event_team_inventory` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-07-05  0:37:09
/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19  Distrib 10.11.14-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: localhost    Database: data
-- ------------------------------------------------------
-- Server version	10.11.14-MariaDB-0+deb12u2

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `bingo_boards`
--

DROP TABLE IF EXISTS `bingo_boards`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `bingo_boards` (
  `board_id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `team_id` int(11) DEFAULT NULL,
  PRIMARY KEY (`board_id`),
  KEY `event_id` (`event_id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `bingo_boards_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`),
  CONSTRAINT `bingo_boards_ibfk_2` FOREIGN KEY (`team_id`) REFERENCES `event_teams` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `bingo_boards`
--

LOCK TABLES `bingo_boards` WRITE;
/*!40000 ALTER TABLE `bingo_boards` DISABLE KEYS */;
INSERT INTO `bingo_boards` VALUES
(2,3,NULL);
/*!40000 ALTER TABLE `bingo_boards` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-07-05  0:37:09
/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19  Distrib 10.11.14-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: localhost    Database: data
-- ------------------------------------------------------
-- Server version	10.11.14-MariaDB-0+deb12u2

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `bingo_games`
--

DROP TABLE IF EXISTS `bingo_games`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `bingo_games` (
  `game_id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `individual_boards` tinyint(1) NOT NULL,
  `board_size` int(11) NOT NULL,
  `win_condition` varchar(50) NOT NULL,
  `center_free` tinyint(1) NOT NULL,
  PRIMARY KEY (`game_id`),
  KEY `event_id` (`event_id`),
  CONSTRAINT `bingo_games_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `bingo_games`
--

LOCK TABLES `bingo_games` WRITE;
/*!40000 ALTER TABLE `bingo_games` DISABLE KEYS */;
INSERT INTO `bingo_games` VALUES
(1,3,0,5,'blackout',0);
/*!40000 ALTER TABLE `bingo_games` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-07-05  0:37:09
/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19  Distrib 10.11.14-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: localhost    Database: data
-- ------------------------------------------------------
-- Server version	10.11.14-MariaDB-0+deb12u2

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `assigned_tasks`
--

DROP TABLE IF EXISTS `assigned_tasks`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `assigned_tasks` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `event_id` int(11) NOT NULL,
  `team_id` int(11) NOT NULL,
  `task_id` int(11) NOT NULL,
  `status` varchar(255) NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `data` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL CHECK (json_valid(`data`)),
  `is_active` tinyint(1) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `event_id` (`event_id`),
  KEY `task_id` (`task_id`),
  KEY `team_id` (`team_id`),
  CONSTRAINT `assigned_tasks_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `events` (`id`),
  CONSTRAINT `assigned_tasks_ibfk_2` FOREIGN KEY (`task_id`) REFERENCES `event_tasks` (`id`),
  CONSTRAINT `assigned_tasks_ibfk_3` FOREIGN KEY (`team_id`) REFERENCES `event_teams` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=195 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `assigned_tasks`
--

LOCK TABLES `assigned_tasks` WRITE;
/*!40000 ALTER TABLE `assigned_tasks` DISABLE KEYS */;
INSERT INTO `assigned_tasks` VALUES
(1,3,9,1,'draft','2025-06-04 15:32:17','2025-06-04 17:17:49','[]',1),
(2,3,9,2,'draft','2025-06-04 15:32:17','2025-06-04 17:21:35','[]',1),
(3,3,9,3,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(4,3,9,4,'draft','2025-06-04 15:32:17','2025-06-04 17:21:35','[]',1),
(5,3,9,5,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(6,3,9,6,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(7,3,9,7,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(8,3,9,8,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(9,3,9,9,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(10,3,9,10,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(11,3,9,11,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(12,3,9,12,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(13,3,9,13,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(14,3,9,14,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(15,3,9,15,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(16,3,9,16,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(17,3,9,17,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(18,3,9,18,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(19,3,9,19,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(20,3,9,20,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(21,3,9,21,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(22,3,9,22,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(23,3,9,23,'pending','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',0),
(24,3,9,24,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(25,3,9,25,'draft','2025-06-04 15:32:17','2025-06-04 17:23:45','[]',1),
(26,3,10,1,'created','2025-06-04 15:32:17','2025-06-04 15:46:40','[]',1),
(27,3,10,2,'created','2025-06-04 15:32:17','2025-06-04 15:46:40','[]',1),
(28,3,10,3,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(29,3,10,4,'created','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',1),
(30,3,10,5,'created','2025-06-04 15:32:17','2025-06-04 15:46:40','[]',1),
(31,3,10,6,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(32,3,10,7,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(33,3,10,8,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(34,3,10,9,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(35,3,10,10,'created','2025-06-04 15:32:17','2025-06-04 15:46:40','[]',1),
(36,3,10,11,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(37,3,10,12,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(38,3,10,13,'created','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',1),
(39,3,10,14,'created','2025-06-04 15:32:17','2025-06-04 15:46:40','[]',1),
(40,3,10,15,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(41,3,10,16,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(42,3,10,17,'created','2025-06-04 15:32:17','2025-06-04 15:46:39','[]',1),
(43,3,10,18,'created','2025-06-04 15:32:17','2025-06-04 15:46:40','[]',1),
(44,3,10,19,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(45,3,10,20,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(46,3,10,21,'pending','2025-06-04 15:32:17','2025-06-04 15:46:41','[]',0),
(47,3,10,22,'created','2025-06-04 15:32:18','2025-06-04 15:46:40','[]',1),
(48,3,10,23,'pending','2025-06-04 15:32:18','2025-06-04 15:46:41','[]',0),
(49,3,10,24,'created','2025-06-04 15:32:18','2025-06-04 15:46:40','[]',1),
(50,3,10,25,'created','2025-06-04 15:32:18','2025-06-04 15:46:39','[]',1),
(51,3,11,1,'created','2025-06-04 15:32:18','2025-06-04 15:46:43','[]',1),
(52,3,11,2,'created','2025-06-04 15:32:18','2025-06-04 15:46:43','[]',1),
(53,3,11,3,'pending','2025-06-04 15:32:18','2025-06-04 15:46:44','[]',0),
(54,3,11,4,'created','2025-06-04 15:32:18','2025-06-04 15:46:42','[]',1),
(55,3,11,5,'created','2025-06-04 15:32:18','2025-06-04 15:46:43','[]',1),
(56,3,11,6,'pending','2025-06-04 15:32:18','2025-06-04 15:46:44','[]',0),
(57,3,11,7,'pending','2025-06-04 15:32:18','2025-06-04 15:46:44','[]',0),
(58,3,11,8,'pending','2025-06-04 15:32:18','2025-06-04 15:46:44','[]',0),
(59,3,11,9,'pending','2025-06-04 15:32:18','2025-06-04 15:46:44','[]',0),
(60,3,11,10,'created','2025-06-04 15:32:18','2025-06-04 15:46:44','[]',1),
(61,3,11,11,'pending','2025-06-04 15:32:18','2025-06-04 15:46:44','[]',0),
(62,3,11,12,'pending','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',0),
(63,3,11,13,'created','2025-06-04 15:32:18','2025-06-04 15:46:42','[]',1),
(64,3,11,14,'created','2025-06-04 15:32:18','2025-06-04 15:46:43','[]',1),
(65,3,11,15,'pending','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',0),
(66,3,11,16,'pending','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',0),
(67,3,11,17,'created','2025-06-04 15:32:18','2025-06-04 15:46:42','[]',1),
(68,3,11,18,'created','2025-06-04 15:32:18','2025-06-04 15:46:43','[]',1),
(69,3,11,19,'pending','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',0),
(70,3,11,20,'pending','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',0),
(71,3,11,21,'pending','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',0),
(72,3,11,22,'created','2025-06-04 15:32:18','2025-06-04 15:46:43','[]',1),
(73,3,11,23,'pending','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',0),
(74,3,11,24,'created','2025-06-04 15:32:18','2025-06-04 15:46:43','[]',1),
(75,3,11,25,'created','2025-06-04 15:32:18','2025-06-04 15:46:42','[]',1),
(76,3,12,1,'created','2025-06-04 15:32:18','2025-06-04 15:46:46','[]',1),
(77,3,12,2,'created','2025-06-04 15:32:18','2025-06-04 15:46:46','[]',1),
(78,3,12,3,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(79,3,12,4,'created','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',1),
(80,3,12,5,'created','2025-06-04 15:32:18','2025-06-04 15:46:46','[]',1),
(81,3,12,6,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(82,3,12,7,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(83,3,12,8,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(84,3,12,9,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(85,3,12,10,'created','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',1),
(86,3,12,11,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(87,3,12,12,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(88,3,12,13,'created','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',1),
(89,3,12,14,'created','2025-06-04 15:32:18','2025-06-04 15:46:46','[]',1),
(90,3,12,15,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(91,3,12,16,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(92,3,12,17,'created','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',1),
(93,3,12,18,'created','2025-06-04 15:32:18','2025-06-04 15:46:46','[]',1),
(94,3,12,19,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(95,3,12,20,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(96,3,12,21,'pending','2025-06-04 15:32:18','2025-06-04 15:46:47','[]',0),
(97,3,12,22,'created','2025-06-04 15:32:18','2025-06-04 15:46:46','[]',1),
(98,3,12,23,'pending','2025-06-04 15:32:18','2025-06-04 15:46:48','[]',0),
(99,3,12,24,'created','2025-06-04 15:32:18','2025-06-04 15:46:46','[]',1),
(100,3,12,25,'created','2025-06-04 15:32:18','2025-06-04 15:46:45','[]',1),
(126,3,9,39,'draft','2025-06-04 15:39:36','2025-06-04 17:23:46','[]',1),
(127,3,9,40,'pending','2025-06-04 15:39:36','2025-06-04 15:46:39','[]',0),
(128,3,9,41,'draft','2025-06-04 15:39:36','2025-06-04 17:23:46','[]',1),
(129,3,9,42,'pending','2025-06-04 15:39:36','2025-06-04 15:46:39','[]',0),
(130,3,9,43,'draft','2025-06-04 15:39:36','2025-06-04 17:23:46','[]',1),
(131,3,9,44,'draft','2025-06-04 15:39:36','2025-06-04 17:25:45','[]',1),
(132,3,9,34,'draft','2025-06-04 15:39:36','2025-06-04 17:25:45','[]',1),
(133,3,9,45,'draft','2025-06-04 15:39:36','2025-06-04 17:25:45','[]',1),
(134,3,9,46,'draft','2025-06-04 15:39:37','2025-06-04 17:25:45','[]',1),
(135,3,9,47,'pending','2025-06-04 15:39:37','2025-06-04 15:46:39','[]',0),
(136,3,9,48,'pending','2025-06-04 15:39:37','2025-06-04 15:46:39','[]',0),
(137,3,9,33,'draft','2025-06-04 15:39:37','2025-06-04 17:25:45','[]',1),
(138,3,9,49,'draft','2025-06-04 15:39:37','2025-06-04 17:25:45','[]',1),
(139,3,9,54,'draft','2025-06-04 15:46:37','2025-06-04 17:25:45','[]',1),
(140,3,9,53,'draft','2025-06-04 15:46:37','2025-06-04 17:25:45','[]',1),
(141,3,9,55,'draft','2025-06-04 15:46:37','2025-06-04 17:27:47','[]',1),
(142,3,9,56,'draft','2025-06-04 15:46:38','2025-06-04 17:27:47','[]',1),
(143,3,10,39,'created','2025-06-04 15:46:39','2025-06-04 15:46:39','[]',1),
(144,3,10,49,'created','2025-06-04 15:46:39','2025-06-04 15:46:39','[]',1),
(145,3,10,54,'created','2025-06-04 15:46:39','2025-06-04 15:46:39','[]',1),
(146,3,10,53,'created','2025-06-04 15:46:39','2025-06-04 15:46:39','[]',1),
(147,3,10,46,'created','2025-06-04 15:46:39','2025-06-04 15:46:39','[]',1),
(148,3,10,55,'created','2025-06-04 15:46:39','2025-06-04 15:46:39','[]',1),
(149,3,10,34,'created','2025-06-04 15:46:40','2025-06-04 15:46:40','[]',1),
(150,3,10,44,'created','2025-06-04 15:46:40','2025-06-04 15:46:40','[]',1),
(151,3,10,56,'created','2025-06-04 15:46:40','2025-06-04 15:46:40','[]',1),
(152,3,10,41,'created','2025-06-04 15:46:40','2025-06-04 15:46:40','[]',1),
(153,3,10,43,'created','2025-06-04 15:46:40','2025-06-04 15:46:40','[]',1),
(154,3,10,33,'created','2025-06-04 15:46:40','2025-06-04 15:46:40','[]',1),
(155,3,10,45,'created','2025-06-04 15:46:40','2025-06-04 15:46:40','[]',1),
(156,3,11,39,'created','2025-06-04 15:46:42','2025-06-04 15:46:42','[]',1),
(157,3,11,49,'created','2025-06-04 15:46:42','2025-06-04 15:46:42','[]',1),
(158,3,11,54,'created','2025-06-04 15:46:42','2025-06-04 15:46:42','[]',1),
(159,3,11,53,'created','2025-06-04 15:46:42','2025-06-04 15:46:42','[]',1),
(160,3,11,46,'created','2025-06-04 15:46:42','2025-06-04 15:46:42','[]',1),
(161,3,11,55,'created','2025-06-04 15:46:42','2025-06-04 15:46:42','[]',1),
(162,3,11,34,'created','2025-06-04 15:46:42','2025-06-04 15:46:42','[]',1),
(163,3,11,44,'created','2025-06-04 15:46:43','2025-06-04 15:46:43','[]',1),
(164,3,11,56,'created','2025-06-04 15:46:43','2025-06-04 15:46:43','[]',1),
(165,3,11,41,'created','2025-06-04 15:46:44','2025-06-04 15:46:44','[]',1),
(166,3,11,43,'created','2025-06-04 15:46:44','2025-06-04 15:46:44','[]',1),
(167,3,11,33,'created','2025-06-04 15:46:44','2025-06-04 15:46:44','[]',1),
(168,3,11,45,'created','2025-06-04 15:46:44','2025-06-04 15:46:44','[]',1),
(169,3,12,39,'created','2025-06-04 15:46:45','2025-06-04 15:46:45','[]',1),
(170,3,12,49,'created','2025-06-04 15:46:45','2025-06-04 15:46:45','[]',1),
(171,3,12,54,'created','2025-06-04 15:46:45','2025-06-04 15:46:45','[]',1),
(172,3,12,53,'created','2025-06-04 15:46:45','2025-06-04 15:46:45','[]',1),
(173,3,12,46,'created','2025-06-04 15:46:45','2025-06-04 15:46:45','[]',1),
(174,3,12,55,'created','2025-06-04 15:46:45','2025-06-04 15:46:45','[]',1),
(175,3,12,34,'created','2025-06-04 15:46:46','2025-06-04 15:46:46','[]',1),
(176,3,12,44,'created','2025-06-04 15:46:46','2025-06-04 15:46:46','[]',1),
(177,3,12,56,'created','2025-06-04 15:46:46','2025-06-04 15:46:46','[]',1),
(178,3,12,41,'created','2025-06-04 15:46:46','2025-06-04 15:46:46','[]',1),
(179,3,12,43,'created','2025-06-04 15:46:46','2025-06-04 15:46:46','[]',1),
(180,3,12,33,'created','2025-06-04 15:46:46','2025-06-04 15:46:46','[]',1),
(181,3,12,45,'created','2025-06-04 15:46:47','2025-06-04 15:46:47','[]',1);
/*!40000 ALTER TABLE `assigned_tasks` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-07-05  0:37:09
/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19  Distrib 10.11.14-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: localhost    Database: data
-- ------------------------------------------------------
-- Server version	10.11.14-MariaDB-0+deb12u2

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `bingo_board_tiles`
--

DROP TABLE IF EXISTS `bingo_board_tiles`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `bingo_board_tiles` (
  `tile_id` int(11) NOT NULL AUTO_INCREMENT,
  `board_id` int(11) NOT NULL,
  `task_id` int(11) NOT NULL,
  `position_x` int(11) NOT NULL,
  `position_y` int(11) NOT NULL,
  `status` varchar(50) NOT NULL,
  `completed_by_team_id` int(11) DEFAULT NULL,
  `date_completed` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `assigned_id` int(11) DEFAULT NULL,
  PRIMARY KEY (`tile_id`),
  KEY `board_id` (`board_id`),
  KEY `completed_by_team_id` (`completed_by_team_id`),
  KEY `assigned_id` (`assigned_id`),
  KEY `task_id` (`task_id`),
  CONSTRAINT `bingo_board_tiles_ibfk_1` FOREIGN KEY (`board_id`) REFERENCES `bingo_boards` (`board_id`),
  CONSTRAINT `bingo_board_tiles_ibfk_2` FOREIGN KEY (`completed_by_team_id`) REFERENCES `event_teams` (`id`),
  CONSTRAINT `bingo_board_tiles_ibfk_3` FOREIGN KEY (`assigned_id`) REFERENCES `assigned_tasks` (`id`),
  CONSTRAINT `bingo_board_tiles_ibfk_4` FOREIGN KEY (`task_id`) REFERENCES `base_tasks` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=51 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `bingo_board_tiles`
--

LOCK TABLES `bingo_board_tiles` WRITE;
/*!40000 ALTER TABLE `bingo_board_tiles` DISABLE KEYS */;
INSERT INTO `bingo_board_tiles` VALUES
(26,2,17,0,0,'pending',NULL,NULL,'2025-06-04 15:46:50','2025-06-04 15:46:50',NULL),
(27,2,39,1,0,'pending',NULL,NULL,'2025-06-04 15:46:50','2025-06-04 15:46:50',NULL),
(28,2,49,2,0,'pending',NULL,NULL,'2025-06-04 15:46:50','2025-06-04 15:46:50',NULL),
(29,2,25,3,0,'pending',NULL,NULL,'2025-06-04 15:46:50','2025-06-04 15:46:50',NULL),
(30,2,54,4,0,'pending',NULL,NULL,'2025-06-04 15:46:50','2025-06-04 15:46:50',NULL),
(31,2,13,0,1,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(32,2,53,1,1,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(33,2,46,2,1,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(34,2,57,3,1,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(35,2,4,4,1,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(36,2,34,0,2,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(37,2,5,1,2,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(38,2,24,2,2,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(39,2,18,3,2,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(40,2,44,4,2,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(41,2,14,0,3,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(42,2,2,1,3,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(43,2,56,2,3,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(44,2,1,3,3,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(45,2,22,4,3,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(46,2,41,0,4,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(47,2,43,1,4,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(48,2,33,2,4,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(49,2,45,3,4,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL),
(50,2,10,4,4,'pending',NULL,NULL,'2025-06-04 15:46:51','2025-06-04 15:46:51',NULL);
/*!40000 ALTER TABLE `bingo_board_tiles` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-07-05  0:37:09
