#!/bin/bash
#########################################################################
# Title:         Restart Running Containers Script                      #
# Author(s):     desimaniac                                             #
# URL:           https://github.com/saltyorg/Saltbox                    #
# Description:   Stop running containers and start them back up.        #
# --                                                                    #
#########################################################################
#                   GNU General Public License v3.0                     #
#########################################################################


# Regular color(s)
NORMAL="\033[0;39m"
GREEN="\033[32m"

echo -e "
$GREEN
 ┌───────────────────────────────────────────────────────────────────────────────────┐
 │ Title:             Restart Running Containers Script                              │
 │ Author(s):         desimaniac, salty                                              │
 │ URL:               https://github.com/saltyorg/Saltbox                            │
 │ Description:       Stop running containers and start them back up.                │
 ├───────────────────────────────────────────────────────────────────────────────────┤
 │                        GNU General Public License v3.0                            │
 └───────────────────────────────────────────────────────────────────────────────────┘
$NORMAL
"

containers=$(docker ps -q)
echo Stopping $containers
docker=$(docker stop $containers)

sleep 3

echo Starting $containers
docker=$(docker start $containers)