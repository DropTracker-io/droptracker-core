#!/bin/bash

echo "Attempting to restart..."
cd /store/droptracker/disc
screen -X -S DTcore kill
echo "Killed core app."
screen -X -S DT-pu kill
echo "Killed player updater."
screen -X -S DT-webhooks kill
echo "Killed webhook bot."
screen -X -S DT-lootboards kill
echo "Killed lootboard updater."
screen -X -S DT-api kill
echo "Killed API app."
screen -X -S DT-hof kill
echo "Killed Hall of Fame bot."
screen -X -S DT-heartbeat kill
echo "Killed heartbeat bot."
./real_startup.sh
echo "Bot restarted successfully after a failed heartbeat response."
exit 0