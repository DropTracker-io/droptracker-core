#!/bin/bash

echo "Attempting to restart..."
cd /store/droptracker/disc
screen -X -S DTcore kill
echo "Killed core app."
screen -X -S DT-pu kill
echo "Killed player updater."
./real_startup.sh
echo "Bot restarted successfully after a failed heartbeat response."
exit 0