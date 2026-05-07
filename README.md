**![DropTracker](https://www.droptracker.io/img/droptracker-small.gif)
# DropTracker - What is that?

Greetings! Thanks for visiting our GitHub page. The DropTracker is an all-in-one solution for clans and players alike to track their drops received while playing Old School RuneScape. Employing our own custom-built RuneLite plugin, we've been able to provide users with years worth of real-time tracking & statistics.

This repository contains all of the necessary code to operate most back-end functions of our application.
The code is quite heavily intermingled for the different portions of our app, but the entry points are below:
- Primary Discord Bot (@DropTracker.io) - `main.py`
- Secondary Discord Bot (Hall of Fame) - `bots/hall_of_fame.py`
- Internal Discord Bot (Reads incoming webhook submissions) - `webhook_bot.py`
- API - `new_api.py`

Furthermore, the following files outline the primary entry points for the different "modules" or "cogs":
- Database models: `db/models/**`
- Submission processing: `data/submissions.py`
- Sending notifications: `services/notification_handler.py`
- Points system: `services/points.py`
- Redis infrastructure: `services/redis_updates.py`
- Hall of Fame generation logic: `services/hall_of_fame.py`
- Loot Leaderboard Updates: `



[RuneLite Plugin](https://www.github.com/joelhalen/droptracker-plugin)
